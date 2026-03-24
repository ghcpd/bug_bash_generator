#!/usr/bin/env python3
"""
generate_step04_package_case_artifacts.py — Parse Copilot CLI output into SWE-bench artifacts (tar.gz + jsonl)
=====================================================================================

Workflow:
  1. Parse AI output, extract case JSON definitions
  2. Copy repo source (excluding .git) as snapshot
  3. Write test file
  4. Package as tar.gz
  5. Write jsonl metadata
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def parse_ai_output(output_path: str) -> list[dict]:
    """Extract case JSON definitions from AI output, supporting multiple formats."""
    with open(output_path, "r", encoding="utf-8") as f:
        content = f.read()

    cases: list[dict] = []

    # Strategy 1: CASE_START / CASE_END delimiters
    for m in re.finditer(r"CASE_START\s*\n(.*?)CASE_END", content, re.DOTALL):
        try:
            cases.append(json.loads(m.group(1).strip()))
        except json.JSONDecodeError:
            pass

    # Strategy 2: ```json ``` code blocks
    if not cases:
        for m in re.finditer(r"```json\s*\n(.*?)```", content, re.DOTALL):
            try:
                obj = json.loads(m.group(1).strip())
                if "instance_id" in obj or "patch" in obj:
                    cases.append(obj)
            except json.JSONDecodeError:
                pass

    # Strategy 3: entire content as JSON
    if not cases:
        try:
            obj = json.loads(content.strip())
            if isinstance(obj, dict):
                cases.append(obj)
            elif isinstance(obj, list):
                cases.extend(obj)
        except json.JSONDecodeError:
            pass

    return cases


def create_snapshot(repo_dir: str, case: dict, snapshot_dir: str) -> None:
    """Copy repo with metadata folders stripped. Repo is already in buggy state."""
    shutil.copytree(
        repo_dir,
        snapshot_dir,
        ignore=shutil.ignore_patterns(
            ".git", ".github", "__pycache__", "*.pyc", ".tox", ".eggs", "*.egg-info",
            "issues.md",  # anti-cheat: issue_text lives in JSONL, not in repo
        ),
        dirs_exist_ok=True,
    )

    # Write test file
    test_code = case.get("test_code", "")
    test_filename = case.get("test_filename", "test_synthetic.py")
    if test_code:
        test_path = os.path.join(snapshot_dir, test_filename)
        with open(test_path, "w", encoding="utf-8") as f:
            f.write(test_code)

    # Write .github/copilot-instructions.md with build/test commands
    setup_cmd = case.get("setup_command", "pip install -e .")
    test_cmd = case.get("test_command", f"python -m pytest {test_filename} -xvs")
    github_dir = os.path.join(snapshot_dir, ".github")
    os.makedirs(github_dir, exist_ok=True)
    with open(os.path.join(github_dir, "copilot-instructions.md"), "w", encoding="utf-8") as f:
        f.write(f"# Build Command\n\n```bash\n{setup_cmd}\n```\n\n")
        f.write(f"# Workflow\n\n```bash\n{test_cmd}\n```\n")


def package_targz(snapshot_dir: str, output_path: str) -> None:
    """Package snapshot directory as tar.gz (atomic write)."""
    tmp_path = output_path + ".tmp"
    with tarfile.open(tmp_path, "w:gz") as tar:
        tar.add(snapshot_dir, arcname=os.path.basename(snapshot_dir))
    os.replace(tmp_path, output_path)


def write_jsonl(
    case: dict,
    repo_slug: str,
    base_commit: str,
    instance_id: str,
    output_path: str,
    metrics_summary: dict | None = None,
) -> None:
    """Write case metadata to a jsonl file."""
    # Build fail_to_pass
    ftp = case.get("fail_to_pass", [])
    if not ftp:
        test_fn = case.get("test_filename", "test_synthetic.py")
        ftp = [f"{test_fn}::test_placeholder"]

    test_fn = case.get("test_filename", "test_synthetic.py")

    record = {
        "instance_id": instance_id,
        "repo": repo_slug.replace("__", "/"),
        "base_commit": base_commit,
        "workspace_dir": instance_id,
        "source": "synthetic_mutation",
        "setup_command": case.get("setup_command", "SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 pip install -e ."),
        "test_command": case.get("test_command", f"python -m pytest {test_fn} -xvs"),
        "issue_text": case.get("issue_text", ""),
        "hints_text": case.get("hints_text", ""),
        "patches": {
            "patch": case.get("patch", ""),
        },
        "fail_to_pass": ftp,
        "pass_to_pass": case.get("pass_to_pass", []),
        "labels": {
            "category": case.get("category", ""),
            "sub_type": case.get("sub_type", ""),
            "difficulty": case.get("difficulty", "L1"),
            "localization": case.get("localization", "explicit"),
            "context_dependency": case.get("context_dependency", "self_contained"),
            "test_modality": case.get("test_modality", "unit_test"),
            "capabilities": case.get("capabilities", []),
            "multi_solution": case.get("multi_solution", False),
        },
        "quality": {
            "status": "verified_pending_audit",
            "generation_success": True,
            "verification_success": True,
            "critic_success": True,
            "audit_success": None,
            "test_fail_before_patch": True,
            "test_pass_after_patch": True,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mutation_type": case.get("sub_type", ""),
        "mutation_description": case.get("mutation_description", ""),
        "mutation_file": case.get("mutation_file", ""),
        "num_files_changed": len(set(re.findall(
            r'^diff --git a/(.*?) b/', case.get("patch", ""), re.MULTILINE))),
        "num_lines_changed": case.get("patch", "").count("\n+")
        + case.get("patch", "").count("\n-"),
    }

    critic_review = case.get("critic_review")
    if critic_review:
        record["critic_review"] = critic_review
        record["quality"]["critic_success"] = critic_review.get("verdict") != "fail"

    if metrics_summary:
        record["benchmark"] = metrics_summary

    with open(output_path + ".tmp", "w", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(output_path + ".tmp", output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Process Copilot output into tar.gz + jsonl")
    parser.add_argument("--repo-dir", required=True, help="Local path of cloned repository")
    parser.add_argument("--ai-output", required=True, help="Copilot CLI output file")
    parser.add_argument("--repo-slug", required=True, help="owner__repo format")
    parser.add_argument("--base-commit", required=True, help="Repository base commit hash")
    parser.add_argument("--case-index", default="0", help="Case index number")
    parser.add_argument("--timestamp", required=True, help="Timestamp for unique ID")
    parser.add_argument("--targz-dir", required=True, help="tar.gz output directory")
    parser.add_argument("--jsonl-dir", required=True, help="jsonl output directory")
    parser.add_argument("--metrics-summary", help="Aggregated Copilot CLI metrics JSON file")
    args = parser.parse_args()

    metrics_summary = None
    if args.metrics_summary:
        with open(args.metrics_summary, "r", encoding="utf-8") as f:
            metrics_summary = json.load(f)

    cases = parse_ai_output(args.ai_output)
    if not cases:
        print("ERROR: No valid case definitions found in AI output", file=sys.stderr)
        print("AI output content:", file=sys.stderr)
        with open(args.ai_output, "r") as f:
            print(f.read()[:2000], file=sys.stderr)
        sys.exit(1)

    for i, case in enumerate(cases):
        idx = f"{args.case_index}_{i}" if len(cases) > 1 else args.case_index
        import hashlib
        fallback_hash = hashlib.md5(
            f"{args.repo_slug}-{idx}-{args.timestamp}".encode()
        ).hexdigest()[:8]
        instance_id = case.get(
            "instance_id",
            f"test-repo-{fallback_hash}",
        )

        cat = case.get("category", "(not set)")
        diff = case.get("difficulty", "(not set)")
        print(f"\n{'='*60}")
        print(f"Processing case: {instance_id}")
        print(f"  category:   {cat}")
        print(f"  difficulty: {diff}")
        print(f"{'='*60}")

        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_dir = os.path.join(tmpdir, instance_id)
            create_snapshot(args.repo_dir, case, snapshot_dir)

            targz_path = os.path.join(args.targz_dir, f"{instance_id}.tar.gz")
            package_targz(snapshot_dir, targz_path)
            print(f"  tar.gz -> {targz_path}")

        jsonl_path = os.path.join(args.jsonl_dir, f"{instance_id}.jsonl")
        write_jsonl(case, args.repo_slug, args.base_commit, instance_id, jsonl_path, metrics_summary)
        print(f"  jsonl  -> {jsonl_path}")

    print(f"\nTotal cases processed: {len(cases)}")
    print(f"\n{'='*60}")
    print(f"  {'Instance ID':<50} {'Category':<35} {'Difficulty'}")
    print(f"  {'-'*48} {'-'*33} {'-'*10}")
    for i, case in enumerate(cases):
        idx = f"{args.case_index}_{i}" if len(cases) > 1 else args.case_index
        fallback_hash = hashlib.md5(
            f"{args.repo_slug}-{idx}-{args.timestamp}".encode()
        ).hexdigest()[:8]
        iid = case.get(
            "instance_id",
            f"test-repo-{fallback_hash}",
        )
        cat = case.get("category", "(not set)")
        diff = case.get("difficulty", "(not set)")
        print(f"  {iid:<50} {cat:<35} {diff}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
