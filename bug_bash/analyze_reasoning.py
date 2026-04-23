#!/usr/bin/env python3
"""
analyze_reasoning.py — Analyze model reasoning from evaluation.jsonl using LLM.

Samples N records, sends each to copilot CLI for analysis, writes summaries.

Usage:
    export GITHUB_TOKEN=ghp_xxx
    python analyze_reasoning.py all_evaluation.jsonl \
        --sample 50 --workers 5 --output analysis_summaries.jsonl
"""

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ANALYSIS_PROMPT_TEMPLATE = """You are analyzing a bug-fixing session to understand why the bug was easy or hard for the model to solve.

## Context
- **Repo**: {reponame}
- **Instance**: {instance_id}
- **Model**: {model}
- **Resolved**: {resolved}
- **Bug category**: {category}
- **Bug description**: {bug_description}
- **Feature that introduced the bug**: {feature_description}

## Issue Text (what the model saw)
{issue_text}

## Model's Session Log (how the model reasoned)
{session_excerpt}

## Model's Patch (what the model changed)
{patch_excerpt}

## Your Analysis

Analyze this case and output a JSON object with these fields:

1. **issue_leaks**: Does the issue_text directly reveal where the bug is? (e.g., mentions specific API names, shows exact character differences, names the affected component)
2. **issue_leak_details**: What exactly in the issue gave away the location?
3. **model_strategy**: How did the model approach the problem? (keyword_grep / structure_scan / hypothesis_test / blind_search)
4. **search_keywords**: What keywords or patterns did the model grep/search for?
5. **steps_to_locate**: How many tool calls did the model make before finding the buggy code?
6. **was_misled**: Did the model investigate wrong locations first?
7. **misled_details**: If misled, what wrong paths did it go down?
8. **test_peeked**: Did the model read any test files despite being told not to?
9. **fix_complexity**: How complex was the fix? (one_line / few_lines / multi_file / restructure)
10. **why_easy_or_hard**: In 2-3 sentences, explain WHY this bug was easy or hard for the model. Focus on: was the issue too precise? Was the bug too shallow? Was the code structure too simple?
11. **improvement_suggestion**: One specific, actionable suggestion to make this TYPE of bug harder to solve. Focus on how the bug should have been injected differently.
12. **difficulty_rating**: Rate 1-5 (1=trivial, 5=very hard)

Output ONLY valid JSON, no other text.
"""


def truncate(text: str, max_chars: int = 3000) -> str:
    if not text or len(text) <= max_chars:
        return text or ""
    return text[:max_chars] + f"\n... [truncated, {len(text)} total chars]"


def load_and_sample(path: str, n: int, resolved_only: bool = True) -> list:
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if resolved_only and not rec.get("test_passed"):
                    continue
                # Skip records without session data
                if not rec.get("patch") and not rec.get("session"):
                    continue
                records.append(rec)
            except json.JSONDecodeError:
                continue

    if len(records) <= n:
        return records
    return random.sample(records, n)


def analyze_one(rec: dict, idx: int, total: int, model: str = "claude-sonnet-4.6") -> dict:
    """Send one record to copilot CLI for analysis."""
    instance_id = rec.get("instance_id", "unknown")
    print(f"  [{idx}/{total}] Analyzing {instance_id}...")

    # Build the analysis prompt
    session_text = rec.get("session", "") or rec.get("patch", "")
    prompt = ANALYSIS_PROMPT_TEMPLATE.format(
        reponame=rec.get("reponame", ""),
        instance_id=instance_id,
        model=rec.get("model", ""),
        resolved=rec.get("test_passed", False),
        category=rec.get("labels", {}).get("category", ""),
        bug_description=rec.get("bug_description", ""),
        feature_description=rec.get("feature_description", ""),
        issue_text=truncate(rec.get("issue_text", ""), 2000),
        session_excerpt=truncate(session_text, 6000),
        patch_excerpt=truncate(rec.get("patch", ""), 2000),
    )

    # Write prompt to temp file (avoid shell escaping issues)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(prompt)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [
                "copilot",
                "-p", f"Read {tmp_path} and follow the instructions exactly.",
                "--model", model,
                "--yolo",
                "--output-format", "text",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env={**os.environ, "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", "")},
        )
        output = result.stdout.strip()

        # Try to extract JSON from output
        try:
            # Find JSON in output
            start = output.find("{")
            end = output.rfind("}") + 1
            if start >= 0 and end > start:
                analysis = json.loads(output[start:end])
            else:
                analysis = {"error": "no JSON found", "raw": output[:500]}
        except json.JSONDecodeError:
            analysis = {"error": "JSON parse failed", "raw": output[:500]}

    except subprocess.TimeoutExpired:
        analysis = {"error": "timeout"}
    except Exception as e:
        analysis = {"error": str(e)}
    finally:
        os.unlink(tmp_path)

    return {
        "instance_id": instance_id,
        "model": rec.get("model", ""),
        "reponame": rec.get("reponame", ""),
        "resolved": rec.get("test_passed", False),
        "category": rec.get("labels", {}).get("category", ""),
        "analysis": analysis,
    }


def generate_report(summaries: list, output_path: str) -> dict:
    """Aggregate summaries into a top-level report."""
    total = len(summaries)
    errors = [s for s in summaries if "error" in s.get("analysis", {})]
    valid = [s for s in summaries if "error" not in s.get("analysis", {})]

    # Difficulty distribution
    difficulty_dist = {}
    for s in valid:
        d = s["analysis"].get("difficulty_rating", "?")
        difficulty_dist[str(d)] = difficulty_dist.get(str(d), 0) + 1

    # Strategy distribution
    strategy_dist = {}
    for s in valid:
        st = s["analysis"].get("model_strategy", "unknown")
        strategy_dist[st] = strategy_dist.get(st, 0) + 1

    # Issue leak distribution
    leak_count = sum(1 for s in valid if s["analysis"].get("issue_leaks"))
    test_peek_count = sum(1 for s in valid if s["analysis"].get("test_peeked"))
    misled_count = sum(1 for s in valid if s["analysis"].get("was_misled"))

    # Fix complexity
    fix_dist = {}
    for s in valid:
        fc = s["analysis"].get("fix_complexity", "unknown")
        fix_dist[fc] = fix_dist.get(fc, 0) + 1

    # Collect improvement suggestions
    suggestions = {}
    for s in valid:
        sug = s["analysis"].get("improvement_suggestion", "")
        if sug:
            suggestions[sug] = suggestions.get(sug, 0) + 1

    # Top 10 most common suggestions
    top_suggestions = sorted(suggestions.items(), key=lambda x: -x[1])[:10]

    # Collect "why easy" explanations for trivial cases
    trivial_explanations = []
    for s in valid:
        if s["analysis"].get("difficulty_rating", 5) <= 2:
            trivial_explanations.append({
                "instance_id": s["instance_id"],
                "repo": s["reponame"],
                "why": s["analysis"].get("why_easy_or_hard", ""),
                "issue_leaks": s["analysis"].get("issue_leak_details", ""),
            })

    report = {
        "total_analyzed": total,
        "valid_analyses": len(valid),
        "errors": len(errors),
        "difficulty_distribution": difficulty_dist,
        "strategy_distribution": strategy_dist,
        "issue_leaks_location_pct": round(leak_count / max(len(valid), 1) * 100, 1),
        "test_peeked_pct": round(test_peek_count / max(len(valid), 1) * 100, 1),
        "was_misled_pct": round(misled_count / max(len(valid), 1) * 100, 1),
        "fix_complexity_distribution": fix_dist,
        "top_improvement_suggestions": top_suggestions,
        "trivial_case_explanations": trivial_explanations[:20],
    }

    report_path = output_path.replace(".jsonl", "_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nReport → {report_path}")

    return report


def main():
    parser = argparse.ArgumentParser(
        description="Analyze model reasoning from evaluation data"
    )
    parser.add_argument("input", help="Path to all_evaluation.jsonl")
    parser.add_argument("--sample", type=int, default=50, help="Number of records to sample")
    parser.add_argument("--workers", type=int, default=5, help="Concurrent workers")
    parser.add_argument("--output", default="analysis_summaries.jsonl", help="Output file")
    parser.add_argument("--model", default="claude-sonnet-4.6", help="Model for analysis")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for sampling")
    args = parser.parse_args()

    if not os.environ.get("GITHUB_TOKEN"):
        sys.exit("ERROR: set GITHUB_TOKEN env var")

    random.seed(args.seed)

    print(f"Loading {args.input}...")
    records = load_and_sample(args.input, args.sample)
    print(f"Sampled {len(records)} resolved records")
    print(f"Workers: {args.workers}")
    print(f"Analysis model: {args.model}")
    print()

    summaries = []
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(analyze_one, rec, i + 1, len(records), args.model): rec
            for i, rec in enumerate(records)
        }
        for future in as_completed(futures):
            try:
                summary = future.result()
                summaries.append(summary)
                # Append incrementally
                with open(args.output, "a", encoding="utf-8") as f:
                    f.write(json.dumps(summary, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"  ERROR: {e}")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.0f}s ({elapsed / len(records):.1f}s/record)")
    print(f"Summaries → {args.output}")

    # Generate report
    report = generate_report(summaries, args.output)

    # Print quick stats
    print(f"\n{'=' * 50}")
    print(f"  ANALYSIS REPORT")
    print(f"{'=' * 50}")
    print(f"  Analyzed:    {report['valid_analyses']}/{report['total_analyzed']}")
    print(f"  Issue leaks: {report['issue_leaks_location_pct']}%")
    print(f"  Test peeked: {report['test_peeked_pct']}%")
    print(f"  Was misled:  {report['was_misled_pct']}%")
    print(f"  Difficulty:  {report['difficulty_distribution']}")
    print(f"  Strategy:    {report['strategy_distribution']}")
    print(f"  Fix complexity: {report['fix_complexity_distribution']}")
    print(f"\n  Top suggestions:")
    for sug, cnt in report.get("top_improvement_suggestions", [])[:5]:
        print(f"    [{cnt}x] {sug[:100]}")


if __name__ == "__main__":
    main()
