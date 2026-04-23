#!/usr/bin/env bash
# generate_step02_classify_and_report.sh
#
# Session 2: Given a repo with a regression (test failure after S1's feature work),
# call an LLM to:
#   1. Classify the bug (category, difficulty, etc.)
#   2. Write a realistic issue_text (as a confused end-user)
#   3. Fill all metadata fields needed by downstream (step04, push, etc.)
#   4. Write case_metadata.json into the repo root
#
# This script does NOT know or care how the bug was introduced.
# It only sees: diff + test failure output.
#
# Usage:
#   ./generate_step02_classify_and_report.sh <work_dir> <repo_slug> <instance_id> \
#       <base_commit> <case_index> [model] [batch_version]

set -euo pipefail

WORK_DIR="${1:?Usage: $0 <work_dir> <repo_slug> <instance_id> <base_commit> <case_index> [model] [batch_version]}"
REPO_SLUG="${2:?}"
INSTANCE_ID="${3:?}"
BASE_COMMIT="${4:?}"
CASE_INDEX="${5:?}"
MODEL="${6:-claude-opus-4.6}"
BATCH_VERSION="${7:-$(date +%Y%m%d)}"
TIMEOUT="${CLASSIFY_TIMEOUT:-300}"

REPO_DIR="$WORK_DIR/repo"
REPO_OWNER=$(echo "$REPO_SLUG" | cut -d'_' -f1)
REPO_NAME=$(echo "$REPO_SLUG" | sed 's/^[^_]*__//' | sed 's/__/\//')

echo "=== Session 2: Classify & Report ==="
echo "  Work dir:    $WORK_DIR"
echo "  Repo:        $REPO_SLUG"
echo "  Instance ID: $INSTANCE_ID"
echo "  Model:       $MODEL"

# ── Gather inputs ────────────────────────────────────────────────────────────

# 1. Git diff
cd "$REPO_DIR"
DIFF=$(git diff HEAD -- '*.py' 2>/dev/null || echo "")
if [ -z "$DIFF" ]; then
    # Try staged
    git add -A -- '*.py' 2>/dev/null || true
    DIFF=$(git diff HEAD -- '*.py' 2>/dev/null || echo "")
fi
DIFF_LINES=$(echo "$DIFF" | wc -l)
echo "  Diff: $DIFF_LINES lines"

# 2. Test failure output (from the buggy test run)
TEST_OUTPUT=""
for logfile in "$WORK_DIR"/native_tests_buggy_attempt_*.log; do
    if [ -f "$logfile" ]; then
        TEST_OUTPUT=$(tail -80 "$logfile")
        break
    fi
done
echo "  Test output: $(echo "$TEST_OUTPUT" | wc -l) lines"

# 3. Fail-to-pass test list
FAIL_TO_PASS=""
for ftpfile in "$WORK_DIR"/fail_to_pass_attempt_*.json; do
    if [ -f "$ftpfile" ]; then
        FAIL_TO_PASS=$(cat "$ftpfile")
        break
    fi
done
echo "  Fail-to-pass: $FAIL_TO_PASS"

# ── Build S2 prompt ──────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
S2_PROMPT_FILE="${SCRIPT_DIR}/issue_and_metadata_prompt.md"
if [ ! -f "$S2_PROMPT_FILE" ]; then
    echo "ERROR: issue_and_metadata_prompt.md not found at $S2_PROMPT_FILE" >&2
    exit 1
fi

S2_PROMPT=$(mktemp)
trap "rm -f $S2_PROMPT" EXIT

# Build full prompt: session2_prompt.md + diff + test output
cat "$S2_PROMPT_FILE" > "$S2_PROMPT"

# Append diff and test output
cat >> "$S2_PROMPT" << S2_DATA_EOF

### 代码变更 (git diff)

\`\`\`diff
${DIFF}
\`\`\`

### 测试失败输出

\`\`\`
${TEST_OUTPUT}
\`\`\`

### 失败的测试列表

${FAIL_TO_PASS}
S2_DATA_EOF

# Check prompt size
PROMPT_BYTES=$(wc -c < "$S2_PROMPT")
echo "  Prompt size: $PROMPT_BYTES bytes"
if [ "$PROMPT_BYTES" -gt 1800000 ]; then
    echo "ERROR: S2 prompt too large ($PROMPT_BYTES bytes)" >&2
    exit 1
fi

# ── Call LLM ─────────────────────────────────────────────────────────────────

S2_OUTPUT="$WORK_DIR/s2_output.txt"
S2_EXIT=0
echo "=== Calling $MODEL for classification (timeout=${TIMEOUT}s) ==="
timeout "$TIMEOUT" copilot \
    -p "$(cat "$S2_PROMPT")" \
    --model "$MODEL" \
    --no-ask-user \
    > "$S2_OUTPUT" \
    2>"$WORK_DIR/s2_log.txt" || S2_EXIT=$?

if [ "$S2_EXIT" -ne 0 ]; then
    echo "ERROR: S2 failed with exit code $S2_EXIT" >&2
    cat "$WORK_DIR/s2_log.txt" >&2 2>/dev/null || true
    exit 1
fi

if [ ! -s "$S2_OUTPUT" ]; then
    echo "ERROR: S2 produced empty output" >&2
    exit 1
fi

# ── Parse output → case_metadata.json ────────────────────────────────────────

python3 -c "
import json, sys, re, os

output = open(sys.argv[1], encoding='utf-8').read()
work_dir = sys.argv[2]
instance_id = sys.argv[3]
repo_slug = sys.argv[4]
base_commit = sys.argv[5]
case_index = sys.argv[6]
batch_version = sys.argv[7]

# Extract issue_text
issue_match = re.search(r'ISSUE_START\s*\n(.*?)ISSUE_END', output, re.DOTALL)
issue_text = issue_match.group(1).strip() if issue_match else ''

# Extract metadata JSON
meta_match = re.search(r'METADATA_START\s*\n(.*?)METADATA_END', output, re.DOTALL)
metadata = {}
if meta_match:
    raw = meta_match.group(1).strip()
    # Remove markdown code fences if present
    raw = re.sub(r'^\`\`\`json?\s*\n?', '', raw)
    raw = re.sub(r'\n?\`\`\`\s*$', '', raw)
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f'WARN: Failed to parse metadata JSON: {e}', file=sys.stderr)

# Read fail_to_pass and pass_to_pass from pipeline files
fail_to_pass = []
pass_to_pass = []
import glob
for f in sorted(glob.glob(os.path.join(work_dir, 'fail_to_pass_attempt_*.json'))):
    fail_to_pass = json.load(open(f, encoding='utf-8'))
    break
for f in sorted(glob.glob(os.path.join(work_dir, 'pass_to_pass_attempt_*.json'))):
    pass_to_pass = json.load(open(f, encoding='utf-8'))
    break

# Read patch from git diff
patch = ''
diff_path = os.path.join(work_dir, 'patch.diff')
if os.path.exists(diff_path):
    patch = open(diff_path, encoding='utf-8').read()

# Read feature_direction from Session 0 output
feature_direction = ''
feature_plan_path = os.path.join(work_dir, 'feature_plan.txt')
if os.path.isfile(feature_plan_path):
    feature_direction = open(feature_plan_path, encoding='utf-8').read().strip()

# Build test_command from fail_to_pass
test_cmd = ''
if fail_to_pass:
    test_cmd = 'python3 -m pytest ' + ' '.join(fail_to_pass[:5]) + ' -xvs'

# Build case_metadata.json
repo_name = repo_slug.replace('__', '/')
case = {
    'instance_id': instance_id,
    'repo': repo_name,
    'base_commit': base_commit,
    'source': 'synthetic_mutation',
    'setup_command': metadata.get('setup_command', 'pip install -e .'),
    'test_command': test_cmd,
    'issue_text': issue_text,
    'hints_text': '',
    'mutation_file': metadata.get('mutation_file', ''),
    'mutation_description': metadata.get('mutation_description', ''),
    'fail_to_pass': fail_to_pass,
    'pass_to_pass': pass_to_pass,
    'category': metadata.get('category', ''),
    'sub_type': metadata.get('sub_type', ''),
    'difficulty': metadata.get('difficulty', 'L2'),
    'localization': 'implicit',
    'context_dependency': 'cross_module',
    'test_modality': 'unit_test',
    'feature_keyword': metadata.get('feature_keyword', ''),
    'feature_direction': feature_direction,
    'mutation_type': metadata.get('category', ''),
    'repo_description': metadata.get('repo_description', ''),
    'feature_description': metadata.get('feature_description', ''),
    'bug_description': metadata.get('bug_description', ''),
    'batch_version': batch_version,
    'labels': {
        'category': metadata.get('category', ''),
        'sub_type': metadata.get('sub_type', ''),
        'difficulty': metadata.get('difficulty', 'L2'),
    },
}

# Write to repo root
out_path = os.path.join(work_dir, 'repo', 'case_metadata.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(case, f, indent=2, ensure_ascii=False)

print(f'Wrote case_metadata.json: {len(issue_text)} chars issue, {len(fail_to_pass)} fail_to_pass')
print(f'  feature_keyword: {case[\"feature_keyword\"]}')
print(f'  category: {case[\"category\"]}')
print(f'  difficulty: {case[\"difficulty\"]}')
" "$S2_OUTPUT" "$WORK_DIR" "$INSTANCE_ID" "$REPO_SLUG" "$BASE_COMMIT" "$CASE_INDEX" "$BATCH_VERSION"

# Clean up
rm -f "$WORK_DIR/s2_log.txt" 2>/dev/null || true

echo "=== Session 2 complete ==="
