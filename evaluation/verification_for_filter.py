#!/usr/bin/env python3
"""
run_and_verify.py — Automated copilot-cli bug fix verification pipeline.

For each matching (tar.gz, jsonl) pair:
  1. Extract tar.gz to a working directory
  2. Parse jsonl for issue_text, test_command, category, etc.
  3. For bug/feature categories: hide test files so the model can't see them
  4. Run copilot-cli with issue_text as prompt (no test generation allowed)
  5. Restore test files
  6. Run setup_command + test_command
  7. Check if fail_to_pass tests now pass
  8. Record results
  9. Repeat for N rounds

Usage:
  python3 verification_for_filter.py \
    --tar-dir  /path/to/tar.gz/ \
    --jsonl-dir /path/to/jsonl/ \
    --output-dir /path/to/results/ \
    --model claude-opus-4.6 \
    --rounds 3

Prerequisites:
  - copilot CLI installed and in PATH
  - GITHUB_TOKEN env var set (or pass --github-token)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Category classification
# ---------------------------------------------------------------------------

# Categories / sub_types that are clearly "bug fix" or "feature" —
# for these we hide existing test files so the model cannot reverse-engineer
# a fix from assertion expectations.
BUG_FEATURE_KEYWORDS = {
    "bug", "fix", "feature", "security", "logic", "algorithm",
    "data handling", "transformation", "error handling", "validation",
    "resource", "concurrency", "synchronization", "api", "interface",
    "configuration", "environment", "access control",
    "wrong_operator", "wrong_comparison", "missing_validation",
    "missing_transform", "missing_check", "off_by_one", "wrong_return",
    "missing_initialization",
}


def is_bug_or_feature(labels: dict) -> bool:
    """Return True if this case is a bug/feature task (not a test-writing task)."""
    category = labels.get("category", "").lower()
    sub_type = labels.get("sub_type", "").lower()
    combined = f"{category} {sub_type}"
    for kw in BUG_FEATURE_KEYWORDS:
        if kw in combined:
            return True
    # Default to True — safer to hide tests unless we know otherwise
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_test_files(test_command: str, fail_to_pass: list) -> set:
    """Derive test-file names from test_command and fail_to_pass entries."""
    files = set()
    # e.g. "python3.11 -m pytest test_synthetic_1.py -xvs"
    files.update(re.findall(r"(test_\S+\.py)", test_command))
    # e.g. "test_synthetic_1.py::test_func_name"
    for entry in fail_to_pass:
        parts = entry.split("::")
        if parts:
            files.add(parts[0])
    return files


def find_matching_pairs(tar_dir: str, jsonl_dir: str) -> list:
    """Return list of dicts {name, tar_path, jsonl_path} for matching pairs."""
    tars = {
        f.replace(".tar.gz", ""): os.path.join(tar_dir, f)
        for f in os.listdir(tar_dir)
        if f.endswith(".tar.gz")
    }
    jsonls = {
        f.replace(".jsonl", ""): os.path.join(jsonl_dir, f)
        for f in os.listdir(jsonl_dir)
        if f.endswith(".jsonl")
    }
    common = sorted(set(tars) & set(jsonls))
    return [
        {"name": n, "tar_path": tars[n], "jsonl_path": jsonls[n]}
        for n in common
    ]


def parse_jsonl(path: str) -> dict:
    """Read the first JSON object from a .jsonl file."""
    with open(path) as fh:
        return json.loads(fh.readline())


def safe_extract_tar(tar_path: str, dest: str):
    """Extract a tar.gz while blocking path-traversal attacks."""
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            resolved = os.path.realpath(os.path.join(dest, member.name))
            if not resolved.startswith(os.path.realpath(dest)):
                raise RuntimeError(
                    f"Path traversal detected in tar member: {member.name}"
                )
        tf.extractall(dest)


def build_prompt(issue_text: str, hints_text: str, labels: dict) -> str:
    """Construct the copilot-cli prompt."""
    parts = []
    if is_bug_or_feature(labels):
        parts.append(
            "Fix the following issue in this codebase. "
            "Do NOT create, modify, or generate any test files or test cases. "
            "Focus only on fixing the production source code.\n\n"
        )
    else:
        parts.append("Fix the following issue in this codebase.\n\n")

    parts.append(f"## Issue\n\n{issue_text}\n")

    if hints_text:
        parts.append(f"\n## Hints\n\n{hints_text}\n")
    return "".join(parts)


def parse_test_results(output: str, fail_to_pass: list) -> tuple:
    """Return (passed, failed) lists by scanning pytest output."""
    passed, failed = [], []
    for test in fail_to_pass:
        if re.search(rf"{re.escape(test)}.*PASSED", output):
            passed.append(test)
        else:
            failed.append(test)
    return passed, failed


# ---------------------------------------------------------------------------
# Docker image resolution
# ---------------------------------------------------------------------------

def _map_lookup_by_slug(map_path: str, repo_slug: str) -> str | None:
    """Return the acr_image for repo_slug from a JSONL map file, or None.

    *map_path* may be a file or a directory containing ``docker-map.jsonl``.
    """
    if os.path.isdir(map_path):
        map_path = os.path.join(map_path, "docker-map.jsonl")
    if not os.path.isfile(map_path):
        return None
    with open(map_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("repo_slug") == repo_slug:
                return entry.get("acr_image")
    return None


def _docker_available() -> bool:
    """Return True if the docker CLI is on PATH and responsive."""
    try:
        subprocess.run(
            ["docker", "version"],
            capture_output=True, timeout=10,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def resolve_deps_image(
    repo_slug: str,
    repo_dir: str,
    map_path: str = "",
    global_image: str = "",
) -> str:
    """Resolve the Docker image for a case.

    Priority:
      1. global_image override (--deps-image / DEPS_IMAGE)
      2. Local image exists (docker inspect)
      3. Map lookup → pull from ACR
      4. Build from Dockerfile.deps in the extracted repo
      5. Empty string → run on host
    """
    if global_image:
        return global_image

    if not _docker_available():
        print("         Docker: not available, falling back to host")
        return ""

    local_tag = f"bugbash-deps-{repo_slug.lower()}"

    # 1. Local image
    ret = subprocess.run(
        ["docker", "image", "inspect", local_tag],
        capture_output=True, timeout=30,
    )
    if ret.returncode == 0:
        print(f"         Docker: reusing local image {local_tag}")
        return local_tag

    # 2. Map lookup → pull
    if map_path:
        acr_image = _map_lookup_by_slug(map_path, repo_slug)
        if acr_image:
            pull = subprocess.run(
                ["docker", "pull", acr_image],
                capture_output=True, text=True, timeout=300,
            )
            if pull.returncode == 0:
                subprocess.run(
                    ["docker", "tag", acr_image, local_tag],
                    capture_output=True, timeout=30,
                )
                print(f"         Docker: pulled {acr_image}, tagged as {local_tag}")
                return local_tag
            else:
                print(f"         Docker: pull failed for {acr_image}, will try build")

    # 3. Build from Dockerfile.deps
    dockerfile = os.path.join(repo_dir, "Dockerfile.deps")
    if os.path.isfile(dockerfile):
        print(f"         Docker: building from Dockerfile.deps …")
        build = subprocess.run(
            ["docker", "build", "-f", dockerfile, "-t", local_tag, "."],
            cwd=repo_dir, capture_output=True, text=True, timeout=600,
        )
        if build.returncode == 0:
            print(f"         Docker: built {local_tag}")
            return local_tag
        else:
            print(f"         Docker: build failed, falling back to host")

    return ""


# ---------------------------------------------------------------------------
# Core single-case runner
# ---------------------------------------------------------------------------

def run_single_case(
    case_data: dict,
    tar_path: str,
    model: str,
    github_token: str,
    round_num: int,
    deps_image: str = "",
    map_path: str = "",
) -> dict:
    """Extract → fix → test → record for one (case, round)."""
    instance_id = case_data["instance_id"]
    issue_text = case_data["issue_text"]
    hints_text = case_data.get("hints_text", "")
    setup_cmd = case_data.get("setup_command", "")
    test_cmd = case_data["test_command"]
    fail_to_pass = case_data.get("fail_to_pass", [])
    labels = case_data.get("labels", {})

    result = {
        "instance_id": instance_id,
        "model": model,
        "round": round_num,
        "resolved": False,
        "tests_passed": [],
        "tests_failed": [],
        "error": None,
        "duration_seconds": 0,
    }

    t0 = time.time()
    work_dir = tempfile.mkdtemp(prefix=f"verify_{instance_id}_r{round_num}_")

    try:
        # --- 1. Extract -------------------------------------------------------
        print(f"  [1/6] Extracting archive …")
        safe_extract_tar(tar_path, work_dir)

        entries = os.listdir(work_dir)
        if len(entries) == 1 and os.path.isdir(os.path.join(work_dir, entries[0])):
            repo_dir = os.path.join(work_dir, entries[0])
        else:
            repo_dir = work_dir

        # Resolve Docker image for this case
        repo_slug = case_data.get("repo", "").replace("/", "__")
        resolved_image = resolve_deps_image(
            repo_slug, repo_dir,
            map_path=map_path,
            global_image=deps_image,
        )

        # Set DEPS_IMAGE env var so copilot-cli and child processes can use it
        if resolved_image:
            os.environ["DEPS_IMAGE"] = resolved_image

        # --- 2. Hide test files (bug / feature) --------------------------------
        test_files = extract_test_files(test_cmd, fail_to_pass)
        hidden_tests = {}

        if is_bug_or_feature(labels) and test_files:
            print(f"  [2/6] Hiding test files: {test_files}")
            backup = os.path.join(work_dir, "_test_backup")
            os.makedirs(backup, exist_ok=True)
            for tf in test_files:
                src = os.path.join(repo_dir, tf)
                if os.path.exists(src):
                    dst = os.path.join(backup, tf)
                    shutil.copy2(src, dst)
                    os.remove(src)
                    hidden_tests[tf] = dst
        else:
            print(f"  [2/6] (skip — not bug/feature)")

        # --- 3. Init local git repo (copilot expects one) ----------------------
        print(f"  [3/6] Initializing git repo …")
        subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.email", "verify@local"],
                        cwd=repo_dir, check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.name", "Verify"],
                        cwd=repo_dir, check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir,
                        check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # --- 4. Run copilot-cli ------------------------------------------------
        prompt = build_prompt(issue_text, hints_text, labels)
        print(f"  [4/6] Running copilot-cli ({model}) …")

        env = os.environ.copy()
        env["GITHUB_TOKEN"] = github_token

        cp = subprocess.run(
            [
                "copilot", "-p", prompt,
                "--allow-all",
                "--no-ask-user",
                "--model", model,
            ],
            cwd=repo_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if cp.returncode != 0:
            result["error"] = f"copilot exit {cp.returncode}: {cp.stderr[:500]}"
            print(f"  WARNING: copilot returned {cp.returncode}")

        # --- 5. Restore test files ----------------------------------------------
        if hidden_tests:
            print(f"  [5/6] Restoring test files …")
            for tf, bak in hidden_tests.items():
                shutil.copy2(bak, os.path.join(repo_dir, tf))
        else:
            print(f"  [5/6] (skip)")

        # --- 6. Setup + run tests (inside Docker) ----------------------------
        print(f"  [6/6] Running tests …")

        def docker_run(cmd: str, timeout: int = 300) -> subprocess.CompletedProcess:
            """Run a shell command inside the deps Docker container."""
            if resolved_image:
                # Ensure pythonX.Y aliases resolve to the container's python3
                shimmed_cmd = (
                    "for v in 3.8 3.9 3.10 3.11 3.12 3.13; do "
                    "  if ! command -v python$v >/dev/null 2>&1; then "
                    "    ln -sf \"$(command -v python3)\" /usr/local/bin/python$v 2>/dev/null || true; "
                    "  fi; "
                    "done && " + cmd
                )
                docker_cmd = [
                    "docker", "run", "--rm",
                    "-v", f"{repo_dir}:/repo",
                    "-w", "/repo",
                    resolved_image,
                    "bash", "-c", shimmed_cmd,
                ]
                return subprocess.run(
                    docker_cmd, capture_output=True, text=True, timeout=timeout,
                )
            else:
                return subprocess.run(
                    cmd, shell=True, cwd=repo_dir,
                    capture_output=True, text=True, timeout=timeout,
                )

        if setup_cmd:
            print(f"         setup: {setup_cmd}")
            docker_run(setup_cmd)

        tr = docker_run(test_cmd)
        output = tr.stdout + tr.stderr
        passed, failed = parse_test_results(output, fail_to_pass)

        result["tests_passed"] = passed
        result["tests_failed"] = failed
        result["resolved"] = len(failed) == 0 and len(passed) > 0
        result["test_exit_code"] = tr.returncode
        result["test_output_tail"] = output[-2000:]

        status = "RESOLVED" if result["resolved"] else "FAILED"
        print(f"         {status}  passed={len(passed)} failed={len(failed)}")

    except subprocess.TimeoutExpired:
        result["error"] = "timeout"
        print(f"  ERROR: timeout")
    except Exception as exc:
        result["error"] = str(exc)
        print(f"  ERROR: {exc}")
    finally:
        result["duration_seconds"] = round(time.time() - t0, 2)
        shutil.rmtree(work_dir, ignore_errors=True)

    return result


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def write_summary(results: list, output_dir: str, model: str):
    """Write summary.json and print a table."""
    case_runs = defaultdict(list)
    for r in results:
        case_runs[r["instance_id"]].append(r)

    per_case = {}
    for iid, runs in case_runs.items():
        ok = sum(1 for r in runs if r["resolved"])
        per_case[iid] = {
            "rounds": len(runs),
            "resolved": ok,
            "resolve_rate": round(ok / len(runs) * 100, 2),
        }

    total = len(results)
    ok = sum(1 for r in results if r["resolved"])
    summary = {
        "model": model,
        "total_cases": len(case_runs),
        "total_runs": total,
        "resolved": ok,
        "resolve_rate": round(ok / total * 100, 2) if total else 0,
        "per_case": per_case,
    }

    path = os.path.join(output_dir, "summary.json")
    with open(path, "w") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"SUMMARY  model={model}")
    print(f"{'='*60}")
    print(f"Cases : {summary['total_cases']}")
    print(f"Runs  : {summary['total_runs']}")
    print(f"Pass  : {ok}/{total}  ({summary['resolve_rate']}%)")
    for iid, s in per_case.items():
        print(f"  {iid}: {s['resolved']}/{s['rounds']} ({s['resolve_rate']}%)")
    print(f"Results → {output_dir}/")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Automated copilot-cli fix → test verification pipeline"
    )
    ap.add_argument("--tar-dir", required=True,
                    help="Directory with .tar.gz archives")
    ap.add_argument("--jsonl-dir", required=True,
                    help="Directory with .jsonl metadata files")
    ap.add_argument("--output-dir", required=True,
                    help="Directory to write results.jsonl / summary.json")
    ap.add_argument("--model", default="claude-opus-4.6",
                    help="Model for copilot-cli (default: claude-opus-4.6)")
    ap.add_argument("--rounds", type=int, default=1,
                    help="Rounds per case (default: 1)")
    ap.add_argument("--github-token", default=None,
                    help="GitHub token (or set GITHUB_TOKEN env)")
    ap.add_argument("--filter", default=None,
                    help="Regex — only process matching case names")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip cases that already have results in output-dir")
    ap.add_argument("--deps-image", default=None,
                    help="Docker image for running setup/tests (or set DEPS_IMAGE env)")
    ap.add_argument("--map", default=None,
                    help="Path to docker-map.jsonl for per-repo image lookup")
    args = ap.parse_args()

    token = args.github_token or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("ERROR: set GITHUB_TOKEN or pass --github-token")

    deps_image = args.deps_image or os.environ.get("DEPS_IMAGE", "")
    map_path = args.map or ""
    if deps_image:
        print(f"Docker : {deps_image} (global override)")
    elif map_path:
        print(f"Docker : per-repo via map {map_path}")
    else:
        print("Docker : auto (local → build → host fallback)")

    pairs = find_matching_pairs(args.tar_dir, args.jsonl_dir)
    if not pairs:
        sys.exit("ERROR: no matching tar.gz / jsonl pairs found")

    if args.filter:
        pat = re.compile(args.filter)
        pairs = [p for p in pairs if pat.search(p["name"])]

    print(f"Cases  : {len(pairs)}")
    print(f"Model  : {args.model}")
    print(f"Rounds : {args.rounds}\n")

    os.makedirs(args.output_dir, exist_ok=True)
    results_path = os.path.join(args.output_dir, "results.jsonl")

    # Load existing results for --skip-existing
    existing_runs = set()  # {(instance_id, round)}
    if args.skip_existing and os.path.exists(results_path):
        with open(results_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                existing_runs.add((rec["instance_id"], rec["round"]))
        if existing_runs:
            print(f"Skip-existing: found {len(existing_runs)} existing run(s) in results.jsonl")

    all_results = []

    for idx, pair in enumerate(pairs, 1):
        case = parse_jsonl(pair["jsonl_path"])
        iid = case["instance_id"]
        cat = case.get("labels", {}).get("category", "?")

        print(f"{'='*60}")
        print(f"[{idx}/{len(pairs)}] {iid}  ({cat})")
        print(f"{'='*60}")

        for rnd in range(1, args.rounds + 1):
            if (iid, rnd) in existing_runs:
                print(f"\n--- round {rnd}/{args.rounds} --- SKIPPED (already exists)")
                continue
            print(f"\n--- round {rnd}/{args.rounds} ---")
            result = run_single_case(
                case, pair["tar_path"], args.model, token, rnd,
                deps_image=deps_image,
                map_path=map_path,
            )
            all_results.append(result)

            # append incrementally
            with open(results_path, "a") as fh:
                fh.write(json.dumps(result, ensure_ascii=False) + "\n")

            tag = "PASS" if result["resolved"] else "FAIL"
            print(f"  => {tag}  ({result['duration_seconds']}s)")

        print()

    write_summary(all_results, args.output_dir, args.model)


if __name__ == "__main__":
    main()
