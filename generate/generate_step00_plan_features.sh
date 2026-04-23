#!/usr/bin/env bash
# generate_step00_plan_features.sh
#
# Session 0: Use Opus to analyze a repo and generate specific feature directions.
# Runs in agent mode — the LLM reads the repo itself using cat/find/grep.
#
# Usage:
#   ./generate_step00_plan_features.sh <repo_dir> <output_file> [copilot_model]

set -euo pipefail

REPO_DIR="${1:?Usage: $0 <repo_dir> <output_file> [model]}"
OUTPUT_FILE="${2:?Usage: $0 <repo_dir> <output_file> [model]}"
MODEL="${3:-claude-opus-4.6}"
TIMEOUT="${PLAN_TIMEOUT:-600}"  # 10 min — agent needs time to explore

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Session 0: Feature Planning (agent mode) ==="
echo "  Repo:   $REPO_DIR"
echo "  Output: $OUTPUT_FILE"
echo "  Model:  $MODEL"

# Read the prompt template
S0_PROMPT_FILE="${SCRIPT_DIR}/feature_direction_prompt.md"
if [ ! -f "$S0_PROMPT_FILE" ]; then
    echo "ERROR: feature_direction_prompt.md not found at $S0_PROMPT_FILE" >&2
    exit 1
fi

S0_PROMPT=$(cat "$S0_PROMPT_FILE")

echo "=== Calling $MODEL in agent mode (timeout=${TIMEOUT}s) ==="

# Run copilot in agent mode — it reads the repo and writes feature_plan.txt itself
PLAN_EXIT=0
cd "$REPO_DIR"
timeout "$TIMEOUT" copilot \
    -p "$S0_PROMPT" \
    --model "$MODEL" \
    --no-ask-user \
    --allow-all \
    > /dev/null \
    2>"${OUTPUT_FILE}.log" || PLAN_EXIT=$?

if [ "$PLAN_EXIT" -eq 124 ]; then
    echo "ERROR: Planning timed out after ${TIMEOUT}s — exiting" >&2
    exit 1
elif [ "$PLAN_EXIT" -ne 0 ]; then
    echo "ERROR: Planning failed with exit code $PLAN_EXIT" >&2
    cat "${OUTPUT_FILE}.log" >&2 2>/dev/null || true
    exit 1
fi

# The model should have written feature_plan.txt in the repo dir
# Copy it to the expected output path if it's not already there
if [ -f "$REPO_DIR/feature_plan.txt" ] && [ "$REPO_DIR/feature_plan.txt" != "$OUTPUT_FILE" ]; then
    cp "$REPO_DIR/feature_plan.txt" "$OUTPUT_FILE"
fi

if [ ! -s "$OUTPUT_FILE" ]; then
    echo "ERROR: Planning produced empty output" >&2
    exit 1
fi

# Quality checks
DIRECTION_COUNT=$(grep -c 'Direction.*[0-9]\|^##.*Direction\|^\*\*Direction' "$OUTPUT_FILE" 2>/dev/null || echo "0")
CODE_REF_COUNT=$(grep -c '\.py:' "$OUTPUT_FILE" 2>/dev/null || echo "0")

if [ "$DIRECTION_COUNT" -lt 3 ] || [ "$CODE_REF_COUNT" -lt 4 ]; then
    echo "WARN: Plan quality may be low ($DIRECTION_COUNT directions, $CODE_REF_COUNT code refs)" >&2
    { echo "⚠️ QUALITY WARNING: This plan may be incomplete ($DIRECTION_COUNT directions, $CODE_REF_COUNT code refs). Use your own judgment."; echo ""; cat "$OUTPUT_FILE"; } > "${OUTPUT_FILE}.tmp" && mv "${OUTPUT_FILE}.tmp" "$OUTPUT_FILE"
fi

# Clean up log file on success
rm -f "${OUTPUT_FILE}.log" 2>/dev/null || true

PLAN_LINES=$(wc -l < "$OUTPUT_FILE")
echo "=== Feature plan generated: $PLAN_LINES lines, $DIRECTION_COUNT directions, $CODE_REF_COUNT code refs ==="
echo "=== Session 0 complete ==="
