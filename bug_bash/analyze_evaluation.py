#!/usr/bin/env python3
"""
analyze_evaluation.py — Three-step evaluation analysis for all_evaluation.jsonl.

Step 1: Pass-rate statistics per model (+ model×repo cross-tab, + batch comparison).
Step 2: Environment-failure classification for failed cases.
Step 3: LLM-based reasoning analysis (triggered when effective pass-rate gap < threshold).

Usage:
    python analyze_evaluation.py all_evaluation.jsonl
    python analyze_evaluation.py all_evaluation.jsonl --batch 20260420
    python analyze_evaluation.py all_evaluation.jsonl --force-reasoning --workers 5
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ───────────────────────── Constants ─────────────────────────

DEFAULT_GAP_THRESHOLD = 10  # percentage-points
DEFAULT_SAMPLE_SIZE = 50
DEFAULT_WORKERS = 5

# ───────────────────────── Step 2: env-error detection ───────
#
# The `track` field contains the model's Copilot CLI session log (tool calls,
# streaming chunks, etc.), NOT a clean build/test log.  Regex must be very
# tight to avoid false-positives on tool names like "apply_patch".
#
# Primary classification uses the *structured* fields (patch, resolved,
# unresolved, regressed, fail_to_pass).  Regex on `track` is only used as
# a secondary signal for genuine infrastructure errors.

# Patterns matched ONLY against lines that look like real error output, not
# tool-call metadata.  We pre-filter track lines before matching.
ENV_ERROR_PATTERNS = [
    ("docker_build_fail", [
        re.compile(r"Cannot connect to the Docker daemon", re.I),
        re.compile(r"Error response from daemon", re.I),
        re.compile(r"docker:.*image.*not found", re.I),
    ]),
    ("deps_install_fail", [
        re.compile(r"ERROR: Could not install packages", re.I),
        re.compile(r"Could not find a version that satisfies", re.I),
        re.compile(r"No matching distribution found for", re.I),
        re.compile(r"pip.*ReadTimeoutError", re.I),
        re.compile(r"npm ERR! code E(RESOLVE|NOSPC|TIMEOUT)", re.I),
    ]),
    ("test_timeout", [
        re.compile(r"SIGKILL|killed by signal 9", re.I),
        re.compile(r"out of memory|OOMKilled", re.I),
        re.compile(r"timed?\s*out.*running tests", re.I),
    ]),
    ("context_budget_exceeded", [
        re.compile(r"context.*budget.*exceeded", re.I),
        re.compile(r"maximum.*turns.*reached", re.I),
        re.compile(r"session.*timed?\s*out", re.I),
        re.compile(r"context.*window.*full", re.I),
    ]),
]


def _extract_error_lines(track: str, max_lines: int = 200) -> str:
    """Extract lines from track that look like actual error output,
    skipping JSON tool-call wrappers and streaming chunk metadata."""
    error_lines = []
    for line in track.split("\n"):
        stripped = line.strip()
        # Skip JSON-ish lines (tool calls, streaming metadata)
        if stripped.startswith(("{", "[", '"')):
            continue
        # Skip empty
        if not stripped:
            continue
        error_lines.append(stripped)
        if len(error_lines) >= max_lines:
            break
    return "\n".join(error_lines)


def classify_failure(rec: dict) -> str:
    """Classify a failed record into env-error category or 'model_wrong'.

    Uses structured fields first, then falls back to regex on cleaned
    track content.
    """
    patch = (rec.get("patch", "") or "").strip()
    track = rec.get("track", "") or ""
    fail_to_pass = rec.get("fail_to_pass", [])
    resolved = rec.get("resolved", [])
    unresolved = rec.get("unresolved", [])
    regressed = rec.get("regressed", [])

    # ── 1. No patch produced at all ──
    if not patch:
        return "no_output"

    # ── 2. Check verification error field (if present) ──
    verification = rec.get("verification", {})
    if isinstance(verification, dict):
        err = verification.get("error", "")
        if err:
            for category, patterns in ENV_ERROR_PATTERNS:
                for pat in patterns:
                    if pat.search(err):
                        return category
            # "patch did not apply" from run_tests ⇒ model's patch was wrong
            if re.search(r"patch did not apply|patch failed", err, re.I):
                return "model_wrong"

    # ── 3. Secondary: scan cleaned track for infra errors ──
    cleaned = _extract_error_lines(track)
    for category, patterns in ENV_ERROR_PATTERNS:
        for pat in patterns:
            if pat.search(cleaned):
                return category

    # ── 4. Default: model produced a patch but tests didn't pass ──
    return "model_wrong"


# ───────────────────────── Step 1: Statistics ────────────────

def stream_stats(path: str, batch_filter: str | None = None):
    """
    Stream-read all_evaluation.jsonl, build aggregated counters.
    Returns (records_meta, per_model, per_model_repo, per_batch_model, all_fail_records).
    """
    per_model = defaultdict(lambda: {"total": 0, "pass": 0, "fail": 0})
    per_model_repo = defaultdict(lambda: defaultdict(lambda: {"total": 0, "pass": 0, "fail": 0}))
    per_batch_model = defaultdict(lambda: defaultdict(lambda: {"total": 0, "pass": 0, "fail": 0}))

    fail_records = []
    pass_records = []
    total_lines = 0
    skipped = 0

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total_lines += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue

            batch_ver = rec.get("batch_version", "")
            if batch_filter and batch_ver != batch_filter:
                skipped += 1
                continue

            model = rec.get("model", "unknown")
            repo = rec.get("reponame", "unknown")
            passed = bool(rec.get("test_passed"))

            # Per-model
            per_model[model]["total"] += 1
            if passed:
                per_model[model]["pass"] += 1
            else:
                per_model[model]["fail"] += 1

            # Per-model×repo
            per_model_repo[model][repo]["total"] += 1
            if passed:
                per_model_repo[model][repo]["pass"] += 1
            else:
                per_model_repo[model][repo]["fail"] += 1

            # Per-batch×model
            per_batch_model[batch_ver][model]["total"] += 1
            if passed:
                per_batch_model[batch_ver][model]["pass"] += 1
            else:
                per_batch_model[batch_ver][model]["fail"] += 1

            # Keep fields needed for analysis
            slim = {
                "instance_id": rec.get("instance_id", ""),
                "model": model,
                "reponame": repo,
                "batch_version": batch_ver,
                "track": rec.get("track", ""),
                "patch": rec.get("patch", ""),
                "session": rec.get("session", ""),
                "issue_text": rec.get("issue_text", ""),
                "bug_description": rec.get("bug_description", ""),
                "feature_description": rec.get("feature_description", ""),
                "labels": rec.get("labels", {}),
                "verification": rec.get("verification", {}),
                "test_passed": passed,
            }
            if passed:
                pass_records.append(slim)
            else:
                fail_records.append(slim)

    meta = {"total_records": total_lines, "skipped": skipped}
    return meta, dict(per_model), per_model_repo, per_batch_model, fail_records, pass_records


def compute_rate(d: dict) -> float:
    if d["total"] == 0:
        return 0.0
    return round(d["pass"] / d["total"] * 100, 1)


# ───────────────────────── Step 2: Failure classification ────

def classify_failures(fail_records: list) -> dict:
    """Classify all fail records and return per-model breakdown."""
    per_model_class = defaultdict(lambda: defaultdict(int))
    per_record_class = []

    for rec in fail_records:
        cat = classify_failure(rec)
        model = rec.get("model", "unknown")
        per_model_class[model][cat] += 1
        per_record_class.append({
            "instance_id": rec["instance_id"],
            "model": model,
            "reponame": rec["reponame"],
            "failure_category": cat,
        })

    return dict(per_model_class), per_record_class


def compute_effective_rate(per_model: dict, per_model_class: dict) -> dict:
    """
    Effective pass rate = pass / (total - env_errors).
    env_errors = everything except 'model_wrong'.
    """
    result = {}
    for model, stats in per_model.items():
        class_dist = per_model_class.get(model, {})
        env_errors = sum(v for k, v in class_dist.items() if k != "model_wrong")
        effective_total = stats["total"] - env_errors
        result[model] = {
            "total": stats["total"],
            "pass": stats["pass"],
            "fail": stats["fail"],
            "env_errors": env_errors,
            "model_wrong": class_dist.get("model_wrong", 0),
            "effective_total": effective_total,
            "raw_rate": compute_rate(stats),
            "effective_rate": round(stats["pass"] / max(effective_total, 1) * 100, 1),
        }
    return result


# ───────────────────────── Step 3: LLM Reasoning Analysis ────

SUCCESS_ANALYSIS_PROMPT = """You are a bug-design auditor. A model SUCCESSFULLY fixed a synthetic bug. Your goal is to figure out WHY it was too easy and how to make similar bugs harder.

## Context
- **Repo**: {reponame}
- **Instance**: {instance_id}
- **Model**: {model}
- **Bug category**: {category}
- **Bug description** (ground truth, not shown to model): {bug_description}

## Issue Text (what the model saw as the "bug report")
{issue_text}

## Model's Session Log (how the model reasoned & searched)
{session_excerpt}

## Model's Patch (what the model changed)
{patch_excerpt}

## Your Analysis — focus on how to make bugs HARDER

Output a JSON object:

1. **issue_leaks_location**: true/false — Does the issue text reveal WHERE the bug is? (mentions specific file names, function names, class names, variable names that directly lead to the buggy code)
2. **issue_leak_details**: What exactly in the issue gave away the location? Quote the leaking phrases.
3. **issue_leaks_fix**: true/false — Does the issue text essentially describe what the fix should be?
4. **issue_clarity**: Rate 1-5 how clear the issue is about the root cause (1=vague, 5=basically tells you the answer)
5. **bug_complexity**: Rate 1-5 (1=single value swap, 2=one-line logic change, 3=multi-line in one file, 4=multi-file coordinated change, 5=architectural/design-level)
6. **model_strategy**: How did the model find the bug? (keyword_grep / error_message_trace / structure_scan / hypothesis_test / blind_search)
7. **search_keywords**: What keywords or patterns did the model grep for? List them.
8. **steps_to_locate**: How many tool calls before the model found the buggy code?
9. **was_trivial_grep**: true/false — Could the bug be found by simply grepping a keyword from the issue?
10. **fix_is_obvious**: true/false — Once located, was the fix immediately obvious without deep understanding?
11. **what_made_it_easy**: 2-3 sentences explaining the PRIMARY reason this bug was easy. Focus on: issue text leakage, shallow bug depth, simple code structure.
12. **how_to_make_harder**: 2-3 CONCRETE, ACTIONABLE suggestions to make this specific type of bug harder. Examples: "Add an indirection layer so the buggy function is called from a wrapper", "Spread the bug across 2 files", "Make the issue describe symptoms without naming the affected API".
13. **difficulty_rating**: Rate 1-5 overall difficulty (1=trivial, 5=very hard)

Output ONLY valid JSON, no other text.
"""

FAILURE_ANALYSIS_PROMPT = """You are analyzing a case where a model FAILED to fix a synthetic bug. Your goal is to understand what made this bug hard and extract patterns we can replicate.

## Context
- **Repo**: {reponame}
- **Instance**: {instance_id}
- **Model**: {model}
- **Bug category**: {category}
- **Bug description** (ground truth): {bug_description}

## Issue Text (what the model saw)
{issue_text}

## Model's Session Log (how the model reasoned)
{session_excerpt}

## Model's Patch (what the model produced — incorrect)
{patch_excerpt}

## Your Analysis — focus on what made this bug HARD

Output a JSON object:

1. **root_cause**: One of: "wrong_location" | "incomplete_fix" | "wrong_approach" | "misunderstood_issue" | "correct_idea_bad_implementation" | "gave_up" | "overwhelmed_by_codebase"
2. **root_cause_detail**: 2-3 sentences on what specifically went wrong in the model's reasoning
3. **what_confused_model**: What aspect of the bug or codebase confused the model? (e.g., "indirection through 3 layers", "misleading variable names", "red herring in a similar file")
4. **issue_was_vague**: true/false — Was the issue text properly vague (didn't leak location)?
5. **bug_required_deep_understanding**: true/false — Did fixing this bug require understanding the broader architecture?
6. **model_strategy**: How did the model approach it? (keyword_grep / error_message_trace / structure_scan / hypothesis_test / blind_search / gave_up)
7. **where_model_got_stuck**: At what point did the model go wrong? (never_found_location / found_but_wrong_fix / found_but_incomplete / wrong_file_entirely)
8. **difficulty_rating**: Rate 1-5 (1=trivial, 5=very hard)
9. **what_made_it_hard**: 2-3 sentences explaining WHY this bug was hard for the model
10. **replicable_pattern**: Can we design more bugs with this same difficulty pattern? Describe the pattern in one sentence.

Output ONLY valid JSON, no other text.
"""


def truncate(text: str, max_chars: int = 3000) -> str:
    if not text or len(text) <= max_chars:
        return text or ""
    return text[:max_chars] + f"\n... [truncated, {len(text)} total chars]"


def _to_str(val) -> str:
    """Normalize LLM output to string (handles list, dict, etc)."""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        return "; ".join(str(v) for v in val)
    if isinstance(val, dict):
        return json.dumps(val, ensure_ascii=False)
    return str(val) if val else ""


def _call_copilot(prompt: str, analysis_model: str) -> dict:
    """Call copilot CLI with a prompt, return parsed JSON analysis."""
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
                "--model", analysis_model,
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

        try:
            start = output.find("{")
            end = output.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(output[start:end])
            else:
                return {"error": "no JSON found", "raw": output[:500]}
        except json.JSONDecodeError:
            return {"error": "JSON parse failed", "raw": output[:500]}

    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        os.unlink(tmp_path)


def analyze_one(rec: dict, idx: int, total: int, analysis_model: str = "claude-sonnet-4.6") -> dict:
    """Analyze one record (success or failure) via copilot CLI."""
    instance_id = rec.get("instance_id", "unknown")
    passed = rec.get("test_passed", False)
    label = "PASS" if passed else "FAIL"
    print(f"  [{idx}/{total}] {label} {instance_id} ({rec.get('model', '')})...")

    session_text = rec.get("session", "") or ""
    template = SUCCESS_ANALYSIS_PROMPT if passed else FAILURE_ANALYSIS_PROMPT

    prompt = template.format(
        reponame=rec.get("reponame", ""),
        instance_id=instance_id,
        model=rec.get("model", ""),
        category=rec.get("labels", {}).get("category", ""),
        bug_description=rec.get("bug_description", ""),
        issue_text=truncate(rec.get("issue_text", ""), 2000),
        session_excerpt=truncate(session_text, 6000),
        patch_excerpt=truncate(rec.get("patch", ""), 2000),
    )

    analysis = _call_copilot(prompt, analysis_model)

    return {
        "instance_id": instance_id,
        "model": rec.get("model", ""),
        "reponame": rec.get("reponame", ""),
        "test_passed": passed,
        "analysis": analysis,
    }


def run_reasoning_analysis(
    pass_records: list,
    fail_records: list,
    per_record_class: list,
    sample_size: int,
    workers: int,
    output_path: str,
    analysis_model: str,
    seed: int,
) -> list:
    """Run Step 3: LLM reasoning analysis on both success and failure cases."""
    # Filter fail records to model_wrong only
    class_map = {r["instance_id"] + "|" + r["model"]: r["failure_category"]
                 for r in per_record_class}
    model_wrong_records = []
    for rec in fail_records:
        key = rec["instance_id"] + "|" + rec["model"]
        cat = class_map.get(key, "model_wrong")
        if cat == "model_wrong":
            rec["failure_category"] = cat
            model_wrong_records.append(rec)

    # Combine: all pass records + model_wrong fail records
    all_candidates = pass_records + model_wrong_records
    if not all_candidates:
        print("  No records to analyze.")
        return []

    random.seed(seed)
    if len(all_candidates) <= sample_size:
        selected = all_candidates
    else:
        # Stratified: keep ratio of pass/fail, sample up to sample_size
        n_pass = len(pass_records)
        n_fail = len(model_wrong_records)
        ratio = n_pass / max(n_pass + n_fail, 1)
        n_sample_pass = max(1, round(sample_size * ratio))
        n_sample_fail = sample_size - n_sample_pass

        sampled_pass = random.sample(pass_records, min(n_sample_pass, n_pass))
        sampled_fail = random.sample(model_wrong_records, min(n_sample_fail, n_fail))
        selected = sampled_pass + sampled_fail

    n_pass_selected = sum(1 for r in selected if r.get("test_passed"))
    n_fail_selected = len(selected) - n_pass_selected
    print(f"  Analyzing {len(selected)} cases "
          f"({n_pass_selected} pass + {n_fail_selected} fail, workers={workers})")

    if not os.environ.get("GITHUB_TOKEN"):
        print("  ERROR: GITHUB_TOKEN not set. Cannot run LLM analysis.")
        return []

    summaries = []
    t0 = time.time()

    reasoning_path = output_path.replace(".json", "_reasoning.jsonl")
    open(reasoning_path, "w").close()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(analyze_one, rec, i + 1, len(selected), analysis_model): rec
            for i, rec in enumerate(selected)
        }
        for future in as_completed(futures):
            try:
                summary = future.result()
                summaries.append(summary)
                with open(reasoning_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(summary, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"  ERROR: {e}")

    elapsed = time.time() - t0
    print(f"  Done in {elapsed:.0f}s ({elapsed / max(len(selected), 1):.1f}s/record)")
    print(f"  Details → {reasoning_path}")
    return summaries


def aggregate_reasoning(summaries: list) -> dict:
    """Aggregate LLM reasoning results for both success and failure cases."""
    pass_summaries = [s for s in summaries if s.get("test_passed") and "error" not in s.get("analysis", {})]
    fail_summaries = [s for s in summaries if not s.get("test_passed") and "error" not in s.get("analysis", {})]
    errors = sum(1 for s in summaries if "error" in s.get("analysis", {}))

    # ── Success analysis: why bugs were easy ──
    success_agg = {}
    if pass_summaries:
        n = len(pass_summaries)
        issue_leaks_loc = sum(1 for s in pass_summaries if s["analysis"].get("issue_leaks_location"))
        issue_leaks_fix = sum(1 for s in pass_summaries if s["analysis"].get("issue_leaks_fix"))
        trivial_grep = sum(1 for s in pass_summaries if s["analysis"].get("was_trivial_grep"))
        fix_obvious = sum(1 for s in pass_summaries if s["analysis"].get("fix_is_obvious"))

        # Distributions
        clarity_dist = defaultdict(int)
        complexity_dist = defaultdict(int)
        difficulty_dist = defaultdict(int)
        strategy_dist = defaultdict(int)
        for s in pass_summaries:
            a = s["analysis"]
            clarity_dist[str(a.get("issue_clarity", "?"))] += 1
            complexity_dist[str(a.get("bug_complexity", "?"))] += 1
            difficulty_dist[str(a.get("difficulty_rating", "?"))] += 1
            strategy_dist[a.get("model_strategy", "unknown")] += 1

        # Collect actionable suggestions
        how_to_harder = defaultdict(int)
        for s in pass_summaries:
            sug = _to_str(s["analysis"].get("how_to_make_harder", ""))
            if sug:
                how_to_harder[sug] += 1
        top_harder = sorted(how_to_harder.items(), key=lambda x: -x[1])[:15]

        # Collect "what made it easy"
        easy_reasons = defaultdict(int)
        for s in pass_summaries:
            reason = _to_str(s["analysis"].get("what_made_it_easy", ""))
            if reason:
                easy_reasons[reason] += 1
        top_easy = sorted(easy_reasons.items(), key=lambda x: -x[1])[:15]

        success_agg = {
            "count": n,
            "issue_leaks_location_pct": round(issue_leaks_loc / n * 100, 1),
            "issue_leaks_fix_pct": round(issue_leaks_fix / n * 100, 1),
            "trivial_grep_pct": round(trivial_grep / n * 100, 1),
            "fix_obvious_pct": round(fix_obvious / n * 100, 1),
            "issue_clarity_dist": dict(clarity_dist),
            "bug_complexity_dist": dict(complexity_dist),
            "difficulty_dist": dict(difficulty_dist),
            "strategy_dist": dict(strategy_dist),
            "top_how_to_make_harder": top_harder,
            "top_what_made_easy": top_easy,
        }

    # ── Failure analysis: what made bugs hard ──
    failure_agg = {}
    if fail_summaries:
        n = len(fail_summaries)
        root_cause_dist = defaultdict(int)
        strategy_dist = defaultdict(int)
        stuck_dist = defaultdict(int)
        difficulty_dist = defaultdict(int)
        issue_vague = sum(1 for s in fail_summaries if s["analysis"].get("issue_was_vague"))
        deep_understanding = sum(1 for s in fail_summaries if s["analysis"].get("bug_required_deep_understanding"))

        for s in fail_summaries:
            a = s["analysis"]
            root_cause_dist[a.get("root_cause", "unknown")] += 1
            strategy_dist[a.get("model_strategy", "unknown")] += 1
            stuck_dist[a.get("where_model_got_stuck", "unknown")] += 1
            difficulty_dist[str(a.get("difficulty_rating", "?"))] += 1

        # Replicable patterns
        patterns = defaultdict(int)
        for s in fail_summaries:
            pat = _to_str(s["analysis"].get("replicable_pattern", ""))
            if pat:
                patterns[pat] += 1
        top_patterns = sorted(patterns.items(), key=lambda x: -x[1])[:10]

        # What made hard
        hard_reasons = defaultdict(int)
        for s in fail_summaries:
            reason = _to_str(s["analysis"].get("what_made_it_hard", ""))
            if reason:
                hard_reasons[reason] += 1
        top_hard = sorted(hard_reasons.items(), key=lambda x: -x[1])[:10]

        failure_agg = {
            "count": n,
            "root_cause_dist": dict(root_cause_dist),
            "strategy_dist": dict(strategy_dist),
            "where_stuck_dist": dict(stuck_dist),
            "difficulty_dist": dict(difficulty_dist),
            "issue_was_vague_pct": round(issue_vague / n * 100, 1),
            "required_deep_understanding_pct": round(deep_understanding / n * 100, 1),
            "top_replicable_patterns": top_patterns,
            "top_what_made_hard": top_hard,
        }

    return {
        "total_analyzed": len(summaries),
        "valid_pass": len(pass_summaries),
        "valid_fail": len(fail_summaries),
        "errors": errors,
        "success_analysis": success_agg,
        "failure_analysis": failure_agg,
    }


# ───────────────────────── Output: JSON report ───────────────

def build_report(
    meta: dict,
    per_model: dict,
    per_model_repo,
    per_batch_model,
    per_model_class: dict,
    effective_rates: dict,
    reasoning_summary: dict | None,
    batch_filter: str | None,
) -> dict:
    """Build the full JSON report."""
    report = {
        "meta": meta,
        "batch_filter": batch_filter,
        "step1_pass_rate": {},
        "step1_model_repo_matrix": {},
        "step1_batch_comparison": {},
        "step2_failure_classification": {},
        "step2_effective_rates": effective_rates,
        "step3_reasoning": reasoning_summary,
    }

    # Step 1: per-model
    for model, stats in sorted(per_model.items()):
        report["step1_pass_rate"][model] = {
            **stats,
            "rate": compute_rate(stats),
        }

    # Step 1: model×repo matrix
    all_repos = set()
    for model_repos in per_model_repo.values():
        all_repos.update(model_repos.keys())
    for model in sorted(per_model_repo.keys()):
        report["step1_model_repo_matrix"][model] = {
            repo: {**per_model_repo[model].get(repo, {"total": 0, "pass": 0, "fail": 0}),
                   "rate": compute_rate(per_model_repo[model].get(repo, {"total": 0, "pass": 0, "fail": 0}))}
            for repo in sorted(all_repos)
        }

    # Step 1: batch comparison
    for batch, models in sorted(per_batch_model.items()):
        report["step1_batch_comparison"][batch] = {
            m: {**s, "rate": compute_rate(s)}
            for m, s in sorted(models.items())
        }

    # Step 2: per-model failure classes
    report["step2_failure_classification"] = per_model_class

    return report


# ───────────────────────── Main ──────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Three-step evaluation analysis for all_evaluation.jsonl"
    )
    parser.add_argument("input", help="Path to all_evaluation.jsonl")
    parser.add_argument("--batch", default=None, help="Filter by batch_version")
    parser.add_argument("--gap-threshold", type=float, default=DEFAULT_GAP_THRESHOLD,
                        help=f"pp gap below which converging warning fires (default: {DEFAULT_GAP_THRESHOLD})")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE_SIZE,
                        help=f"Max records for Step 3 (≤N: full, >N: sample N; default: {DEFAULT_SAMPLE_SIZE})")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Concurrent workers for Step 3 (default: {DEFAULT_WORKERS})")
    parser.add_argument("--output", default="eval_analysis_report.json",
                        help="Output JSON file path")
    parser.add_argument("--analysis-model", default="claude-sonnet-4.6",
                        help="Model for Step 3 analysis")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--tokens-file", default="copilot_tokens",
                        help="File containing GitHub PAT tokens (one per line)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip interactive confirmation, run Step 3 directly")
    args = parser.parse_args()

    output_json = args.output if args.output.endswith(".json") else args.output + ".json"

    # ── Load tokens from file ──
    tokens = []
    tokens_path = Path(args.tokens_file)
    if tokens_path.exists():
        with open(tokens_path, "r", encoding="utf-8") as tf:
            tokens = [line.strip() for line in tf if line.strip() and not line.startswith("#")]
        if tokens:
            os.environ["GITHUB_TOKEN"] = tokens[0]
            print(f"  Loaded {len(tokens)} token(s) from {tokens_path}")
    elif os.environ.get("GITHUB_TOKEN"):
        tokens = [os.environ["GITHUB_TOKEN"]]
    if not tokens:
        print("  WARNING: No tokens found (no copilot_tokens file and no GITHUB_TOKEN env).")
        print("           Step 3 LLM analysis will be unavailable.")

    print("=" * 60)
    print("  EVALUATION ANALYSIS")
    print("=" * 60)
    if args.batch:
        print(f"  Batch filter: {args.batch}")
    print(f"  Input: {args.input}")
    print()

    # ── Step 1: Stream statistics ──
    print("━━ Step 1: Computing pass rates ━━")
    t0 = time.time()
    meta, per_model, per_model_repo, per_batch_model, fail_records, pass_records = \
        stream_stats(args.input, args.batch)
    elapsed = time.time() - t0
    print(f"  Read {meta['total_records']} records in {elapsed:.1f}s "
          f"(skipped {meta['skipped']})")
    print(f"  Pass: {len(pass_records)}, Fail: {len(fail_records)}")

    for model, stats in sorted(per_model.items()):
        rate = compute_rate(stats)
        print(f"  {model:30s}  {stats['pass']:4d}/{stats['total']:4d}  = {rate}%")
    print()

    # ── Step 2: Classify failures ──
    print("━━ Step 2: Classifying failures ━━")
    t1 = time.time()
    per_model_class, per_record_class = classify_failures(fail_records)
    effective_rates = compute_effective_rate(per_model, per_model_class)
    elapsed = time.time() - t1
    print(f"  Classified {len(fail_records)} failures in {elapsed:.1f}s")

    for model, eff in sorted(effective_rates.items()):
        print(f"  {model:30s}  raw={eff['raw_rate']}%  "
              f"env_err={eff['env_errors']}  model_wrong={eff['model_wrong']}  "
              f"effective={eff['effective_rate']}%")

    # Check gap
    eff_rates = [e["effective_rate"] for e in effective_rates.values()]
    gap = (max(eff_rates) - min(eff_rates)) if eff_rates else 0
    print(f"\n  Effective rate gap: {gap:.1f}pp")
    print()

    # ── Print Step 1+2 summary ──
    print("=" * 60)
    print("  STEP 1+2 SUMMARY")
    print("=" * 60)
    for model, eff in sorted(effective_rates.items()):
        print(f"  {model:30s}  raw={eff['raw_rate']}%  "
              f"effective={eff['effective_rate']}%  "
              f"(env_err={eff['env_errors']}, model_wrong={eff['model_wrong']})")
    print(f"\n  Gap: {gap:.1f}pp")
    print("=" * 60)

    # ── Step 3: LLM reasoning analysis ──
    reasoning_summary = None
    model_wrong_count = sum(c.get("model_wrong", 0) for c in per_model_class.values())
    total_analyzable = len(pass_records) + model_wrong_count
    sample_count = min(total_analyzable, args.sample)

    if total_analyzable == 0:
        print("\n  No records to analyze. Skipping Step 3.")
    elif not tokens:
        print("\n  No tokens available. Cannot run Step 3.")
    elif args.yes:
        print(f"\n━━ Step 3: LLM reasoning analysis ━━")
        print(f"  {len(pass_records)} pass + {model_wrong_count} model_wrong = "
              f"{total_analyzable} total, sampling {sample_count}")
        summaries = run_reasoning_analysis(
            pass_records=pass_records,
            fail_records=fail_records,
            per_record_class=per_record_class,
            sample_size=args.sample,
            workers=args.workers,
            output_path=output_json,
            analysis_model=args.analysis_model,
            seed=args.seed,
        )
        if summaries:
            reasoning_summary = aggregate_reasoning(summaries)
    else:
        print(f"\n  {len(pass_records)} pass + {model_wrong_count} model_wrong = "
              f"{total_analyzable} analyzable cases.")
        print(f"  Will analyze {sample_count} cases (stratified sample).")
        print(f"  - SUCCESS cases: why bugs were easy, issue leak, complexity")
        print(f"  - FAILURE cases: what confused the model, replicable patterns")
        answer = input("  Run Step 3? [y/N]: ").strip().lower()
        if answer in ("y", "yes"):
            print(f"\n━━ Step 3: LLM reasoning analysis ━━")
            summaries = run_reasoning_analysis(
                pass_records=pass_records,
                fail_records=fail_records,
                per_record_class=per_record_class,
                sample_size=args.sample,
                workers=args.workers,
                output_path=output_json,
                analysis_model=args.analysis_model,
                seed=args.seed,
            )
            if summaries:
                reasoning_summary = aggregate_reasoning(summaries)
        else:
            print("  Skipped Step 3.")
    print()

    # ── Build & write JSON report ──
    report = build_report(
        meta=meta,
        per_model=per_model,
        per_model_repo=per_model_repo,
        per_batch_model=per_batch_model,
        per_model_class=per_model_class,
        effective_rates=effective_rates,
        reasoning_summary=reasoning_summary,
        batch_filter=args.batch,
    )

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"  JSON report → {output_json}")

    # ── Print reasoning results if available ──
    if reasoning_summary:
        sa = reasoning_summary.get("success_analysis", {})
        fa = reasoning_summary.get("failure_analysis", {})
        print()
        print("=" * 60)
        print("  REASONING ANALYSIS RESULTS")
        print("=" * 60)
        print(f"  Analyzed: pass={reasoning_summary['valid_pass']}, "
              f"fail={reasoning_summary['valid_fail']}, "
              f"errors={reasoning_summary['errors']}")

        if sa:
            print(f"\n  ── WHY BUGS WERE EASY (success cases) ──")
            print(f"  Issue leaks location: {sa.get('issue_leaks_location_pct', '?')}%")
            print(f"  Issue leaks fix:      {sa.get('issue_leaks_fix_pct', '?')}%")
            print(f"  Trivial grep:         {sa.get('trivial_grep_pct', '?')}%")
            print(f"  Fix obvious:          {sa.get('fix_obvious_pct', '?')}%")
            print(f"  Issue clarity dist:   {sa.get('issue_clarity_dist', {})}")
            print(f"  Bug complexity dist:  {sa.get('bug_complexity_dist', {})}")
            print(f"  Strategy:             {sa.get('strategy_dist', {})}")
            if sa.get("top_how_to_make_harder"):
                print(f"\n  HOW TO MAKE HARDER:")
                for sug, cnt in sa["top_how_to_make_harder"][:8]:
                    print(f"    [{cnt}x] {sug[:140]}")
            if sa.get("top_what_made_easy"):
                print(f"\n  WHAT MADE EASY:")
                for reason, cnt in sa["top_what_made_easy"][:8]:
                    print(f"    [{cnt}x] {reason[:140]}")

        if fa:
            print(f"\n  ── WHY MODELS FAILED (failure cases) ──")
            print(f"  Root causes:          {fa.get('root_cause_dist', {})}")
            print(f"  Where stuck:          {fa.get('where_stuck_dist', {})}")
            print(f"  Issue was vague:      {fa.get('issue_was_vague_pct', '?')}%")
            print(f"  Required deep understanding: {fa.get('required_deep_understanding_pct', '?')}%")
            if fa.get("top_replicable_patterns"):
                print(f"\n  REPLICABLE HARD PATTERNS:")
                for pat, cnt in fa["top_replicable_patterns"][:8]:
                    print(f"    [{cnt}x] {pat[:140]}")
            if fa.get("top_what_made_hard"):
                print(f"\n  WHAT MADE HARD:")
                for reason, cnt in fa["top_what_made_hard"][:8]:
                    print(f"    [{cnt}x] {reason[:140]}")

        print("=" * 60)


if __name__ == "__main__":
    main()
