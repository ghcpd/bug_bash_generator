#!/usr/bin/env python3
"""
audit_step02_batch_audit_cases.py — Batch-audit generated SWE-bench cases (jsonl + tar.gz)
===========================================================================

Workflow:
  1. Scan the jsonl directory for all .jsonl files
  2. For each case:
     a. Extract the corresponding tar.gz to a temp directory
    b. Convert jsonl to audit_step03_validate_instance.py-compatible instance JSON
    c. Invoke audit_step03_validate_instance.py for L1-L7 audit
     d. Collect results
  3. Output summary report

Note: audit_step03_validate_instance.py must be co-located in the same directory.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def find_cases(jsonl_dir: str, targz_dir: str) -> list[dict]:
    """Scan jsonl directory, match corresponding tar.gz files."""
    cases = []
    for jsonl_path in sorted(glob.glob(os.path.join(jsonl_dir, "*.jsonl"))):
        case_id = Path(jsonl_path).stem
        targz_path = os.path.join(targz_dir, f"{case_id}.tar.gz")

        with open(jsonl_path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            print(f"[SKIP] Empty jsonl: {jsonl_path}")
            continue

        record = json.loads(lines[0])
        cases.append({
            "case_id": case_id,
            "jsonl_path": jsonl_path,
            "targz_path": targz_path,
            "record": record,
            "targz_exists": os.path.isfile(targz_path),
        })
    return cases


def prepare_workspace(case: dict, work_dir: str) -> str | None:
    """Extract tar.gz and generate audit_step03_validate_instance.py-compatible instance JSON.

    Returns: instance JSON path, or None on failure.
    """
    case_id = case["case_id"]
    record = case["record"]

    if not case["targz_exists"]:
        print(f"[SKIP] tar.gz not found for {case_id}: {case['targz_path']}")
        return None

    # Extract snapshot
    with tarfile.open(case["targz_path"], "r:gz") as tar:
        tar.extractall(work_dir)

    # Find extracted directory (the arcname inside the tar.gz)
    extracted = [
        d for d in os.listdir(work_dir)
        if os.path.isdir(os.path.join(work_dir, d))
    ]
    if not extracted:
        print(f"[ERROR] No directory found in tar.gz for {case_id}")
        return None

    workspace_dir = extracted[0]

    # Generate instance JSON (format expected by audit_step03_validate_instance.py)
    instance = dict(record)
    instance["workspace_dir"] = workspace_dir
    instance.setdefault("pr_number", None)
    instance.setdefault("issue_number", None)
    instance.setdefault("num_files_changed", 1)
    instance.setdefault("num_lines_changed", 0)

    json_path = os.path.join(work_dir, f"{case_id}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(instance, f, indent=2, ensure_ascii=False)

    return json_path


def run_audit(json_path: str, level: str) -> dict:
    """Invoke audit_step03_validate_instance.py to audit a single instance."""
    # audit_step03_validate_instance.py is deployed alongside this file
    script_dir = os.path.dirname(os.path.abspath(__file__))
    audit_script = os.path.join(script_dir, "audit_step03_validate_instance.py")

    if not os.path.isfile(audit_script):
        return {
            "error": f"audit_step03_validate_instance.py not found at {audit_script}. "
                     "Ensure it is deployed alongside audit_step02_batch_audit_cases.py."
        }

    result_file = json_path.replace(".json", "_audit_result.json")
    cmd = [
        sys.executable, audit_script,
        json_path,
        "--level", level,
        "--json-output", result_file,
    ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
        cwd=os.path.dirname(json_path),
    )

    output = {
        "stdout": proc.stdout[-3000:] if len(proc.stdout) > 3000 else proc.stdout,
        "stderr": proc.stderr[-1000:] if len(proc.stderr) > 1000 else proc.stderr,
        "returncode": proc.returncode,
    }

    if os.path.isfile(result_file):
        with open(result_file, "r", encoding="utf-8") as f:
            output["audit_result"] = json.load(f)

    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-audit generated SWE-bench cases")
    parser.add_argument("--jsonl-dir", required=True, help="Directory containing jsonl files")
    parser.add_argument("--targz-dir", required=True, help="Directory containing tar.gz files")
    parser.add_argument("--output-dir", required=True, help="Audit results output directory")
    parser.add_argument("--level", default="L7", help="Audit depth (default: L7)")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    cases = find_cases(args.jsonl_dir, args.targz_dir)
    if not cases:
        print("No cases found to audit.")
        sys.exit(0)

    print(f"Found {len(cases)} case(s) to audit (level={args.level})")
    print("=" * 70)

    summary = {"total": len(cases), "passed": 0, "failed": 0, "skipped": 0}
    all_results = []

    for case in cases:
        case_id = case["case_id"]
        print(f"\n--- Auditing: {case_id} ---")

        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = prepare_workspace(case, tmpdir)
            if not json_path:
                summary["skipped"] += 1
                all_results.append({"case_id": case_id, "status": "skipped"})
                continue

            result = run_audit(json_path, args.level)

            # Record result
            passed = False
            if "audit_result" in result:
                audit_data = result["audit_result"]
                if isinstance(audit_data, list) and audit_data:
                    passed = audit_data[0].get("passed", False)

            if passed:
                summary["passed"] += 1
                status = "PASSED"
            else:
                summary["failed"] += 1
                status = "FAILED"

            all_results.append({
                "case_id": case_id,
                "status": status,
                "audit_result": result.get("audit_result"),
                "stdout_tail": result.get("stdout", "")[-500:],
            })

            # Write per-case audit result
            case_result_path = os.path.join(
                args.output_dir, f"{case_id}_audit.json"
            )
            with open(case_result_path, "w", encoding="utf-8") as f:
                json.dump(all_results[-1], f, indent=2, ensure_ascii=False)

            print(f"  Result: {status}")

    # Summary report
    print(f"\n{'='*70}")
    print(f"  Audit Summary:")
    print(f"    Total:   {summary['total']}")
    print(f"    Passed:  {summary['passed']}")
    print(f"    Failed:  {summary['failed']}")
    print(f"    Skipped: {summary['skipped']}")
    print(f"{'='*70}")

    summary_path = os.path.join(args.output_dir, "audit_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(
            {"summary": summary, "results": all_results},
            f,
            indent=2,
            ensure_ascii=False,
        )
    print(f"Summary written to: {summary_path}")


if __name__ == "__main__":
    main()
