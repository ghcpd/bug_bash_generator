#!/usr/bin/env python3
"""Extract all unique 'repo' field values from .json/.jsonl files recursively.
Output: repo.txt in the script's execution directory.
"""
import json
import os
import sys

def extract_repos(obj):
    results = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "repo":
                if isinstance(v, str):
                    results.add(v)
                elif isinstance(v, (int, float)):
                    results.add(str(v))
            else:
                results |= extract_repos(v)
    elif isinstance(obj, list):
        for item in obj:
            results |= extract_repos(item)
    return results

def process_file(filepath):
    repos = set()
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read().strip()
        if not content:
            return repos
        try:
            data = json.loads(content)
            if isinstance(data, list):
                for item in data:
                    repos |= extract_repos(item)
            else:
                repos |= extract_repos(data)
        except (json.JSONDecodeError, ValueError):
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    repos |= extract_repos(obj)
                except (json.JSONDecodeError, ValueError):
                    pass
    except Exception as e:
        print("Warning: failed to process %s: %s" % (filepath, e), file=sys.stderr)
    return repos

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    all_repos = set()
    file_count = 0

    print("Scanning directory: %s" % script_dir, flush=True)

    for root, dirs, files in os.walk(script_dir):
        for fname in files:
            if fname.endswith(".json") or fname.endswith(".jsonl"):
                fpath = os.path.join(root, fname)
                print("  Processing: %s" % fpath, flush=True)
                found = process_file(fpath)
                if found:
                    all_repos |= found
                file_count += 1

    output_path = os.path.join(script_dir, "repo.txt")
    with open(output_path, "w", encoding="utf-8") as out:
        for r in sorted(all_repos):
            out.write(r + "\n")

    print("Scanned %d json/jsonl files. Found %d unique repo values." % (file_count, len(all_repos)), flush=True)
    print("Written to: %s" % output_path, flush=True)

if __name__ == "__main__":
    main()
