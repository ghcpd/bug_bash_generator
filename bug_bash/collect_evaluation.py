#!/usr/bin/env python3
"""
collect_evaluation.py — Collect all evaluation.jsonl files into one.

Walks a root directory with structure:
    root/
      {instance_id}/
        {model}/
          evaluation.jsonl

Merges all evaluation.jsonl records into root/all_evaluation.jsonl.

Usage:
    python collect_evaluation.py /path/to/evaluation/root
    python collect_evaluation.py /path/to/evaluation/root --output custom_name.jsonl
"""

import argparse
import json
import os
import sys
from collections import defaultdict


def collect(root_dir: str, output_name: str = "all_evaluation.jsonl") -> None:
    if not os.path.isdir(root_dir):
        print(f"ERROR: {root_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    output_path = os.path.join(root_dir, output_name)
    records = []
    stats = defaultdict(int)
    skipped = 0

    # Walk: root / instance_id / model / evaluation.jsonl
    for instance_id in sorted(os.listdir(root_dir)):
        instance_dir = os.path.join(root_dir, instance_id)
        if not os.path.isdir(instance_dir):
            continue
        # Skip output file itself
        if instance_id == output_name:
            continue

        for model_name in sorted(os.listdir(instance_dir)):
            model_dir = os.path.join(instance_dir, model_name)
            if not os.path.isdir(model_dir):
                continue

            eval_path = os.path.join(model_dir, "evaluation.jsonl")
            if not os.path.isfile(eval_path):
                continue

            try:
                with open(eval_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        record = json.loads(line)
                        records.append(record)
                        stats["instances"].add(instance_id) if isinstance(
                            stats.get("instances"), set
                        ) else None
                        stats["models"].add(model_name) if isinstance(
                            stats.get("models"), set
                        ) else None
            except (json.JSONDecodeError, OSError) as e:
                print(f"  WARN: skipping {eval_path}: {e}", file=sys.stderr)
                skipped += 1
                continue

    # Collect unique instances and models from records
    unique_instances = set()
    unique_models = set()
    passed = 0
    failed = 0
    for r in records:
        unique_instances.add(r.get("instance_id", ""))
        unique_models.add(r.get("model", ""))
        if r.get("test_passed"):
            passed += 1
        else:
            failed += 1

    # Write output
    with open(output_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"{'=' * 50}")
    print(f"  Collected evaluation records")
    print(f"{'=' * 50}")
    print(f"  Root:      {root_dir}")
    print(f"  Output:    {output_path}")
    print(f"  Instances: {len(unique_instances)}")
    print(f"  Models:    {len(unique_models)} ({', '.join(sorted(unique_models))})")
    print(f"  Records:   {len(records)}")
    print(f"  Passed:    {passed}")
    print(f"  Failed:    {failed}")
    if skipped:
        print(f"  Skipped:   {skipped}")
    print(f"{'=' * 50}")


def main():
    parser = argparse.ArgumentParser(
        description="Collect all evaluation.jsonl files into one."
    )
    parser.add_argument(
        "root_dir",
        help="Root directory containing {instance_id}/{model}/evaluation.jsonl",
    )
    parser.add_argument(
        "--output",
        default="all_evaluation.jsonl",
        help="Output filename (written inside root_dir, default: all_evaluation.jsonl)",
    )
    args = parser.parse_args()
    collect(args.root_dir, args.output)


if __name__ == "__main__":
    main()
