#!/usr/bin/env python3
"""
summary.py — Aggregate and analyse results from verification_for_filter.py runs.

Reads results.jsonl files from one or more result directories (top-level or
per-case sub-directories) and produces a consolidated report.

Usage:
  python3 summary.py /path/to/result_dir [/path/to/result_dir2 ...]
  python3 summary.py /path/to/result_dir --output report.json
  python3 summary.py /path/to/result_dir --csv report.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_results(paths: list[str]) -> list[dict]:
    """Load all result records from one or more directories.

    Each path can be:
      - A directory containing results.jsonl (and optionally per-case sub-dirs)
      - A direct path to a .jsonl file
    """
    records: list[dict] = []
    seen = set()  # dedup by (instance_id, model, round, duration)

    for p in paths:
        if os.path.isfile(p) and p.endswith(".jsonl"):
            _load_jsonl(p, records, seen)
        elif os.path.isdir(p):
            # Only read per-case sub-directories (skip top-level results.jsonl)
            for entry in sorted(os.listdir(p)):
                entry_path = os.path.join(p, entry)
                if os.path.isdir(entry_path):
                    sub = os.path.join(entry_path, "results.jsonl")
                    if os.path.isfile(sub):
                        _load_jsonl(sub, records, seen)

    return records


def _load_jsonl(path: str, out: list, seen: set) -> None:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (
                rec.get("instance_id", ""),
                rec.get("model", ""),
                rec.get("round", 0),
                rec.get("duration_seconds", 0),
            )
            if key not in seen:
                seen.add(key)
                out.append(rec)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyse(records: list[dict]) -> dict:
    """Build a structured summary from result records."""

    # Group by model
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_model[r.get("model", "unknown")].append(r)

    # Group by instance
    by_instance: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_instance[r["instance_id"]].append(r)

    models_summary = {}
    for model, runs in sorted(by_model.items()):
        total = len(runs)
        resolved = sum(1 for r in runs if r.get("resolved"))
        errors = sum(1 for r in runs if r.get("error"))
        durations = [r["duration_seconds"] for r in runs if r.get("duration_seconds")]
        avg_dur = round(sum(durations) / len(durations), 1) if durations else 0

        models_summary[model] = {
            "total_runs": total,
            "resolved": resolved,
            "failed": total - resolved,
            "errors": errors,
            "resolve_rate": round(resolved / total * 100, 2) if total else 0,
            "avg_duration_seconds": avg_dur,
        }

    # Per-instance breakdown (across all models/rounds)
    instances_summary = {}
    for iid, runs in sorted(by_instance.items()):
        total = len(runs)
        resolved = sum(1 for r in runs if r.get("resolved"))
        models_used = sorted(set(r.get("model", "") for r in runs))
        instances_summary[iid] = {
            "total_runs": total,
            "resolved": resolved,
            "resolve_rate": round(resolved / total * 100, 2) if total else 0,
            "models": models_used,
        }

    # Per-model per-instance (for detailed drill-down)
    model_instance: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in records:
        model = r.get("model", "unknown")
        iid = r["instance_id"]
        if iid not in model_instance[model]:
            model_instance[model][iid] = {"runs": 0, "resolved": 0}
        model_instance[model][iid]["runs"] += 1
        if r.get("resolved"):
            model_instance[model][iid]["resolved"] += 1

    # Global stats
    total = len(records)
    resolved = sum(1 for r in records if r.get("resolved"))

    return {
        "total_records": total,
        "total_resolved": resolved,
        "total_failed": total - resolved,
        "overall_resolve_rate": round(resolved / total * 100, 2) if total else 0,
        "models": models_summary,
        "instances": instances_summary,
        "model_instance": {m: dict(v) for m, v in model_instance.items()},
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_report(summary: dict) -> None:
    """Print a human-readable report to stdout."""
    print(f"\n{'='*70}")
    print(f"  VERIFICATION SUMMARY")
    print(f"{'='*70}")
    print(f"  Total runs     : {summary['total_records']}")
    print(f"  Resolved       : {summary['total_resolved']}")
    print(f"  Failed         : {summary['total_failed']}")
    print(f"  Resolve rate   : {summary['overall_resolve_rate']}%")

    # Per-model table
    print(f"\n{'─'*70}")
    print(f"  {'Model':<30} {'Runs':>6} {'Pass':>6} {'Fail':>6} {'Err':>5} {'Rate':>7} {'Avg(s)':>8}")
    print(f"  {'─'*28} {'─'*6} {'─'*6} {'─'*6} {'─'*5} {'─'*7} {'─'*8}")
    for model, ms in sorted(summary["models"].items()):
        print(f"  {model:<30} {ms['total_runs']:>6} {ms['resolved']:>6} "
              f"{ms['failed']:>6} {ms['errors']:>5} {ms['resolve_rate']:>6.1f}% "
              f"{ms['avg_duration_seconds']:>7.1f}")

    # Per-instance table (sorted by resolve rate ascending = hardest first)
    instances = summary["instances"]
    sorted_inst = sorted(instances.items(), key=lambda x: x[1]["resolve_rate"])

    print(f"\n{'─'*70}")
    print(f"  {'Instance ID':<50} {'Runs':>5} {'Pass':>5} {'Rate':>7}")
    print(f"  {'─'*48} {'─'*5} {'─'*5} {'─'*7}")

    # Show hardest 20 + easiest 10
    display = sorted_inst[:20]
    if len(sorted_inst) > 30:
        display.append(None)  # separator
        display.extend(sorted_inst[-10:])
    else:
        display = sorted_inst

    for item in display:
        if item is None:
            print(f"  {'... (middle omitted) ...'}")
            continue
        iid, s = item
        print(f"  {iid:<50} {s['total_runs']:>5} {s['resolved']:>5} {s['resolve_rate']:>6.1f}%")

    print(f"{'='*70}\n")


def write_csv(summary: dict, path: str) -> None:
    """Write a CSV with one row per (model, instance)."""
    rows = []
    for model, instances in summary["model_instance"].items():
        for iid, stats in sorted(instances.items()):
            rate = round(stats["resolved"] / stats["runs"] * 100, 2) if stats["runs"] else 0
            rows.append({
                "model": model,
                "instance_id": iid,
                "runs": stats["runs"],
                "resolved": stats["resolved"],
                "resolve_rate": rate,
            })

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["model", "instance_id", "runs", "resolved", "resolve_rate"])
        w.writeheader()
        w.writerows(rows)
    print(f"CSV → {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Summarise verification results from results.jsonl files"
    )
    ap.add_argument("dirs", nargs="+",
                    help="Result directories or .jsonl files to analyse")
    ap.add_argument("--output", "-o", default=None,
                    help="Write JSON summary to this file")
    ap.add_argument("--csv", default=None,
                    help="Write per-model-instance CSV to this file")
    ap.add_argument("--quiet", "-q", action="store_true",
                    help="Suppress console report")
    args = ap.parse_args()

    records = load_results(args.dirs)
    if not records:
        sys.exit("ERROR: no result records found")

    summary = analyse(records)

    if not args.quiet:
        print_report(summary)

    if args.output:
        # Strip large per-instance data for JSON output readability
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        print(f"JSON → {args.output}")

    if args.csv:
        write_csv(summary, args.csv)


if __name__ == "__main__":
    main()
