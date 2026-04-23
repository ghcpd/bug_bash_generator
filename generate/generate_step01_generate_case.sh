#!/bin/bash
# ============================================================================
# generate_step01_generate_case.sh — Generate ONE SWE-bench case via Copilot CLI
# ============================================================================
# Usage (called by ADF Custom Activity — one Batch node per case):
#   bash generate/generate_step01_generate_case.sh <task_json> <github_token> <prompt_path> <output_base> [batch_version]
#
# Arguments:
#   task_json     — Base64-encoded JSON, e.g. {"repo":"https://...","case_index":0,"category":"Logic & Algorithm","difficulty":"L1"}
#   github_token  — GitHub Token for Copilot CLI auth
#   prompt_path   — Full path to prompt file on mounted storage (falls back to built-in default_prompt.md)
#   output_base   — Output root directory (mounted storage path)
#   batch_version — (optional) Batch version label. Any string. Defaults to today's date (YYYYMMDD)
#
# Architecture:
#   Each case = one ForEach item = one Batch node = one gh copilot instance.
#   Parallelism is controlled by ADF batchCount (= number of Batch nodes).
#   No in-process parallelism — avoids gh copilot single-instance conflicts.
#
# Environment variables (optional):
#   DEPS_IMAGE_OVERRIDE — Full image reference (e.g. myacr.azurecr.io/bugbash-deps:tag).
#                         When set, the script pulls this image instead of building locally.
# ============================================================================
set -ex

# Ensure ~/.local/bin is in PATH — pip install --user puts executables there
export PATH="$HOME/.local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

TASK_JSON=$(echo "$1" | base64 -d)
GITHUB_TOKEN="$2"
PROMPT_PATH="$3"
OUTPUT_BASE="$4"
BATCH_VERSION="${5:-$(date +%Y%m%d)}"

echo "=== Batch version: ${BATCH_VERSION} ==="

# ── Parse task JSON with python3 (no jq dependency) ─────────────────────────
eval "$(python3 -c "
import json, sys
t = json.loads(sys.argv[1])
print(f'REPO_URL={chr(34)}{t[\"repo\"]}{chr(34)}')
print(f'CASE_INDEX={t.get(\"case_index\", 0)}')
print(f'CATEGORY={chr(34)}{t.get(\"category\", \"\")}{chr(34)}')
print(f'DIFFICULTY={chr(34)}{t.get(\"difficulty\", \"\")}{chr(34)}')
" "$TASK_JSON")"

export GH_TOKEN="$GITHUB_TOKEN"

# ── Fail-fast: configurable timeout (seconds) for gh copilot calls ────────────
COPILOT_TIMEOUT="${COPILOT_TIMEOUT:-900}"  # 15 minutes default

# ── Detect Python binary (prefer one that has pip) ──────────────────────────
PY=""
for candidate in python3.11 python3.12 python3 python; do
  if command -v "$candidate" &>/dev/null; then
    if "$candidate" -m pip --version &>/dev/null; then
      PY="$candidate"
      echo "Selected $PY (has pip) — $($PY --version 2>&1)"
      break
    fi
  fi
done
# Fallback: use whatever python3 is available even without pip
if [ -z "$PY" ]; then
  if command -v python3 &>/dev/null; then PY=python3
  elif command -v python &>/dev/null; then PY=python
  else echo "ERROR: No python found" >&2; exit 1; fi
  echo "Using Python (no pip yet): $PY ($($PY --version 2>&1))"
fi

# NOTE: pip/pytest are installed inside the Docker image, not on the host.
# Host only needs Python for orchestration scripts (generate_step02..04).

# ── Preflight: verify GitHub auth BEFORE doing any real work ─────────────────
echo "=== Preflight: checking GitHub auth ==="
if ! timeout 30 gh auth status 2>&1; then
    echo "ERROR: GitHub auth failed — GH_TOKEN is invalid or expired. Failing fast." >&2
    exit 1
fi
echo "=== Preflight: auth OK ==="

# ── Prompt file: prefer provided path, fall back to built-in default_prompt.md
if [ -f "$PROMPT_PATH" ]; then
    PROMPT_PATH="$(cd "$(dirname "$PROMPT_PATH")" && pwd)/$(basename "$PROMPT_PATH")"
    echo "Using external prompt: $PROMPT_PATH"
elif [ -f "${SCRIPT_DIR}/default_prompt.md" ]; then
    echo "WARN: Prompt file not found at '$PROMPT_PATH', using built-in default_prompt.md"
    PROMPT_PATH="${SCRIPT_DIR}/default_prompt.md"
else
    echo "ERROR: No prompt file found" >&2
    exit 1
fi

# ── Output directories ───────────────────────────────────────────────────────
TARGZ_DIR="${OUTPUT_BASE}/tar.gz"
JSONL_DIR="${OUTPUT_BASE}/jsonl"
METRICS_DIR="${OUTPUT_BASE}/metrics"
IMAGE_CACHE_DIR="${OUTPUT_BASE}/repo_images"
mkdir -p "$TARGZ_DIR" "$JSONL_DIR" "$METRICS_DIR" "$IMAGE_CACHE_DIR"

# ── Temporary workspace ──────────────────────────────────────────────────────
WORK_DIR=$(mktemp -d)
cleanup() {
    # Docker runs as root → .pytest_cache / __pycache__ are root-owned.
    # Use a container to remove them before host-side rm.
    if [ -n "${DEPS_IMAGE:-}" ] && command -v docker &>/dev/null; then
        docker run --rm -v "$WORK_DIR:$WORK_DIR" -w "$WORK_DIR" "${DEPS_IMAGE}" \
            find "$WORK_DIR" -mindepth 1 -delete 2>/dev/null || true
    fi
    rm -rf "$WORK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

now_ms() {
    python3 -c 'import time; print(int(time.time() * 1000))'
}

pass_to_pass_fail() {
    local detail="$1"
    local message="PASS_TO_PASS_FAILED: ${detail}. No original tests found, or original tests failed to stay passing."
    echo "$message" >&2
    echo "$message"
    exit 1
}

cat > "$WORK_DIR/pytest_recorder.py" <<'PYTEST_RECORDER_EOF'
import json
import sys

import pytest


class Recorder:
    def __init__(self):
        self.collected = []
        self.passed = []
        self.failed = []
        self.skipped = []
        self.errors = []

    def pytest_collection_modifyitems(self, session, config, items):
        self.collected = [item.nodeid for item in items]

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            if report.outcome == "passed":
                self.passed.append(report.nodeid)
            elif report.outcome == "failed":
                self.failed.append(report.nodeid)
            elif report.outcome == "skipped":
                self.skipped.append(report.nodeid)
        elif report.when == "setup" and report.failed:
            self.errors.append(report.nodeid)


def _unique(values):
    return sorted(dict.fromkeys(values))


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: pytest_recorder.py <output_json> [pytest args...]", file=sys.stderr)
        return 2

    output_json = sys.argv[1]
    pytest_args = sys.argv[2:]
    recorder = Recorder()
    exit_code = pytest.main(pytest_args, plugins=[recorder])
    if hasattr(exit_code, "value"):
        exit_code = exit_code.value

    payload = {
        "collected": _unique(recorder.collected),
        "passed": _unique(recorder.passed),
        "failed": _unique(recorder.failed),
        "skipped": _unique(recorder.skipped),
        "errors": _unique(recorder.errors),
        "exit_code": int(exit_code),
    }
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
PYTEST_RECORDER_EOF

# ── Parse repo info ──────────────────────────────────────────────────────────
REPO_OWNER=$(echo "$REPO_URL" | sed -E 's|.*github\.com[/:]([^/]+)/([^/.]+).*|\1|')
REPO_NAME=$(echo "$REPO_URL" | sed -E 's|.*github\.com[/:]([^/]+)/([^/.]+).*|\2|')
REPO_SLUG="${REPO_OWNER}__${REPO_NAME}"

# ── Resume: check if this case already exists ────────────────────────────────
# Clean up any .tmp files left by a previously killed process
find "$TARGZ_DIR" "$JSONL_DIR" -name "*.tmp" -delete 2>/dev/null || true

# Check for existing JSONL files for this repo+case_index — auto-increment if exists
ORIGINAL_CASE_INDEX="$CASE_INDEX"
while true; do
    EXISTING_JSONL=$(python3 -c "
import json, sys, glob
repo_slug = sys.argv[1]
case_idx = int(sys.argv[2])
jsonl_dir = sys.argv[3]
for f in sorted(glob.glob(jsonl_dir + '/gen-case__*.jsonl') + glob.glob(jsonl_dir + '/feature-*.jsonl')):
    try:
        with open(f) as fh:
            d = json.loads(fh.readline())
        r = d.get('repo', '').replace('/', '__')
        ci = d.get('labels', {}).get('case_index', -1)
        if ci == -1:
            # fallback: extract case_index from instance_id if available
            iid = d.get('instance_id', '')
            parts = iid.rsplit('-', 1)
            try: ci = int(parts[-1])
            except: ci = -1
        else:
            ci = int(ci)
        if r == repo_slug and ci == case_idx:
            print(f)
            break
    except: pass
" "$REPO_SLUG" "$CASE_INDEX" "$JSONL_DIR" 2>/dev/null)

    if [ -n "$EXISTING_JSONL" ]; then
        EXISTING_ID=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.loads(f.readline())
    print(d.get('instance_id', ''))
except Exception:
    print('')
" "$EXISTING_JSONL" 2>/dev/null)
        EXISTING_TARGZ="${TARGZ_DIR}/${EXISTING_ID}.tar.gz"
        if [ -n "$EXISTING_ID" ] && [ -f "$EXISTING_TARGZ" ]; then
            echo "=== Case ${CASE_INDEX} already exists for ${REPO_SLUG} (${EXISTING_ID}), incrementing ==="
            CASE_INDEX=$((CASE_INDEX + 1))
            continue
        else
            echo "WARN: Removing incomplete/corrupted case: $EXISTING_JSONL"
            rm -f "$EXISTING_JSONL" "$EXISTING_TARGZ"
            break
        fi
    else
        break
    fi
done
if [ "$CASE_INDEX" -ne "$ORIGINAL_CASE_INDEX" ]; then
    echo "=== Auto-incremented case index: ${ORIGINAL_CASE_INDEX} → ${CASE_INDEX} ==="
fi

# Collect previously generated mutation files for diversity hints
PREV_MUTATIONS=""
for PREV_JSONL in $(find "$JSONL_DIR" \( -name "gen-case__*.jsonl" -o -name "feature-*.jsonl" \) 2>/dev/null | sort); do
    MUT_FILE=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.loads(f.readline())
    print(d.get('mutation_file', ''))
except Exception:
    print('')
" "$PREV_JSONL" 2>/dev/null)
    if [ -n "$MUT_FILE" ]; then
        PREV_MUTATIONS="${PREV_MUTATIONS}- ${MUT_FILE}\n"
    fi
done

# ── Clone repository ─────────────────────────────────────────────────────────
git clone --depth 1 "$REPO_URL" "$WORK_DIR/repo"
cd "$WORK_DIR/repo"
BASE_COMMIT=$(git rev-parse HEAD)

# Collect key files to inject repo structure into the prompt
REPO_TREE=$(find . -maxdepth 3 -type f \( -name '*.py' -o -name 'pyproject.toml' -o -name 'setup.py' -o -name 'setup.cfg' -o -name 'Makefile' -o -name 'requirements*.txt' -o -name 'tox.ini' \) | head -60 | sort)

# ── Assign feature direction per case_index for diversity ─────────────────────
# Scan source modules (exclude tests, __init__, setup, conftest) and assign one per case_index.
# This prevents parallel Batch nodes from converging on the same code area.
FEATURE_TARGET=$(python3 -c "
import sys, os, glob

case_idx = int(sys.argv[1])

# Collect non-trivial source .py files
candidates = []
for root, dirs, files in os.walk('.'):
    # Skip hidden dirs, test dirs, build dirs
    dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('tests', 'test', '__pycache__', '.tox', '.eggs', 'build', 'dist', 'docs')]
    for f in files:
        if not f.endswith('.py'):
            continue
        if f in ('__init__.py', 'setup.py', 'conftest.py', 'noxfile.py'):
            continue
        if f.startswith('test_') or f.endswith('_test.py'):
            continue
        path = os.path.join(root, f)
        # Only include files with meaningful content (>20 lines)
        try:
            lines = sum(1 for _ in open(path, encoding='utf-8', errors='ignore'))
            if lines >= 20:
                candidates.append(path.lstrip('./'))
        except:
            pass

candidates.sort()
if not candidates:
    print('')
    sys.exit(0)

# Round-robin assignment
target = candidates[case_idx % len(candidates)]
print(target)
" "$CASE_INDEX" 2>/dev/null)

if [ -n "$FEATURE_TARGET" ]; then
    echo "=== Feature direction: case ${CASE_INDEX} → ${FEATURE_TARGET} ==="
else
    echo "=== Feature direction: no suitable target found, agent will choose freely ==="
fi

# Install dependencies (runtime + test) — Docker image
# Resolution order:
#   1. Cached image tar in repo_images → docker load
#   2. No cache → generate Dockerfile, build locally
LOCAL_IMAGE_TAG="bugbash-deps-${REPO_SLUG,,}"
IMAGE_CACHE_TAR="${IMAGE_CACHE_DIR}/${REPO_OWNER}/${REPO_NAME}/deps.tar"
IMAGE_BUILT_NEW=false

if [ -f "$IMAGE_CACHE_TAR" ]; then
    echo "=== Cached image found: ${IMAGE_CACHE_TAR} ==="
    docker load -i "$IMAGE_CACHE_TAR" 2>&1
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to load cached image from ${IMAGE_CACHE_TAR}" >&2
        exit 1
    fi
    DEPS_IMAGE="$LOCAL_IMAGE_TAG"
    echo "=== Loaded cached image: ${DEPS_IMAGE} ==="
else
    echo "=== No cached image for ${REPO_OWNER}/${REPO_NAME}, building from scratch ==="
    DEPS_DOCKERFILE="$WORK_DIR/repo/Dockerfile.deps"

    $PY "$SCRIPT_DIR/generate_deps_dockerfile.py" \
        --repo-dir "$WORK_DIR/repo" \
        --output "$DEPS_DOCKERFILE"

    DEPS_IMAGE="$LOCAL_IMAGE_TAG"
    echo "=== Building dependency image: ${DEPS_IMAGE} ==="
    docker build -f "$DEPS_DOCKERFILE" -t "$DEPS_IMAGE" "$WORK_DIR/repo" 2>&1
    if [ $? -ne 0 ]; then
        echo "ERROR: Docker build failed — cannot proceed without container" >&2
        exit 1
    fi
    IMAGE_BUILT_NEW=true
fi
echo "=== Dependency image ready: ${DEPS_IMAGE} ==="

# Python binary inside the Docker container (always python3, regardless of host $PY)
CONTAINER_PY="python3"

# Helper: inline docker run with repo mounted at /repo (matching the editable install path in the image)
# Usage: docker run --rm -v "$WORK_DIR/repo:/repo" -v "$WORK_DIR:$WORK_DIR" -w /repo "$DEPS_IMAGE" <cmd>

# ── Re-install editable package into the mounted volume ──────────────────────
# The Docker image built _version.py (setuptools_scm etc.) inside its own /repo,
# but we mount $WORK_DIR/repo over /repo at runtime, losing those generated files.
# Run a one-time editable install so generated files land on the host volume.
echo "=== Re-installing editable package into mounted volume ==="
docker run --rm \
    -v "$WORK_DIR/repo:/repo" -v "$WORK_DIR:$WORK_DIR" -w /repo "$DEPS_IMAGE" \
    bash -c "$CONTAINER_PY -m pip install -e . --no-deps 2>/dev/null \
        || SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 $CONTAINER_PY -m pip install -e . --no-deps 2>/dev/null \
        || true" \
    > /dev/null 2>&1
echo "=== Test dependency installation complete ==="

# ── Baseline: require original tests and verify clean pass-to-pass baseline ──
NATIVE_TESTS_ENABLED="${NATIVE_TESTS_ENABLED:-1}"
NATIVE_TESTS_REQUIRED="${NATIVE_TESTS_REQUIRED:-1}"
NATIVE_TESTS_REQUIRE_IF_PRESENT="${NATIVE_TESTS_REQUIRE_IF_PRESENT:-1}"
NATIVE_TEST_TIMEOUT="${NATIVE_TEST_TIMEOUT:-600}"
NATIVE_BASELINE_JSON="$WORK_DIR/native_tests_clean.json"
NATIVE_BASELINE_LOG="$WORK_DIR/native_tests_clean.log"
NATIVE_BASELINE_CONFIRMED=false
NATIVE_TEST_FILE_COUNT=$(find "$WORK_DIR/repo" -type f \( -path '*/tests/*.py' -o -path '*/test/*.py' -o -name 'test_*.py' -o -name '*_test.py' \) \
    | wc -l | tr -d ' ')
NATIVE_TESTS_PRESENT=false
BASELINE_REQUIRED=0

if [ "$NATIVE_TEST_FILE_COUNT" -gt 0 ]; then
    NATIVE_TESTS_PRESENT=true
fi

if [ "$NATIVE_TESTS_REQUIRED" = "1" ] || { [ "$NATIVE_TESTS_REQUIRE_IF_PRESENT" = "1" ] && [ "$NATIVE_TESTS_PRESENT" = "true" ]; }; then
    BASELINE_REQUIRED=1
fi

if [ "$NATIVE_TESTS_ENABLED" = "1" ]; then
    echo "=== Native test discovery: ${NATIVE_TEST_FILE_COUNT} candidate file(s) ==="
    if [ "$NATIVE_TESTS_PRESENT" != "true" ]; then
        pass_to_pass_fail "No original test files detected in repository"
    else
        echo "=== Baseline: verifying native tests on clean repo ==="
    BASELINE_EXIT=0
    timeout "$NATIVE_TEST_TIMEOUT" docker run --rm \
        -v "$WORK_DIR/repo:/repo" -v "$WORK_DIR:$WORK_DIR" -w /repo "$DEPS_IMAGE" \
        "$CONTAINER_PY" "$WORK_DIR/pytest_recorder.py" "$NATIVE_BASELINE_JSON" \
        -q --rootdir /repo -o addopts= \
        > "$NATIVE_BASELINE_LOG" 2>&1 || BASELINE_EXIT=$?

    NATIVE_BASELINE_COUNT=$(python3 -c "
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding='utf-8'))
    print(len(data.get('collected', [])))
except Exception:
    print(0)
" "$NATIVE_BASELINE_JSON" 2>/dev/null)

    if [ "$BASELINE_EXIT" -eq 124 ]; then
        pass_to_pass_fail "Original test baseline timed out after ${NATIVE_TEST_TIMEOUT}s"
    elif [ "$BASELINE_EXIT" -ne 0 ]; then
        echo "ERROR: Native baseline tests do not pass on the clean repo." >&2
        tail -40 "$NATIVE_BASELINE_LOG" || true
        pass_to_pass_fail "Original tests failed on the clean repository"
    elif [ "$NATIVE_BASELINE_COUNT" -eq 0 ]; then
        echo "ERROR: Native test files were detected but pytest collected 0 native tests." >&2
        tail -40 "$NATIVE_BASELINE_LOG" || true
        pass_to_pass_fail "Original test files exist but pytest collected 0 runnable tests"
    else
        NATIVE_BASELINE_CONFIRMED=true
        echo "=== Native baseline passed (${NATIVE_BASELINE_COUNT} collected test(s)) ==="

        # Save image to shared cache if newly built (so other tasks reuse it)
        if [ "$IMAGE_BUILT_NEW" = "true" ] && [ ! -f "$IMAGE_CACHE_TAR" ]; then
            echo "=== Saving image to cache: ${IMAGE_CACHE_TAR} ==="
            mkdir -p "$(dirname "$IMAGE_CACHE_TAR")"
            # Save to local disk first (Azure Files SMB can fail on large direct writes)
            LOCAL_SAVE="$WORK_DIR/deps_image.tar"
            if docker save -o "$LOCAL_SAVE" "$DEPS_IMAGE" 2>&1; then
                echo "=== docker save OK ($(du -h "$LOCAL_SAVE" | cut -f1)), copying to shared storage ==="
                if cp "$LOCAL_SAVE" "${IMAGE_CACHE_TAR}.tmp" 2>&1 && mv "${IMAGE_CACHE_TAR}.tmp" "$IMAGE_CACHE_TAR" 2>&1; then
                    echo "=== Image cached: ${IMAGE_CACHE_TAR} ($(du -h "$IMAGE_CACHE_TAR" | cut -f1)) ==="
                else
                    echo "WARN: Failed to copy image to shared storage (non-fatal)" >&2
                    rm -f "${IMAGE_CACHE_TAR}.tmp" 2>/dev/null || true
                fi
            else
                echo "WARN: docker save failed (non-fatal)" >&2
            fi
            rm -f "$LOCAL_SAVE" 2>/dev/null || true
        fi
    fi
    fi
else
    pass_to_pass_fail "Native baseline gate is disabled"
fi

# ── Session 0: Feature Planning (runs once before retry loop) ─────────────────
PLAN_MODEL="${PLAN_MODEL:-claude-opus-4.6-1m}"
if [ "${SKIP_PLAN:-0}" != "1" ]; then
    echo "=== Running Session 0: Feature Planning ==="
    bash "$SCRIPT_DIR/generate_step00_plan_features.sh" \
        "$WORK_DIR/repo" \
        "$WORK_DIR/feature_plan.txt" \
        "$PLAN_MODEL" \
        || echo "WARN: Session 0 failed (non-fatal) — proceeding without feature plan" >&2
else
    echo "=== Session 0 skipped (SKIP_PLAN=1) ==="
fi

# ── Generate case (with auto-retry on quality failures) ──────────────────────
MAX_RETRIES="${MAX_RETRIES:-3}"
PROMPT_TEMPLATE=$(cat "$PROMPT_PATH")
TASK_TS=$(date +%Y%m%d%H%M%S)
TASK_RUN_ID="${REPO_SLUG}-task-${TASK_TS}-${CASE_INDEX}"
ATTEMPT_METRICS_DIR="$WORK_DIR/copilot_metrics"
mkdir -p "$ATTEMPT_METRICS_DIR"
LAST_FAILURE_REASON=""

GEN_SUCCESS=false
for ATTEMPT in $(seq 1 "$MAX_RETRIES"); do
echo ""
echo "================================================================"
echo "=== Attempt ${ATTEMPT}/${MAX_RETRIES} for case ${CASE_INDEX} of ${REPO_SLUG} ==="
echo "================================================================"

# Reset repo to clean state before each attempt
cd "$WORK_DIR/repo"
git checkout -- . 2>/dev/null || true
git clean -fd -e Dockerfile.deps 2>/dev/null || true

TIMESTAMP=$(date +%Y%m%d%H%M%S)
INSTANCE_HASH=$(echo -n "${REPO_SLUG}-${CASE_INDEX}-${TIMESTAMP}-${ATTEMPT}" | md5sum | cut -c1-8)
INSTANCE_ID="feature-add-${INSTANCE_HASH}"

echo "=== Generating case ${CASE_INDEX} for ${REPO_SLUG} (${INSTANCE_ID}) ==="

# ── Build agent prompt ───────────────────────────────────────────────────────
# Read Session 0 feature plan if available, strip "Blast radius" lines
# (those are for internal evaluation only — showing them to S1 would make it
# "be careful" around those areas, reducing the chance of natural bugs)
FEATURE_PLAN=""
if [ -f "$WORK_DIR/feature_plan.txt" ]; then
    FEATURE_PLAN=$(grep -v '^\- \*\*Blast radius\*\*' "$WORK_DIR/feature_plan.txt" | grep -v '^\- \*\*影响范围\*\*')
    echo "=== Feature plan loaded: $(wc -l < "$WORK_DIR/feature_plan.txt") lines (blast radius stripped) ==="
fi

cat > "$WORK_DIR/full_prompt.md" << PROMPT_EOF
${PROMPT_TEMPLATE}

## Repository Info
- Repository: ${REPO_URL}
- Project: ${REPO_OWNER}/${REPO_NAME}
$([ -n "$FEATURE_TARGET" ] && echo -e "\n### Assigned Feature Target (MANDATORY)\nYour feature MUST primarily modify \`${FEATURE_TARGET}\`.\nYou may also touch other files if the feature naturally requires it, but the main behavioral change MUST be in this file.")
$([ -n "$PREV_MUTATIONS" ] && echo -e "\n### Previously Used Files (DO NOT mutate these again)\n${PREV_MUTATIONS}")

### Repository Structure
\`\`\`
${REPO_TREE}
\`\`\`

## Your Workflow (follow these steps IN ORDER)

You have tools to read files, modify files, and run shell commands.
The repository is already cloned at the current working directory.

### Step 1: Explore & Plan
- Read source files (use \`cat\` or file_read) to understand the codebase
- Review the project's structure, APIs, and existing tests
- Plan a realistic, small-to-medium feature that integrates with the existing architecture

### Step 2: Implement Feature
- Pick a feature from your plan and implement it
- Modify existing files and/or add new ones as needed
- Follow the project's coding style and conventions

### Docker Environment (MANDATORY)
A Docker image \`${DEPS_IMAGE}\` has been pre-built with all runtime and test dependencies installed.
**ALL shell commands that execute project code or run tests MUST run inside this container:**
\`\`\`
docker run --rm -v ${WORK_DIR}/repo:/repo -v ${WORK_DIR}:${WORK_DIR} -w /repo ${DEPS_IMAGE} <command>
\`\`\`
Examples:
- Run tests: \`docker run --rm -v ${WORK_DIR}/repo:/repo -v ${WORK_DIR}:${WORK_DIR} -w /repo ${DEPS_IMAGE} python3 -m pytest -x --timeout=60\`
- Run a Python script: \`docker run --rm -v ${WORK_DIR}/repo:/repo -v ${WORK_DIR}:${WORK_DIR} -w /repo ${DEPS_IMAGE} python3 script.py\`

You may still use \`cat\`, \`git\`, \`ls\`, \`find\` etc. directly on the host for reading files.
**Do NOT \`pip install\` on the host** — the container already has everything.

### Step 3: Run Existing Tests
- Run: \`docker run --rm -v ${WORK_DIR}/repo:/repo -v ${WORK_DIR}:${WORK_DIR} -w /repo ${DEPS_IMAGE} python3 -m pytest -x --timeout=60\`
- If ALL existing tests pass: go back to Step 2 for the next feature
- If ANY existing test fails: run \`git add -A && git commit -m "feature development - test failure detected"\`, then stop
- If you completed all features and all tests still pass: run \`git add -A && git commit -m "feature development complete"\` and stop
- Do NOT create any \`test_synthetic_*.py\` files. We only use the project's existing native tests for verification.
- Do NOT revert changes or run \`git checkout\`. Leave the repo as-is.
PROMPT_EOF

# ── Invoke gh copilot agent (single instance per node) ───────────────────────
echo "=== Running gh copilot agent (timeout=${COPILOT_TIMEOUT}s) ==="

COPILOT_EXIT=0
GENERATE_LOG_DIR="$WORK_DIR/copilot_logs/generate_attempt_${ATTEMPT}"
mkdir -p "$GENERATE_LOG_DIR"
GENERATE_START_MS=$(now_ms)
# Use copilot directly (not gh copilot --) for WSL/local compatibility.
# Pass prompt via file to avoid command-line argument length limits.
timeout "$COPILOT_TIMEOUT" copilot \
    --log-dir "$GENERATE_LOG_DIR" \
    --log-level debug \
    -p "$(cat "$WORK_DIR/full_prompt.md")" \
    --allow-all \
    --no-ask-user \
    --model "${COPILOT_MODEL:-claude-sonnet-4.6}" \
    2>&1 | tee "$WORK_DIR/copilot_output.txt" || COPILOT_EXIT=$?
GENERATE_END_MS=$(now_ms)
GENERATE_WALL_MS=$((GENERATE_END_MS - GENERATE_START_MS))
python3 "$SCRIPT_DIR/generate_step02_extract_copilot_metrics.py" \
    --log-dir "$GENERATE_LOG_DIR" \
    --invocation-type generate \
    --attempt "$ATTEMPT" \
    --wall-time-ms "$GENERATE_WALL_MS" \
    --exit-code "$COPILOT_EXIT" \
    > "$ATTEMPT_METRICS_DIR/generate_attempt_${ATTEMPT}.json"

# ── Fail-fast: check copilot result ──────────────────────────────────────────
if [ "$COPILOT_EXIT" -eq 124 ]; then
    LAST_FAILURE_REASON="generate_timeout"
    echo "WARN: gh copilot timed out after ${COPILOT_TIMEOUT}s — retrying" >&2
    continue
fi

# Copilot auth failure — no point retrying with the same token
if grep -q "No authentication information found" "$WORK_DIR/copilot_output.txt" 2>/dev/null; then
    echo "ERROR: Copilot authentication failed — token is invalid or lacks Copilot access. Failing fast." >&2
    exit 1
fi

OUTPUT_SIZE=$(wc -c < "$WORK_DIR/copilot_output.txt" 2>/dev/null || echo 0)
if [ "$OUTPUT_SIZE" -lt 100 ]; then
    LAST_FAILURE_REASON="generate_output_too_small"
    echo "WARN: gh copilot output too small (${OUTPUT_SIZE} bytes) — retrying" >&2
    continue
fi
echo "=== Agent finished (${OUTPUT_SIZE} bytes output) ==="

# ── Agent done — pipeline will check for regressions below ───────────────────
# No FEATURES_COMPLETE / FEATURES_INCOMPLETE markers.
# The pipeline determines success by running pytest and comparing with baseline.

# ── Self-check validation (disabled — difficulty control deferred) ────────────
# Self-check and difficulty gates are temporarily disabled to maximize case output.
# When difficulty control is re-enabled, uncomment this block.
if false && [ -n "$DIFFICULTY" ] && echo "$DIFFICULTY" | grep -qE '^L[34]$'; then
    SELF_CHECK_VERDICT=$(python3 -c "
import json, sys, re
output = open(sys.argv[1]).read()
m = re.search(r'SELF_CHECK_START\s*\n(.*?)SELF_CHECK_END', output, re.DOTALL)
if not m:
    print('MISSING')
    sys.exit(0)
try:
    d = json.loads(m.group(1).strip())
    verdict = d.get('overall_verdict', 'MISSING')
    matches = d.get('first_impression_matches_patch', True)
    looks_correct = d.get('buggy_code_looks_correct', False)
    # For L4: first impression must NOT match patch AND code must look correct
    if sys.argv[2] == 'L4' and (matches or not looks_correct):
        print('FAIL_L4')
    elif sys.argv[2] == 'L3' and matches:
        print('FAIL_L3')
    else:
        print(verdict)
except:
    print('PARSE_ERROR')
" "$WORK_DIR/copilot_output.txt" "$DIFFICULTY" 2>/dev/null)

    echo "=== Self-check verdict: ${SELF_CHECK_VERDICT} (difficulty: ${DIFFICULTY}) ==="
    case "$SELF_CHECK_VERDICT" in
        PASS) echo "Self-check passed" ;;
        MISSING)
            LAST_FAILURE_REASON="self_check_missing"
            echo "WARN: Agent did not output SELF_CHECK block for ${DIFFICULTY} — retrying" >&2
            continue ;;
        FAIL_L3|FAIL_L4|NEEDS_REVISION)
            LAST_FAILURE_REASON="self_check_failed_${DIFFICULTY}"
            echo "WARN: Self-check failed for ${DIFFICULTY} (${SELF_CHECK_VERDICT}) — issue not calibrated. Retrying." >&2
            continue ;;
        *)
            echo "WARN: Self-check parse issue (${SELF_CHECK_VERDICT}) — proceeding anyway" ;;
    esac
fi

# ── Capture actual changes from git ──────────────────────────────────────────
cd "$WORK_DIR/repo"

# Stage all new/modified .py files so git diff HEAD captures untracked additions
git add -A -- '*.py' 2>/dev/null || true

# patch = forward diff (model's actual changes, creates the regression)
PATCH=$(git diff HEAD -- '*.py')
if [ -z "$PATCH" ]; then
    LAST_FAILURE_REASON="no_source_patch"
    echo "WARN: No source file changes detected — retrying" >&2
    continue
fi
echo "=== Captured patch ($(echo "$PATCH" | wc -l) lines) ==="

# gold_patch removed — Feature Add has no single "correct fix", so reverse diff is meaningless.
# Verification uses git checkout to revert to clean state instead.

# ── Patch stats (informational, no hard file-count gates) ─────────────────────
NUM_PATCH_FILES=$(echo "$PATCH" | grep -c '^diff --git' || true)
NUM_PATCH_LINES=$(echo "$PATCH" | grep -cE '^[+-][^+-]' || true)
echo "=== Patch: ${NUM_PATCH_FILES} file(s), ~${NUM_PATCH_LINES} line(s), difficulty: ${DIFFICULTY:-any} ==="

# ── Run native tests on buggy code to find broken_by_mutation ────────────────
CASE_METADATA_JSON="$WORK_DIR/repo/case_metadata.json"

if [ "$NATIVE_BASELINE_CONFIRMED" != "true" ]; then
    LAST_FAILURE_REASON="native_baseline_not_confirmed"
    echo "WARN: Native baseline was not confirmed — cannot compute broken_by_mutation. Retrying." >&2
    continue
fi

echo "=== Running native tests on buggy code ==="
NATIVE_BUGGY_JSON="$WORK_DIR/native_tests_buggy_attempt_${ATTEMPT}.json"
NATIVE_BUGGY_LOG="$WORK_DIR/native_tests_buggy_attempt_${ATTEMPT}.log"
NATIVE_BUGGY_EXIT=0
timeout "$NATIVE_TEST_TIMEOUT" docker run --rm \
    -v "$WORK_DIR/repo:/repo" -v "$WORK_DIR:$WORK_DIR" -w /repo "$DEPS_IMAGE" \
    "$CONTAINER_PY" "$WORK_DIR/pytest_recorder.py" "$NATIVE_BUGGY_JSON" \
    -q --rootdir /repo -o addopts= \
    > "$NATIVE_BUGGY_LOG" 2>&1 || NATIVE_BUGGY_EXIT=$?

if [ "$NATIVE_BUGGY_EXIT" -eq 124 ]; then
    LAST_FAILURE_REASON="native_buggy_tests_timeout"
    echo "WARN: Native tests timed out on buggy code (${NATIVE_TEST_TIMEOUT}s). Mutation too destructive — retrying." >&2
    continue
fi

# Compute broken_by_mutation, pass_to_pass, fail_to_pass, test_command
PASS_TO_PASS_JSON="$WORK_DIR/pass_to_pass_attempt_${ATTEMPT}.json"
FAIL_TO_PASS_JSON="$WORK_DIR/fail_to_pass_attempt_${ATTEMPT}.json"
python3 -c "
import json, sys
baseline = json.load(open(sys.argv[1], encoding='utf-8'))
buggy = json.load(open(sys.argv[2], encoding='utf-8'))
baseline_passed = set(baseline.get('passed', []))
buggy_passed = set(buggy.get('passed', []))
buggy_failed = set(buggy.get('failed', [])) | set(buggy.get('errors', []))

broken_by_mutation = sorted(baseline_passed & buggy_failed)
shared = sorted(baseline_passed & buggy_passed)

with open(sys.argv[3], 'w', encoding='utf-8') as f:
    json.dump(shared, f, ensure_ascii=False)

with open(sys.argv[4], 'w', encoding='utf-8') as f:
    json.dump(broken_by_mutation, f, ensure_ascii=False)

# Print summary
if broken_by_mutation:
    print('--- Regressions (was PASS, now FAIL) ---', file=sys.stderr)
    for t in broken_by_mutation:
        print(f'  REGRESSION: {t}', file=sys.stderr)
    print(f'--- Total: {len(broken_by_mutation)} regression(s) ---', file=sys.stderr)

print(f'{len(shared)}|{len(broken_by_mutation)}|{len(baseline_passed)}')
" "$NATIVE_BASELINE_JSON" "$NATIVE_BUGGY_JSON" "$PASS_TO_PASS_JSON" "$FAIL_TO_PASS_JSON" \
    > "$WORK_DIR/native_stats.txt"

NATIVE_STATS=$(cat "$WORK_DIR/native_stats.txt" 2>/dev/null || echo "0|0|0")
PASS_TO_PASS_COUNT=$(echo "$NATIVE_STATS" | cut -d'|' -f1)
BROKEN_COUNT=$(echo "$NATIVE_STATS" | cut -d'|' -f2)
BASELINE_TOTAL=$(echo "$NATIVE_STATS" | cut -d'|' -f3)

echo "=== Native tests: ${PASS_TO_PASS_COUNT} pass_to_pass, ${BROKEN_COUNT} broken_by_mutation (of ${BASELINE_TOTAL} baseline) ==="

if [ "$BROKEN_COUNT" -eq 0 ]; then
    LAST_FAILURE_REASON="no_tests_broken"
    echo "WARN: Feature did not break any existing tests — no regression to use. Retrying." >&2
    continue
fi

if [ "$PASS_TO_PASS_COUNT" -eq 0 ]; then
    LAST_FAILURE_REASON="all_tests_broken"
    echo "WARN: All native tests broken by mutation — too destructive. Retrying." >&2
    continue
fi

# Build test_command from broken_by_mutation
FAIL_TO_PASS_TESTS=$(python3 -c "
import json, sys
tests = json.load(open(sys.argv[1], encoding='utf-8'))
print(' '.join(tests))
" "$FAIL_TO_PASS_JSON")
TEST_COMMAND="python3 -m pytest ${FAIL_TO_PASS_TESTS} -xvs"
echo "=== test_command: ${TEST_COMMAND} ==="

# ── Host verification: FAIL check only (no rollback needed) ──────────────────
echo "=== Host verification: FAIL check ==="
VERIFY_TIMEOUT="${VERIFY_TIMEOUT:-120}"
cd "$WORK_DIR/repo"
FAIL_EXIT=0
timeout "$VERIFY_TIMEOUT" docker run --rm \
    -v "$WORK_DIR/repo:/repo" -v "$WORK_DIR:$WORK_DIR" -w /repo "$DEPS_IMAGE" \
    bash -c "$TEST_COMMAND" \
    > "$WORK_DIR/verify_fail.txt" 2>&1 || FAIL_EXIT=$?
tail -20 "$WORK_DIR/verify_fail.txt"

if [ "$FAIL_EXIT" -eq 0 ]; then
    LAST_FAILURE_REASON="verification_fail_check_did_not_fail"
    echo "WARN: Tests PASS on buggy code — broken_by_mutation inconsistent. Retrying." >&2
    continue
fi
echo "--- FAIL check passed (exit=$FAIL_EXIT) ---"

# ── Session 2: Classify bug and generate issue_text ──────────────────────────
# S2 reads the diff + test output, writes case_metadata.json with:
# issue_text, feature_keyword, category, difficulty, mutation_file,
# mutation_description, repo_description, feature_description, bug_description
echo "=== Running Session 2: Classify & Report ==="

# Save patch to file for S2 to reference
echo "$PATCH" > "$WORK_DIR/patch.diff"

S2_MODEL="${S2_MODEL:-claude-opus-4.6}"
bash "$SCRIPT_DIR/generate_step02_classify_and_report.sh" \
    "$WORK_DIR" \
    "$REPO_SLUG" \
    "$INSTANCE_ID" \
    "$BASE_COMMIT" \
    "$CASE_INDEX" \
    "$S2_MODEL" \
    "$BATCH_VERSION" \
    || {
        echo "WARN: Session 2 failed — proceeding with basic metadata" >&2
    }

# ── Build case_metadata.json (merge S2 output with pipeline data) ────────────
FAIL_TO_PASS_LIST=$(cat "$FAIL_TO_PASS_JSON")
PASS_TO_PASS_LIST=$(cat "$PASS_TO_PASS_JSON")

python3 -c "
import json, sys, re, os

metadata_path = sys.argv[1]
patch = open(sys.argv[2]).read()
instance_id = sys.argv[3]
repo_slug = sys.argv[4]
base_commit = sys.argv[5]
case_index = sys.argv[6]
category = sys.argv[7] if len(sys.argv) > 7 else ''
difficulty = sys.argv[8] if len(sys.argv) > 8 else ''
test_command = sys.argv[9] if len(sys.argv) > 9 else ''
fail_to_pass = json.loads(sys.argv[10]) if len(sys.argv) > 10 else []
pass_to_pass = json.loads(sys.argv[11]) if len(sys.argv) > 11 else []
test_code = sys.argv[12] if len(sys.argv) > 12 else ''
batch_version = sys.argv[13] if len(sys.argv) > 13 else ''

# Try to read agent-created case_metadata.json for issue_text and other fields
case = None
if os.path.isfile(metadata_path):
    try:
        case = json.load(open(metadata_path, encoding='utf-8'))
        print(f'OK: Read case_metadata.json ({len(case)} keys)', file=sys.stderr)
    except (json.JSONDecodeError, OSError) as e:
        print(f'WARN: case_metadata.json invalid ({e}), building from scratch', file=sys.stderr)

if case is None:
    # Fallback: build metadata from git diff
    diff_files = re.findall(r'^diff --git a/(.*?) b/', patch, re.MULTILINE)
    mutation_file = diff_files[0] if diff_files else 'unknown'
    repo_owner, repo_name = repo_slug.split('__', 1)
    case = {
        'instance_id': instance_id,
        'repo': f'{repo_owner}/{repo_name}',
        'base_commit': base_commit,
        'source': 'synthetic_mutation',
        'setup_command': 'pip install -e .',
        'issue_text': f'Regression detected: {len(fail_to_pass)} existing test(s) now fail after recent changes.',
        'hints_text': '',
        'mutation_file': mutation_file,
        'mutation_description': f'Feature added in {mutation_file}',
        'category': category or 'Logic & Algorithm',
        'sub_type': 'auto_detected',
        'difficulty': difficulty or 'L2',
        'localization': 'implicit',
        'context_dependency': 'local_context',
        'test_modality': 'unit_test',
    }

# Host-authoritative fields (override whatever copilot wrote)
case['instance_id'] = instance_id
case['base_commit'] = base_commit
case['test_command'] = test_command
case['fail_to_pass'] = fail_to_pass
case['pass_to_pass'] = pass_to_pass
case['patch'] = patch
case['batch_version'] = batch_version
if test_code:
    case['test_code'] = test_code

# Normalize category into allowed enum, store in labels.category
VALID_CATEGORIES = {
    'Logic & Algorithm',
    'bugrelated',
    'Data Handling & Transformation',
    'API & Interface Contract',
    'Error Handling & Edge Cases',
    'Infrastructure & Tooling',
    'Performance & Efficiency',
    'Security & Access Control',
    'Configuration & Environment',
    'Type & Validation',
    'Documentation & Naming',
}
raw_category = case.get('category', '') or case.get('labels', {}).get('category', '') or ''
if raw_category not in VALID_CATEGORIES:
    raw_category = 'bugrelated'
labels = case.get('labels', {}) if isinstance(case.get('labels'), dict) else {}
labels['category'] = raw_category
case['labels'] = labels

with open(metadata_path, 'w', encoding='utf-8') as f:
    json.dump(case, f, indent=2, ensure_ascii=False)
print(f'OK: case_metadata.json ready (fail_to_pass={len(fail_to_pass)}, pass_to_pass={len(pass_to_pass)}, batch={batch_version})')
" "$CASE_METADATA_JSON" <(echo "$PATCH") \
  "$INSTANCE_ID" "$REPO_SLUG" "$BASE_COMMIT" "$CASE_INDEX" \
  "$CATEGORY" "$DIFFICULTY" "$TEST_COMMAND" \
  "$FAIL_TO_PASS_LIST" "$PASS_TO_PASS_LIST" "" "$BATCH_VERSION"

# ── Rebuild INSTANCE_ID with descriptive name ────────────────────────────────
# Now that case_metadata.json has feature_keyword and mutation_type from Copilot,
# rebuild INSTANCE_ID as: feature-{repo_name}-{feature_keyword}-{bug_type}-{hash}
# Also ensure repo_description, feature_description, bug_description fields exist.
NEW_INSTANCE_ID=$(python3 -c "
import json, sys, os, re

metadata_path = sys.argv[1]
old_id = sys.argv[2]
repo_name = sys.argv[3].lower()
instance_hash = sys.argv[4]

try:
    case = json.load(open(metadata_path, encoding='utf-8'))
except Exception:
    print(old_id)
    sys.exit(0)

# Extract feature_keyword (agent-provided, human-readable slug)
feature_kw = case.get('feature_keyword', '')
if not feature_kw:
    # Fallback: extract from mutation_description
    desc = case.get('mutation_description', '')
    if desc:
        # Take first 3-4 meaningful words, slugify
        words = re.findall(r'[a-zA-Z]+', desc)
        feature_kw = '_'.join(words[:4]).lower()
    else:
        feature_kw = 'unknown'

# Extract mutation_type -> bug type slug (use category value directly, slugified)
VALID_CATEGORIES = {
    'Logic & Algorithm': 'logic-algorithm',
    'Data Handling & Transformation': 'data-handling',
    'API & Interface Contract': 'api-contract',
    'Error Handling & Edge Cases': 'error-handling',
    'Infrastructure & Tooling': 'infra-tooling',
    'Performance & Efficiency': 'performance',
    'Security & Access Control': 'security',
    'Configuration & Environment': 'config-env',
    'Type & Validation': 'type-validation',
    'Documentation & Naming': 'doc-naming',
    'bugrelated': 'bugrelated',
}
mutation_type = case.get('mutation_type', '') or case.get('category', '') or case.get('labels', {}).get('category', '')
bug_slug = VALID_CATEGORIES.get(mutation_type, '')
if not bug_slug:
    # Try slugifying the raw value
    bug_slug = re.sub(r'[^a-z0-9]', '_', mutation_type.lower()).strip('_')[:30] if mutation_type else 'unknown'

# Sanitize feature_kw: use hyphens as word separator
feature_kw = re.sub(r'[^a-z0-9]', '-', feature_kw.lower()).strip('-')[:30]
# Sanitize bug_slug: use hyphens as word separator
bug_slug = re.sub(r'[^a-z0-9]', '-', bug_slug.lower()).strip('-')[:30]

new_id = f'gen-case__{repo_name}__{feature_kw}__{bug_slug}__{instance_hash}'

# Ensure description fields exist with fallbacks
if not case.get('repo_description'):
    case['repo_description'] = ''
if not case.get('feature_description'):
    case['feature_description'] = case.get('mutation_description', '')
if not case.get('bug_description'):
    case['bug_description'] = ''

# Update instance_id inside metadata
case['instance_id'] = new_id
with open(metadata_path, 'w', encoding='utf-8') as f:
    json.dump(case, f, indent=2, ensure_ascii=False)

print(new_id)
" "$CASE_METADATA_JSON" "$INSTANCE_ID" "$REPO_NAME" "$INSTANCE_HASH" 2>/dev/null)

if [ -n "$NEW_INSTANCE_ID" ] && [ "$NEW_INSTANCE_ID" != "$INSTANCE_ID" ]; then
    echo "=== Instance ID renamed: ${INSTANCE_ID} → ${NEW_INSTANCE_ID} ==="
    INSTANCE_ID="$NEW_INSTANCE_ID"
else
    echo "=== Instance ID unchanged: ${INSTANCE_ID} ==="
fi

# ── P1b: LLM Critic — semantic quality review (disabled — difficulty control deferred) ─
# Critic is temporarily disabled to maximize case output without quality gates.
# When difficulty control is re-enabled, uncomment the critic logic below.
if true; then
    echo "=== Skipping LLM Critic (difficulty control deferred) ==="
    CRITIC_VERDICT='{"verdict": "skipped", "reason": "critic disabled"}'
    CRITIC_TOTAL=6
else
echo "=== LLM Critic: semantic quality review ==="
CRITIC_TIMEOUT="${CRITIC_TIMEOUT:-120}"

# Extract issue_text from metadata for critic review
ISSUE_TEXT=$(python3 -c "
import json, sys, os
metadata_path = sys.argv[1]
if os.path.isfile(metadata_path):
    try:
        case = json.load(open(metadata_path, encoding='utf-8'))
        print(case.get('issue_text', ''))
    except: print('')
else: print('')
" "$CASE_METADATA_JSON" 2>/dev/null)

PATCH_SUMMARY=$(echo "$PATCH" | head -40)
TEST_SUMMARY=$(head -50 "$TEST_FILE")

cat > "$WORK_DIR/critic_prompt.md" << CRITIC_HEADER
You are a QA reviewer for synthetic SWE-bench benchmark instances. Review the following case and output a JSON verdict.
The assigned difficulty level is: ${DIFFICULTY:-unknown}

## Checklist (score each 0 or 1)
1. **issue_no_leak**: Does issue_text avoid mentioning filenames, function names, line numbers, or how to fix? (0 if any are mentioned)
2. **issue_patch_coherent**: Does issue_text describe a problem that the patch actually introduces? (0 if unrelated)
3. **issue_difficulty_calibrated**: Is the issue_text calibrated to the assigned difficulty?
   - L1: should include error message + repro code + specific API name → score 1 if it does
   - L2: should describe feature area + behavior, with repro, but NOT name internal functions → score 1 if it does
   - L3: should describe ONLY the symptom, and naturally lead to investigating the WRONG location first → score 1 if a developer's first instinct would be to look somewhere OTHER than the actual patch location
   - L4: score 1 ONLY if ALL of these hold: (a) issue contains at least one wrong hypothesis pointing to a genuinely unrelated area, (b) issue does NOT contain any accurate causal explanation of why the bug occurs — even in abstract terms without code identifiers, (c) a senior developer reading the issue would NOT look at the patch location as their first or second investigation target. Score 0 if the issue contains implementation-level vocabulary that maps to the real bug mechanism (e.g., "accumulator initialized before validation", "sort key inverted", "processing order reversed") even without naming files or functions
4. **test_deterministic**: Do the tests look deterministic? (0 if they use time.time(), random, sleep, or network calls with tight thresholds)
5. **test_behavior_not_content**: Do tests check behavior/output, not source code content? (0 if tests read/grep source files)
6. **no_test_in_patch**: Does patch modify ONLY source files, not test files? (0 if test files appear in patch)

Output EXACTLY this JSON (nothing else):
CRITIC_START
{"issue_no_leak": 0or1, "issue_patch_coherent": 0or1, "issue_difficulty_calibrated": 0or1, "test_deterministic": 0or1, "test_behavior_not_content": 0or1, "no_test_in_patch": 0or1, "total": sum_of_6, "verdict": "pass" or "fail", "reason": "one sentence if fail"}
CRITIC_END
CRITIC_HEADER

cat >> "$WORK_DIR/critic_prompt.md" << CRITIC_DATA

## Case Data

### issue_text
${ISSUE_TEXT}

### patch (first 40 lines)
\`\`\`diff
${PATCH_SUMMARY}
\`\`\`

### test_code (first 50 lines)
\`\`\`python
${TEST_SUMMARY}
\`\`\`
CRITIC_DATA

CRITIC_EXIT=0
CRITIC_LOG_DIR="$WORK_DIR/copilot_logs/critic_attempt_${ATTEMPT}"
mkdir -p "$CRITIC_LOG_DIR"
CRITIC_START_MS=$(now_ms)
timeout "$CRITIC_TIMEOUT" gh copilot -- \
    --log-dir "$CRITIC_LOG_DIR" \
    --log-level debug \
    -p "$(cat "$WORK_DIR/critic_prompt.md")" \
    --yolo \
    --no-ask-user \
    --model "${COPILOT_MODEL:-claude-sonnet-4.6}" \
    -s \
    2>&1 | tee "$WORK_DIR/critic_output.txt" || CRITIC_EXIT=$?
CRITIC_END_MS=$(now_ms)
CRITIC_WALL_MS=$((CRITIC_END_MS - CRITIC_START_MS))
python3 "$SCRIPT_DIR/generate_step02_extract_copilot_metrics.py" \
    --log-dir "$CRITIC_LOG_DIR" \
    --invocation-type critic \
    --attempt "$ATTEMPT" \
    --wall-time-ms "$CRITIC_WALL_MS" \
    --exit-code "$CRITIC_EXIT" \
    > "$ATTEMPT_METRICS_DIR/critic_attempt_${ATTEMPT}.json"

# Parse critic verdict
CRITIC_VERDICT=$(python3 -c "
import json, sys, re
output = open(sys.argv[1]).read()
m = re.search(r'CRITIC_START\s*\n(.*?)CRITIC_END', output, re.DOTALL)
if not m:
    # Try to find JSON directly
    m = re.search(r'\{[^{}]*\"verdict\"[^{}]*\}', output)
    if m:
        try:
            d = json.loads(m.group(0))
            print(json.dumps(d))
            sys.exit(0)
        except: pass
    print('{\"verdict\": \"unknown\", \"reason\": \"critic output unparseable\"}')
    sys.exit(0)
try:
    d = json.loads(m.group(1).strip())
    print(json.dumps(d))
except:
    print('{\"verdict\": \"unknown\", \"reason\": \"critic JSON invalid\"}')
" "$WORK_DIR/critic_output.txt" 2>/dev/null)

echo "Critic verdict: $CRITIC_VERDICT"

# Inject critic result into metadata
python3 -c "
import json, sys, os
metadata_path = sys.argv[1]
critic = json.loads(sys.argv[2])
if os.path.isfile(metadata_path):
    try:
        case = json.load(open(metadata_path, encoding='utf-8'))
        case['critic_review'] = critic
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(case, f, indent=2, ensure_ascii=False)
        print('OK: Critic review injected into metadata')
    except Exception as e:
        print(f'WARN: Could not inject critic review: {e}', file=sys.stderr)
else:
    print('WARN: case_metadata.json not found; skipping critic injection', file=sys.stderr)
" "$CASE_METADATA_JSON" "$CRITIC_VERDICT"

# Fail on critic rejection (verdict=fail with total<5 means serious quality issue)
CRITIC_TOTAL=$(python3 -c "
import json, sys
d = json.loads(sys.argv[1])
print(d.get('total', 6))
" "$CRITIC_VERDICT" 2>/dev/null || echo 6)

if [ "$CRITIC_TOTAL" -lt 4 ]; then
    LAST_FAILURE_REASON="critic_rejected_case"
    echo "WARN: LLM Critic rejected case (score=${CRITIC_TOTAL}/6). Retrying." >&2
    continue
fi
echo "=== LLM Critic: passed (score=${CRITIC_TOTAL}/6) ==="

fi  # end critic if/else (L1/L2 skip vs L3/L4 run)

# All checks passed — break out of retry loop
echo "=== Attempt ${ATTEMPT}/${MAX_RETRIES} SUCCEEDED ==="
GEN_SUCCESS=true
break

done  # end retry loop

if [ "$GEN_SUCCESS" != "true" ]; then
    python3 "$SCRIPT_DIR/generate_step03_aggregate_case_metrics.py" \
        --metrics-dir "$ATTEMPT_METRICS_DIR" \
        --task-run-id "$TASK_RUN_ID" \
        --repo-slug "$REPO_SLUG" \
        --case-index "$CASE_INDEX" \
        --model "${COPILOT_MODEL:-claude-sonnet-4.6}" \
        --max-retries "$MAX_RETRIES" \
        --pipeline-success false \
        --failure-reason "$LAST_FAILURE_REASON" \
        > "$METRICS_DIR/${TASK_RUN_ID}.failed.metrics.json"
    echo "========================================" >&2
    echo "FAILED: SELF-CHECK/GENERATION FAIL ${MAX_RETRIES}/${MAX_RETRIES}" >&2
    echo "REASON: ${LAST_FAILURE_REASON}" >&2
    echo "CASE: ${CASE_INDEX} REPO: ${REPO_SLUG}" >&2
    echo "========================================" >&2
    echo "========================================"
    echo "FAILED: SELF-CHECK/GENERATION FAIL ${MAX_RETRIES}/${MAX_RETRIES}"
    echo "REASON: ${LAST_FAILURE_REASON}"
    echo "CASE: ${CASE_INDEX} REPO: ${REPO_SLUG}"
    echo "========================================"
    exit 1
fi

python3 "$SCRIPT_DIR/generate_step03_aggregate_case_metrics.py" \
    --metrics-dir "$ATTEMPT_METRICS_DIR" \
    --task-run-id "$TASK_RUN_ID" \
    --repo-slug "$REPO_SLUG" \
    --case-index "$CASE_INDEX" \
    --model "${COPILOT_MODEL:-claude-sonnet-4.6}" \
    --max-retries "$MAX_RETRIES" \
    --pipeline-success true \
    --instance-id "$INSTANCE_ID" \
    --successful-attempt "$ATTEMPT" \
    > "$WORK_DIR/case_metrics_summary.json"
cp "$WORK_DIR/case_metrics_summary.json" "$METRICS_DIR/${INSTANCE_ID}.metrics.json"

# ── Post-process: AI output -> tar.gz + jsonl ────────────────────────────────
python3 "$SCRIPT_DIR/generate_step04_package_case_artifacts.py" \
    --repo-dir "$WORK_DIR/repo" \
    --ai-output "$WORK_DIR/copilot_output.txt" \
    --repo-slug "$REPO_SLUG" \
    --base-commit "$BASE_COMMIT" \
    --case-index "$CASE_INDEX" \
    --timestamp "$TIMESTAMP" \
    --targz-dir "$TARGZ_DIR" \
    --jsonl-dir "$JSONL_DIR" \
    --metrics-summary "$WORK_DIR/case_metrics_summary.json"

# ── Copy artifacts to batch-versioned directory ──────────────────────────────
BATCH_TARGZ_DIR="${OUTPUT_BASE}/${BATCH_VERSION}/tar.gz"
BATCH_JSONL_DIR="${OUTPUT_BASE}/${BATCH_VERSION}/jsonl"
mkdir -p "$BATCH_TARGZ_DIR" "$BATCH_JSONL_DIR"

# Find the artifacts just created by step04 (named by INSTANCE_ID)
if [ -f "${TARGZ_DIR}/${INSTANCE_ID}.tar.gz" ]; then
    cp "${TARGZ_DIR}/${INSTANCE_ID}.tar.gz" "${BATCH_TARGZ_DIR}/"
    echo "=== Copied tar.gz to batch dir: ${BATCH_TARGZ_DIR}/${INSTANCE_ID}.tar.gz ==="
fi
if [ -f "${JSONL_DIR}/${INSTANCE_ID}.jsonl" ]; then
    cp "${JSONL_DIR}/${INSTANCE_ID}.jsonl" "${BATCH_JSONL_DIR}/"
    echo "=== Copied jsonl to batch dir: ${BATCH_JSONL_DIR}/${INSTANCE_ID}.jsonl ==="
fi

echo "=== Generation complete: ${INSTANCE_ID} (batch: ${BATCH_VERSION}) ==="
