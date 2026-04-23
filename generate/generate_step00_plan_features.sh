#!/usr/bin/env bash
# generate_step00_plan_features.sh
#
# Session 0: Use Opus to analyze a repo and generate specific feature directions.
# This runs BEFORE generate_step01 and produces a feature plan file that
# Session 1 consumes.
#
# Usage:
#   ./generate_step00_plan_features.sh <repo_dir> <output_file> [copilot_model]
#
# Input:
#   repo_dir     — path to the cloned repo
#   output_file  — where to write the feature plan (text)
#   copilot_model — model to use (default: claude-opus-4.6)
#
# Output:
#   Writes a feature plan to output_file. Session 1's prompt will include
#   this as "your colleague's suggestions".

set -euo pipefail

REPO_DIR="${1:?Usage: $0 <repo_dir> <output_file> [model]}"
OUTPUT_FILE="${2:?Usage: $0 <repo_dir> <output_file> [model]}"
MODEL="${3:-claude-opus-4.6}"
TIMEOUT="${PLAN_TIMEOUT:-300}"  # 5 min default — planning is fast

echo "=== Session 0: Feature Planning ==="
echo "  Repo:   $REPO_DIR"
echo "  Output: $OUTPUT_FILE"
echo "  Model:  $MODEL"

# ── Cleanup management ───────────────────────────────────────────────────────
CLEANUP_FILES=()
cleanup() { rm -f "${CLEANUP_FILES[@]}"; }
trap cleanup EXIT

# ── Step 1: Extract repo context (full source) ───────────────────────────────

REPO_CONTEXT=$(mktemp)
CLEANUP_FILES+=("$REPO_CONTEXT")

{
    echo "## Project Structure"
    echo '```'
    find "$REPO_DIR" -maxdepth 4 -name '*.py' \
        -not -path '*/\.*' \
        -not -path '*/node_modules/*' \
        -not -path '*/__pycache__/*' \
        -not -path '*/build/*' \
        -not -path '*/.tox/*' \
        | sed "s|$REPO_DIR/||" | sort
    echo '```'
    echo ""

    # README (first 80 lines)
    for readme in README.md README.rst README.txt README; do
        if [ -f "$REPO_DIR/$readme" ]; then
            echo "## README (first 80 lines)"
            echo '```'
            head -80 "$REPO_DIR/$readme"
            echo '```'
            echo ""
            break
        fi
    done

    # pyproject.toml project section
    if [ -f "$REPO_DIR/pyproject.toml" ]; then
        echo "## pyproject.toml (project section)"
        echo '```'
        sed -n '/^\[project\]/,/^\[/p' "$REPO_DIR/pyproject.toml" | head -30
        echo '```'
        echo ""
    fi

    # Source code (non-test) — separate section
    echo "## __SOURCE_CODE_START__"
    echo "## Source Code (non-test)"
    find "$REPO_DIR" -name '*.py' \
        -not -path '*/\.*' \
        -not -path '*/node_modules/*' \
        -not -path '*/__pycache__/*' \
        -not -path '*/build/*' \
        -not -path '*/.tox/*' \
        -not -path '*/dist/*' \
        -not -path '*/.eggs/*' \
        -not -path '*/tests/*' \
        -not -path '*/test/*' \
        -not -name 'test_*' \
        -not -name '*_test.py' \
        -not -name 'conftest*' \
        | sort | while read -r pyfile; do
        relpath=$(echo "$pyfile" | sed "s|$REPO_DIR/||")
        linecount=$(wc -l < "$pyfile")
        echo "### === $relpath ($linecount lines) ==="
        echo '```python'
        cat "$pyfile"
        echo '```'
        echo ""
    done

    # Test code — separate section
    echo "## Test Code"
    find "$REPO_DIR" -name '*.py' \
        -not -path '*/\.*' \
        -not -path '*/__pycache__/*' \
        -not -path '*/build/*' \
        -not -path '*/.tox/*' \
        \( -path '*/tests/*' -o -path '*/test/*' -o -name 'test_*' -o -name '*_test.py' -o -name 'conftest*' \) \
        | sort | while read -r pyfile; do
        relpath=$(echo "$pyfile" | sed "s|$REPO_DIR/||")
        linecount=$(wc -l < "$pyfile")
        echo "### === $relpath ($linecount lines) ==="
        echo '```python'
        cat "$pyfile"
        echo '```'
        echo ""
    done

} > "$REPO_CONTEXT"

CONTEXT_BYTES=$(wc -c < "$REPO_CONTEXT")
CONTEXT_LINES=$(wc -l < "$REPO_CONTEXT")
# Rough token estimate: 1 token ≈ 4 bytes for code
CONTEXT_TOKENS_EST=$((CONTEXT_BYTES / 4))

echo "  Context extracted: $CONTEXT_LINES lines, $CONTEXT_BYTES bytes (~${CONTEXT_TOKENS_EST} tokens)"

# Size check: if too large for context window, fall back to summary mode
MAX_TOKENS="${PLAN_MAX_CONTEXT_TOKENS:-200000}"
if [ "$CONTEXT_TOKENS_EST" -gt "$MAX_TOKENS" ]; then
    echo "  WARN: Context too large (${CONTEXT_TOKENS_EST} tokens > ${MAX_TOKENS}). Falling back to summary mode."

    REPO_CONTEXT_FULL="$REPO_CONTEXT.full"
    CLEANUP_FILES+=("$REPO_CONTEXT_FULL")
    mv "$REPO_CONTEXT" "$REPO_CONTEXT_FULL"

    {
        # Keep the tree + README + pyproject sections (cut at unique marker)
        sed '/^## __SOURCE_CODE_START__/,$d' "$REPO_CONTEXT_FULL"

        # Summary mode: AST-based signature extraction
        echo "## Source Code (signatures only — project too large for full source)"
        find "$REPO_DIR" -name '*.py' \
            -not -path '*/\.*' \
            -not -path '*/__pycache__/*' \
            -not -path '*/build/*' \
            -not -path '*/.tox/*' \
            | sort | while read -r pyfile; do
            relpath=$(echo "$pyfile" | sed "s|$REPO_DIR/||")
            echo "### === $relpath ==="
            echo '```python'
            python3 -c "
import ast, sys
try:
    tree = ast.parse(open(sys.argv[1]).read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ', '.join(a.arg for a in node.args.args)
            prefix = 'async def' if isinstance(node, ast.AsyncFunctionDef) else 'def'
            print(f'{node.lineno}: {prefix} {node.name}({args})')
        elif isinstance(node, ast.ClassDef):
            bases = ', '.join(getattr(b, 'id', ast.dump(b)) for b in node.bases)
            print(f'{node.lineno}: class {node.name}({bases})')
except Exception:
    pass
" "$pyfile" 2>/dev/null || true
            echo '```'
            echo ""
        done
    } > "$REPO_CONTEXT"

    NEW_BYTES=$(wc -c < "$REPO_CONTEXT")
    echo "  Summary mode: $(wc -l < "$REPO_CONTEXT") lines, $NEW_BYTES bytes (~$((NEW_BYTES / 4)) tokens)"
fi

# ── Step 2: Call Opus for feature planning ────────────────────────────────────

PLAN_PROMPT=$(mktemp)
CLEANUP_FILES+=("$PLAN_PROMPT")

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
S0_PROMPT_FILE="${SCRIPT_DIR}/feature_direction_prompt.md"
if [ ! -f "$S0_PROMPT_FILE" ]; then
    echo "ERROR: feature_direction_prompt.md not found at $S0_PROMPT_FILE" >&2
    exit 1
fi

# Build full prompt: session0_prompt.md + repo context
cat "$S0_PROMPT_FILE" > "$PLAN_PROMPT"

# Append repo context
echo "" >> "$PLAN_PROMPT"
echo "## Project Source Code" >> "$PLAN_PROMPT"
cat "$REPO_CONTEXT" >> "$PLAN_PROMPT"

echo "=== Calling $MODEL for feature planning (timeout=${TIMEOUT}s) ==="

# Pass prompt via $(cat) — same approach as generate_step01.
# Check size first to avoid ARG_MAX (usually 2MB on Linux).
PROMPT_BYTES=$(wc -c < "$PLAN_PROMPT")
if [ "$PROMPT_BYTES" -gt 1800000 ]; then
    echo "ERROR: Prompt too large for -p argument (${PROMPT_BYTES} bytes > 1.8MB ARG_MAX safety limit)" >&2
    echo "  Consider using a smaller repo or increasing PLAN_MAX_CONTEXT_TOKENS" >&2
    exit 1
fi

PLAN_EXIT=0
timeout "$TIMEOUT" copilot \
    -p "$(cat "$PLAN_PROMPT")" \
    --model "$MODEL" \
    --no-ask-user \
    > "$OUTPUT_FILE" \
    2>"${OUTPUT_FILE}.log" || PLAN_EXIT=$?

if [ "$PLAN_EXIT" -eq 124 ]; then
    echo "ERROR: Planning timed out after ${TIMEOUT}s — exiting" >&2
    exit 1
elif [ "$PLAN_EXIT" -ne 0 ]; then
    echo "ERROR: Planning failed with exit code $PLAN_EXIT" >&2
    cat "${OUTPUT_FILE}.log" >&2 2>/dev/null || true
    exit 1
fi

# Verify output is not empty
if [ ! -s "$OUTPUT_FILE" ]; then
    echo "ERROR: Planning produced empty output" >&2
    exit 1
fi

# ── Quality checks ───────────────────────────────────────────────────────────
DIRECTION_COUNT=$(grep -c '方向.*[0-9]\|^##.*方向\|^\*\*方向' "$OUTPUT_FILE" 2>/dev/null || echo "0")
CODE_REF_COUNT=$(grep -c '\.py:' "$OUTPUT_FILE" 2>/dev/null || echo "0")

if [ "$DIRECTION_COUNT" -lt 3 ] || [ "$CODE_REF_COUNT" -lt 4 ]; then
    echo "WARN: Plan quality may be low ($DIRECTION_COUNT directions, $CODE_REF_COUNT code refs)" >&2
    # Prepend warning to output so Session 1 is aware
    { echo "⚠️ QUALITY WARNING: This plan may be incomplete ($DIRECTION_COUNT directions, $CODE_REF_COUNT code refs). Use your own judgment."; echo ""; cat "$OUTPUT_FILE"; } > "${OUTPUT_FILE}.tmp" && mv "${OUTPUT_FILE}.tmp" "$OUTPUT_FILE"
fi

# Clean up log file (keep only on error)
rm -f "${OUTPUT_FILE}.log" 2>/dev/null || true

PLAN_LINES=$(wc -l < "$OUTPUT_FILE")
echo "=== Feature plan generated: $PLAN_LINES lines, $DIRECTION_COUNT directions, $CODE_REF_COUNT code refs ==="
echo "=== Session 0 complete ==="
