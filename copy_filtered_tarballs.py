#!/usr/bin/env python3
"""
Copy and rename tarballs based on doc_eval_cases_filtered.jsonl.

Reads each record from the jsonl, locates the corresponding tar.gz
in workdir/tarballs/ (named as {base_commit[:12]}_{repoName}.tar.gz),
and copies it to workdir/filtered_tarballs/ with the new name
{owner__repoName__fullBaseCommit}.tar.gz.

Uses multiple threads for fast parallel copying.

Usage:
    python3 copy_filtered_tarballs.py [--workers N]

Expects the following layout relative to the script's directory:
    ./doc_eval_cases_filtered.jsonl
    ./tarballs/*.tar.gz
Output goes to:
    ./filtered_tarballs/
"""

import json
import os
import shutil
import sys
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# Thread-safe counter
class Counter:
    def __init__(self):
        self._lock = threading.Lock()
        self.success = 0
        self.not_found = 0
        self.error = 0

    def inc_success(self):
        with self._lock:
            self.success += 1

    def inc_not_found(self):
        with self._lock:
            self.not_found += 1

    def inc_error(self):
        with self._lock:
            self.error += 1


def copy_one(src_path, dst_path, counter):
    """Copy a single file. Returns (dst_basename, status_str)."""
    dst_name = os.path.basename(dst_path)
    if not os.path.isfile(src_path):
        counter.inc_not_found()
        return dst_name, "NOT_FOUND"
    try:
        shutil.copy2(src_path, dst_path)
        counter.inc_success()
        return dst_name, "OK"
    except Exception as e:
        counter.inc_error()
        return dst_name, f"ERROR: {e}"


def main():
    parser = argparse.ArgumentParser(
        description="Copy & rename tarballs in parallel based on jsonl metadata."
    )
    parser.add_argument(
        "--workers", type=int, default=8,
        help="Number of parallel copy threads (default: 8)"
    )
    args = parser.parse_args()

    workdir = SCRIPT_DIR
    num_workers = args.workers
    jsonl_path = os.path.join(workdir, "doc_eval_cases_filtered.jsonl")
    tarballs_dir = os.path.join(workdir, "tarballs")
    output_dir = os.path.join(workdir, "filtered_tarballs")

    if not os.path.isfile(jsonl_path):
        print(f"ERROR: {jsonl_path} not found")
        sys.exit(1)
    if not os.path.isdir(tarballs_dir):
        print(f"ERROR: {tarballs_dir} not found")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)

    # ---- Phase 1: Build unique copy tasks from jsonl ----
    tasks = {}  # unique_key -> (src_path, dst_path)
    skipped = 0

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            record = json.loads(line)
            repo_field = record.get("repo", "")       # owner/repoName
            base_commit = record.get("base_commit", "")

            if not repo_field or not base_commit:
                skipped += 1
                continue

            # Parse owner and repo name from "owner/repoName"
            if "/" not in repo_field:
                skipped += 1
                continue

            owner, repo_name = repo_field.split("/", 1)

            # Unique key: owner__repoName__commit (used for dedup and dst naming)
            unique_key = f"{owner}__{repo_name}__{base_commit}"

            if unique_key in tasks:
                skipped += 1
                continue

            # Source: {commit[:12]}_{repoName}.tar.gz
            src_name = f"{base_commit[:12]}_{repo_name}.tar.gz"
            src_path = os.path.join(tarballs_dir, src_name)

            # Destination: {owner}__{repoName}__{fullCommit}.tar.gz
            dst_name = f"{unique_key}.tar.gz"
            dst_path = os.path.join(output_dir, dst_name)

            tasks[unique_key] = (src_path, dst_path)

    print(f"Unique copy tasks: {len(tasks)}, Skipped (dup/empty): {skipped}")
    print(f"Using {num_workers} worker threads\n")

    # ---- Phase 2: Parallel copy ----
    counter = Counter()
    done = 0
    total = len(tasks)

    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        futures = {
            pool.submit(copy_one, src, dst, counter): key
            for key, (src, dst) in tasks.items()
        }
        for future in as_completed(futures):
            done += 1
            name, status = future.result()
            if status != "OK":
                print(f"  [{done}/{total}] {status}: {name}")
            elif done % 50 == 0 or done == total:
                print(f"  [{done}/{total}] copied ...", flush=True)

    print(f"\nDone. Copied: {counter.success}, Not found: {counter.not_found}, "
          f"Errors: {counter.error}, Skipped: {skipped}")
    print(f"Output: {output_dir}")


if __name__ == "__main__":
    main()
