#!/usr/bin/env python3
"""
evaluate_quality.py — AI critic pipeline for dataset quality evaluation.

For each feature-induced bug sample, evaluates whether it is worth
including in the dataset by scoring 8 quality dimensions (0-2 each),
applying hard filter rules, and producing a structured verdict
(accept / review / reject).

Usage:
  python3 evaluate_quality.py \
    --tar-dir  /path/to/tar.gz/ \
    --jsonl-dir /path/to/jsonl/ \
    --output-dir /path/to/quality_results/ \
    --model claude-opus-4.6 \
    --github-token <token>
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

# reuse helpers from the difficulty/verification script
sys.path.insert(0, os.path.dirname(__file__))
from evaluate_difficulty import (
    find_matching_pairs, parse_jsonl, safe_extract_tar
)

PROMPT_VERSION = "quality-v1"

# Dimension weights: feature_naturalness and issue_alignment get 1.5x
DIMENSION_WEIGHTS = {
    "causality": 1.0,
    "reproducibility": 1.0,
    "issue_alignment": 1.5,
    "feature_naturalness": 1.5,
    "debug_value": 1.0,
    "test_validity": 1.0,
    "localizability": 1.0,
    "information_completeness": 1.0,
}

QUALITY_PROMPT_TEMPLATE = """\
You are a dataset quality auditor for a benchmark of feature-induced bugs. \
Your task is to evaluate whether this sample is suitable for inclusion in \
a training / evaluation dataset for AI coding agents.

You are NOT fixing the bug. You are judging the QUALITY of this sample.

## Input

You are given:
- An issue description (the bug report)
- A feature patch (the code change that introduced the bug)
- Failing tests (tests that fail because of the bug)
- The codebase (available in the current working directory)

## Step 1: Hard Filter Rules

First, check the following hard rejection rules. If ANY is true, the sample \
must be rejected regardless of scores.

| Rule | Description |
|------|-------------|
| `non_reproducible` | The failing tests are flaky or depend on timing / randomness / external state. |
| `test_issue_mismatch` | The failing tests check something unrelated to the issue description. |
| `obviously_injected_bug` | The patch looks like intentional sabotage rather than a natural feature addition. |
| `no_clear_failure_signal` | Tests fail but the failure cannot be stably mapped to a specific bug in the patch. |
| `insufficient_context` | The issue is too vague, the patch is too large, or there are too few tests to form a complete task. |

## Step 2: Score 8 Dimensions (0-2 each)

Rate each dimension on a scale of 0-2:
- 0 = poor / not met
- 1 = acceptable / partially met
- 2 = good / fully met

**IMPORTANT**: If you lack sufficient information to judge a dimension, \
score it LOW (0 or 1). Do NOT guess or extrapolate beyond what is given.

### 1. causality
Does the feature patch clearly and directly cause the failing tests to fail?
- 0: No causal link visible
- 1: Plausible but indirect connection
- 2: Clear and direct causal chain from patch to failure

### 2. reproducibility
Is the bug stable and deterministically reproducible?
- 0: Flaky, timing-dependent, or environment-specific
- 1: Mostly reproducible but with minor caveats
- 2: Fully deterministic and stable

### 3. issue_alignment
Do the failing tests genuinely correspond to the expected behavior described in the issue?
- 0: Tests and issue are about different things
- 1: Partial overlap; some tests are relevant
- 2: Tests directly validate the issue's expected behavior

### 4. feature_naturalness
Does the bug look like a natural side-effect of adding a feature, NOT an artificially injected defect?
- 0: Looks deliberately broken or contrived
- 1: Somewhat natural but with suspicious patterns
- 2: Clearly a realistic regression from feature development

### 5. debug_value
Does this sample provide real debugging value (not a trivial one-line obvious fix)?
- 0: Trivially obvious fix (e.g., typo, missing import)
- 1: Requires some reasoning but straightforward
- 2: Requires meaningful debugging and understanding

### 6. test_validity
Are the tests stable, reasonable, and not dependent on coincidental factors?
- 0: Tests are brittle, over-specific, or unreliable
- 1: Tests are acceptable but could be better
- 2: Tests are well-written, stable, and meaningful

### 7. localizability
Can the bug be reasonably localized to the patch-related code area?
- 0: Bug location is unrelated to the patch
- 1: Some connection but requires significant exploration
- 2: Bug clearly localizable to the changed code

### 8. information_completeness
Do the issue + patch + tests form a complete, self-contained task?
- 0: Missing critical information; cannot form a viable task
- 1: Mostly complete but some gaps
- 2: Fully self-contained with clear inputs, expected behavior, and validation

## Evidence Requirements

For EACH dimension and EACH hard filter rule, you MUST cite the evidence \
source. Every rationale must explicitly reference one of:
- `issue` — from the issue description
- `patch` — from the feature patch diff
- `test` — from the failing test code
- `codebase` — from the surrounding source code

If you cannot find evidence, say so and score low.

---

## Sample Information

**Instance ID:** {instance_id}
**Repository:** {repo}
**Category:** {category}
**Sub-type:** {sub_type}

### Issue Description

{issue_text}

{hints_section}

### Feature Patch

```diff
{patch_text}
```

### Failing Tests

**fail_to_pass:** {fail_to_pass}

{test_command_section}

### Codebase Context

The full source code is available in the current working directory. \
Examine the codebase to understand the structure and validate your assessment.

---

## Output Requirements

You MUST create a file called `quality.json` in the repository root with \
EXACTLY this JSON structure. Do NOT deviate from this schema.

```json
{{
  "instance_id": "{instance_id}",
  "hard_filters": {{
    "non_reproducible": {{ "triggered": <true|false>, "rationale": "<one sentence>", "evidence_source": "<issue|patch|test|codebase>" }},
    "test_issue_mismatch": {{ "triggered": <true|false>, "rationale": "<one sentence>", "evidence_source": "<issue|patch|test|codebase>" }},
    "obviously_injected_bug": {{ "triggered": <true|false>, "rationale": "<one sentence>", "evidence_source": "<issue|patch|test|codebase>" }},
    "no_clear_failure_signal": {{ "triggered": <true|false>, "rationale": "<one sentence>", "evidence_source": "<issue|patch|test|codebase>" }},
    "insufficient_context": {{ "triggered": <true|false>, "rationale": "<one sentence>", "evidence_source": "<issue|patch|test|codebase>" }}
  }},
  "dimensions": {{
    "causality": {{ "score": <0-2>, "rationale": "<one sentence>", "evidence_source": "<issue|patch|test|codebase>" }},
    "reproducibility": {{ "score": <0-2>, "rationale": "<one sentence>", "evidence_source": "<issue|patch|test|codebase>" }},
    "issue_alignment": {{ "score": <0-2>, "rationale": "<one sentence>", "evidence_source": "<issue|patch|test|codebase>" }},
    "feature_naturalness": {{ "score": <0-2>, "rationale": "<one sentence>", "evidence_source": "<issue|patch|test|codebase>" }},
    "debug_value": {{ "score": <0-2>, "rationale": "<one sentence>", "evidence_source": "<issue|patch|test|codebase>" }},
    "test_validity": {{ "score": <0-2>, "rationale": "<one sentence>", "evidence_source": "<issue|patch|test|codebase>" }},
    "localizability": {{ "score": <0-2>, "rationale": "<one sentence>", "evidence_source": "<issue|patch|test|codebase>" }},
    "information_completeness": {{ "score": <0-2>, "rationale": "<one sentence>", "evidence_source": "<issue|patch|test|codebase>" }}
  }},
  "verdict": {{
    "decision": "<accept|review|reject>",
    "raw_total": <0-16>,
    "weighted_total": <float>,
    "rejection_reasons": ["<reason1>", "..."],
    "summary": "<2-3 sentence overall justification>"
  }}
}}
```

## Decision Rules (you MUST follow these exactly)

1. If ANY hard filter is triggered → `reject`
2. If raw_total >= 13 AND feature_naturalness >= 2 AND issue_alignment >= 2 → `accept`
3. If raw_total is 10-12 → `review`
4. Otherwise → `reject`

`weighted_total` = sum of (score * weight) for each dimension, where weights are:
  causality=1.0, reproducibility=1.0, issue_alignment=1.5, \
feature_naturalness=1.5, debug_value=1.0, test_validity=1.0, \
localizability=1.0, information_completeness=1.0

`rejection_reasons` should list any triggered hard filters and/or low-scoring \
dimensions (score=0). Leave empty list if accepting.

Do NOT fix the issue. Do NOT create or modify any source files. \
Only create `quality.json`.
"""


def build_quality_prompt(case_data: dict) -> str:
    """Build the quality evaluation prompt from case metadata."""
    hints = case_data.get("hints_text", "")
    hints_section = f"### Hints\n\n{hints}" if hints else ""

    patch_text = case_data.get("patch", case_data.get("gold_patch", "N/A"))
    fail_to_pass = case_data.get("fail_to_pass", [])
    if isinstance(fail_to_pass, str):
        fail_to_pass = json.loads(fail_to_pass)

    test_cmd = case_data.get("test_command", "")
    test_command_section = f"**Test command:** `{test_cmd}`" if test_cmd else ""

    return QUALITY_PROMPT_TEMPLATE.format(
        repo=case_data.get("repo", "unknown"),
        category=case_data.get("labels", {}).get("category", "unknown"),
        sub_type=case_data.get("labels", {}).get("sub_type", "unknown"),
        issue_text=case_data["issue_text"],
        hints_section=hints_section,
        instance_id=case_data["instance_id"],
        patch_text=patch_text,
        fail_to_pass=json.dumps(fail_to_pass),
        test_command_section=test_command_section,
    )


def compute_verdict(evaluation: dict) -> dict:
    """Re-compute the verdict from raw evaluation to enforce consistent rules."""
    dims = evaluation.get("dimensions", {})
    hard = evaluation.get("hard_filters", {})

    # Check hard filters
    hard_rejections = [
        name for name, val in hard.items()
        if val.get("triggered", False)
    ]

    # Compute scores
    raw_total = sum(
        dims.get(d, {}).get("score", 0) for d in DIMENSION_WEIGHTS
    )
    weighted_total = sum(
        dims.get(d, {}).get("score", 0) * w
        for d, w in DIMENSION_WEIGHTS.items()
    )

    fn_score = dims.get("feature_naturalness", {}).get("score", 0)
    ia_score = dims.get("issue_alignment", {}).get("score", 0)

    # Zero-score dimensions
    zero_dims = [
        d for d in DIMENSION_WEIGHTS
        if dims.get(d, {}).get("score", 0) == 0
    ]

    # Decision rules
    rejection_reasons = []
    if hard_rejections:
        decision = "reject"
        rejection_reasons.extend(f"hard_filter:{r}" for r in hard_rejections)
    elif raw_total >= 13 and fn_score >= 2 and ia_score >= 2:
        decision = "accept"
    elif 10 <= raw_total <= 12:
        decision = "review"
    else:
        decision = "reject"
        if raw_total < 10:
            rejection_reasons.append(f"low_total_score:{raw_total}")

    if zero_dims:
        rejection_reasons.extend(f"zero_score:{d}" for d in zero_dims)

    return {
        "decision": decision,
        "raw_total": raw_total,
        "weighted_total": round(weighted_total, 2),
        "rejection_reasons": rejection_reasons,
    }


def evaluate_one(case_data: dict, tar_path: str, model: str,
                 github_token: str, run_id: str) -> dict:
    """Run the AI critic on a single case and return structured results."""
    instance_id = case_data["instance_id"]
    result = {
        "instance_id": instance_id,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "run_id": run_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "evaluation": None,
        "verdict": None,
        "error": None,
        "existing_labels": case_data.get("labels", {}),
    }

    work_dir = tempfile.mkdtemp(prefix=f"qual_{instance_id}_")
    t0 = time.time()

    try:
        # Extract
        safe_extract_tar(tar_path, work_dir)
        entries = os.listdir(work_dir)
        if len(entries) == 1 and os.path.isdir(os.path.join(work_dir, entries[0])):
            repo_dir = os.path.join(work_dir, entries[0])
        else:
            repo_dir = work_dir

        # Init git (copilot needs it)
        for cmd in [
            ["git", "init", "-q"],
            ["git", "config", "user.email", "eval@local"],
            ["git", "config", "user.name", "Eval"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "init"],
        ]:
            subprocess.run(cmd, cwd=repo_dir, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Run copilot
        prompt = build_quality_prompt(case_data)
        env = os.environ.copy()
        env["GITHUB_TOKEN"] = github_token

        cp = subprocess.run(
            [
                "copilot", "-p", prompt,
                "--allow-all",
                "--no-ask-user",
                "--model", model,
            ],
            cwd=repo_dir, env=env,
            capture_output=True, text=True, timeout=300,
        )

        if cp.returncode != 0:
            result["error"] = f"copilot exit {cp.returncode}"

        # Read output
        quality_json = os.path.join(repo_dir, "quality.json")
        if os.path.exists(quality_json):
            with open(quality_json) as f:
                evaluation = json.load(f)
            result["evaluation"] = evaluation
            # Re-compute verdict server-side to enforce consistent rules
            result["verdict"] = compute_verdict(evaluation)
        else:
            result["error"] = "quality.json not created"

    except subprocess.TimeoutExpired:
        result["error"] = "timeout"
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        result["duration_seconds"] = round(time.time() - t0, 2)
        shutil.rmtree(work_dir, ignore_errors=True)

    return result


def main():
    ap = argparse.ArgumentParser(
        description="AI critic pipeline for dataset quality evaluation"
    )
    ap.add_argument("--tar-dir", required=True)
    ap.add_argument("--jsonl-dir", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--model", default="claude-opus-4.6")
    ap.add_argument("--github-token", default=None)
    ap.add_argument("--filter", default=None,
                    help="Regex to filter case names")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip cases that already have results in output-dir")
    args = ap.parse_args()

    token = args.github_token or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("ERROR: set GITHUB_TOKEN or pass --github-token")

    pairs = find_matching_pairs(args.tar_dir, args.jsonl_dir)
    if not pairs:
        sys.exit("ERROR: no matching pairs found")

    if args.filter:
        pat = re.compile(args.filter)
        pairs = [p for p in pairs if pat.search(p["name"])]

    os.makedirs(args.output_dir, exist_ok=True)
    results_path = os.path.join(args.output_dir, "quality_results.jsonl")
    run_id = uuid.uuid4().hex[:12]

    # Load existing results for --skip-existing
    existing_ids = set()
    if args.skip_existing and os.path.exists(results_path):
        with open(results_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                existing_ids.add(rec["instance_id"])
        if existing_ids:
            print(f"Skip-existing: found {len(existing_ids)} existing "
                  f"result(s) in quality_results.jsonl")

    print(f"Cases: {len(pairs)}  Model: {args.model}  Run: {run_id}\n")

    accept_count = review_count = reject_count = error_count = 0

    for idx, pair in enumerate(pairs, 1):
        case = parse_jsonl(pair["jsonl_path"])
        iid = case["instance_id"]

        print(f"[{idx}/{len(pairs)}] {iid}")

        if iid in existing_ids:
            print(f"  => SKIPPED (already in results)\n")
            continue

        result = evaluate_one(case, pair["tar_path"], args.model, token, run_id)

        with open(results_path, "a") as fh:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")

        if result["verdict"]:
            v = result["verdict"]
            decision = v["decision"]
            print(f"  => {decision.upper()}  "
                  f"(raw={v['raw_total']}/16  weighted={v['weighted_total']})  "
                  f"[{result['duration_seconds']}s]")
            if v["rejection_reasons"]:
                print(f"     reasons: {', '.join(v['rejection_reasons'])}")
            if decision == "accept":
                accept_count += 1
            elif decision == "review":
                review_count += 1
            else:
                reject_count += 1
        else:
            print(f"  => ERROR: {result['error']}")
            error_count += 1
        print()

    # Summary
    total = accept_count + review_count + reject_count + error_count
    print("=" * 60)
    print(f"Quality Evaluation Summary  (run={run_id})")
    print(f"  Total:   {total}")
    print(f"  Accept:  {accept_count}")
    print(f"  Review:  {review_count}")
    print(f"  Reject:  {reject_count}")
    print(f"  Errors:  {error_count}")
    print(f"Results → {results_path}")


if __name__ == "__main__":
    main()
