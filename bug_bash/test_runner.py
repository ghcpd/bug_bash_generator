#!/usr/bin/env python3
"""
Test Runner — Apply a patch to a repo and run tests in a Docker container.

Standalone, reusable module for verifying model-generated patches.

Usage (CLI):
    python3 test_runner.py <jsonl_file> \\
        --patch-file patch.diff \\
        --images-dir /path/to/images \\
        --repos-dir  /path/to/tar.gz \\
        [--collect] \\
        [--output-file results.json]

Usage (as module):
    from test_runner import run_tests

    result = run_tests(
        jsonl_data=data,
        patch=patch_str,
        image_tag="bugbash-deps-...:latest",
        repo_dir="/path/to/extracted/repo",
        collect=True,
    )

Output (simple mode — collect=False):
    {"passed": true/false, "stdout": "...", "stderr": "..."}

Output (collect mode — collect=True):
    {
        "reponame": "marshmallow-code/marshmallow",
        "instance_id": "feature-add-372f7e23",
        "batch_version": "",
        "issue_text": "...",
        "fail_to_pass": [...],
        "pass_to_pass": [...],
        "labels": {...},
        "resolved": [...],
        "unresolved": [...],
        "still_passing": [...],
        "regressed": [...],
        "cost_usd": 1
    }
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile

# Shared helpers from run_pipeline (same directory)
_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

from run_pipeline import (  # noqa: E402
    CONTAINER_WORKDIR,
    load_jsonl,
    load_docker_image,
    extract_repo,
    ensure_git_repo,
    start_container,
    stop_container,
)


# ─────────────────────────────────────────────────────────
# Pytest output parsing
# ─────────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TEST_STATUS_RE = re.compile(
    r"^(.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)",
    re.MULTILINE,
)


def parse_pytest_output(output: str) -> dict[str, str]:
    """Parse pytest verbose output into {test_id: status}."""
    clean = _ANSI_RE.sub("", output)
    results: dict[str, str] = {}
    for m in _TEST_STATUS_RE.finditer(clean):
        test_id = m.group(1).strip()
        status = m.group(2)
        if "::" in test_id:
            results[test_id] = status
    return results


# ─────────────────────────────────────────────────────────
# Running tests in a container
# ─────────────────────────────────────────────────────────
def run_test_batch(
    container_name: str,
    test_ids: list[str],
    workdir: str = CONTAINER_WORKDIR,
    timeout: int = 600,
) -> tuple[str, int]:
    """Run a batch of pytest test IDs inside the container.

    Returns (combined_output, returncode).
    """
    test_args = " ".join(test_ids)
    cmd = f"cd {workdir} && python3 -m pytest {test_args} -v --tb=short 2>&1"
    r = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return (r.stdout or "") + (r.stderr or ""), r.returncode


# ─────────────────────────────────────────────────────────
# Main test function
# ─────────────────────────────────────────────────────────
def run_tests(
    jsonl_data: dict,
    patch: str,
    image_tag: str,
    repo_dir: str,
    collect: bool = False,
) -> dict:
    """Apply *patch* to a copy of *repo_dir* and run tests.

    Args:
        jsonl_data: Parsed JSONL record.
        patch:      git diff patch string to apply.
        image_tag:  Docker image tag (must already be loaded).
        repo_dir:   Path to extracted repo (will be copied, not modified).
        collect:    If True, run per-test verification and return detailed
                    results. If False, just check pass/fail.

    Returns:
        dict — see module docstring for output schemas.
    """
    instance_id = jsonl_data.get("instance_id", "unknown")
    test_cmd = jsonl_data.get("test_command", "")
    setup_cmd = jsonl_data.get("setup_command", "")
    fail_to_pass = jsonl_data.get("fail_to_pass", [])
    pass_to_pass = jsonl_data.get("pass_to_pass", [])

    work_dir = tempfile.mkdtemp(prefix=f"bugbash_test_{instance_id}_")
    container_name = None

    try:
        # Copy repo for isolation
        local_repo = os.path.join(work_dir, "repo")
        shutil.copytree(repo_dir, local_repo, symlinks=True)
        ensure_git_repo(local_repo)

        # Apply patch
        if patch:
            r = subprocess.run(
                ["git", "apply", "--whitespace=nowarn"],
                input=patch,
                capture_output=True, text=True,
                timeout=30, cwd=local_repo,
            )
            if r.returncode != 0:
                return {
                    "passed": False,
                    "error": f"Patch apply failed: {r.stderr}",
                }

        # Start container with bind mount
        container_name = start_container(image_tag, local_repo)

        # Run setup command if provided
        if setup_cmd:
            subprocess.run(
                ["docker", "exec", container_name, "bash", "-c",
                 f"cd {CONTAINER_WORKDIR} && {setup_cmd}"],
                capture_output=True, text=True, timeout=600,
            )

        # ── Simple mode ──
        if not collect:
            r = subprocess.run(
                ["docker", "exec", container_name, "bash", "-c",
                 f"cd {CONTAINER_WORKDIR} && {test_cmd}"],
                capture_output=True, text=True, timeout=600,
            )
            return {
                "passed": r.returncode == 0,
                "stdout": (r.stdout or "")[-2000:],
                "stderr": (r.stderr or "")[-2000:],
            }

        # ── Collect mode: per-test verification ──
        BATCH_SIZE = 50

        # Run fail_to_pass tests
        ftp_results: dict[str, str] = {}
        if fail_to_pass:
            for i in range(0, len(fail_to_pass), BATCH_SIZE):
                batch = fail_to_pass[i:i + BATCH_SIZE]
                output, _ = run_test_batch(container_name, batch)
                ftp_results.update(parse_pytest_output(output))

        # Run pass_to_pass tests
        ptp_results: dict[str, str] = {}
        if pass_to_pass:
            for i in range(0, len(pass_to_pass), BATCH_SIZE):
                batch = pass_to_pass[i:i + BATCH_SIZE]
                output, _ = run_test_batch(container_name, batch)
                ptp_results.update(parse_pytest_output(output))

        # Categorise results
        # A test is "resolved" if it now passes; "unresolved" if it
        # explicitly failed/errored or was never collected.
        resolved = [
            t for t in fail_to_pass
            if ftp_results.get(t) == "PASSED"
        ]
        unresolved = [
            t for t in fail_to_pass
            if ftp_results.get(t) != "PASSED"
        ]
        # A test is "regressed" only if it explicitly FAILED or ERRORed.
        # Tests not collected (absent from output) are assumed still passing.
        _fail_statuses = {"FAILED", "ERROR"}
        still_passing = [
            t for t in pass_to_pass
            if ptp_results.get(t, "PASSED") not in _fail_statuses
        ]
        regressed = [
            t for t in pass_to_pass
            if ptp_results.get(t) in _fail_statuses
        ]

        return {
            "reponame": jsonl_data.get("repo", ""),
            "instance_id": instance_id,
            "batch_version": jsonl_data.get("batch_version", ""),
            "issue_text": jsonl_data.get("issue_text", ""),
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": pass_to_pass,
            "labels": jsonl_data.get("labels", {}),
            "resolved": resolved,
            "unresolved": unresolved,
            "still_passing": still_passing,
            "regressed": regressed,
            "cost_usd": 1,
        }

    finally:
        if container_name:
            stop_container(container_name)
        shutil.rmtree(work_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Test Runner — apply patch and run tests in Docker")
    parser.add_argument(
        "jsonl_file", help="JSONL task file")
    parser.add_argument(
        "--patch-file", required=True,
        help="Patch file path (use '-' for stdin)")
    parser.add_argument(
        "--images-dir", required=True,
        help="Docker images directory (contains owner/repo/deps.tar)")
    parser.add_argument(
        "--repos-dir", required=True,
        help="Repo archives directory (contains workspace_dir.tar.gz)")
    parser.add_argument(
        "--collect", action="store_true",
        help="Collect detailed per-test results")
    parser.add_argument(
        "--output-file", "-o", default=None,
        help="Write JSON results to file (default: stdout)")

    args = parser.parse_args()

    # Load JSONL
    data = load_jsonl(args.jsonl_file)
    repo = data.get("repo", "")
    instance_id = data.get("instance_id", "unknown")
    workspace_dir = data.get("workspace_dir", instance_id)

    # Read patch
    if args.patch_file == "-":
        patch = sys.stdin.read()
    else:
        with open(args.patch_file) as f:
            patch = f.read()

    # Load Docker image
    image_tar = os.path.join(args.images_dir, repo, "deps.tar")
    if not os.path.isfile(image_tar):
        logging.error(f"Image not found: {image_tar}")
        sys.exit(1)
    image_tag = load_docker_image(image_tar)

    # Extract repo
    repo_tar = os.path.join(args.repos_dir, f"{workspace_dir}.tar.gz")
    if not os.path.isfile(repo_tar):
        repo_tar = os.path.join(args.repos_dir, f"{instance_id}.tar.gz")
    if not os.path.isfile(repo_tar):
        logging.error(f"Repo archive not found: {repo_tar}")
        sys.exit(1)

    tmp_dir = tempfile.mkdtemp(prefix="bugbash_test_base_")
    try:
        repo_dir = extract_repo(repo_tar, tmp_dir)
        result = run_tests(
            data, patch, image_tag, repo_dir, collect=args.collect)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Output
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output_file:
        with open(args.output_file, "w") as f:
            f.write(output + "\n")
        logging.info(f"Results written to {args.output_file}")
    else:
        print(output)


if __name__ == "__main__":
    main()
