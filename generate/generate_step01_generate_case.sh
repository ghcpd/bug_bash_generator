#!/bin/bash
# ============================================================================
# generate_step01_generate_case.sh — Generate ONE synthetic SWE-bench case via Copilot CLI
# ============================================================================
# Usage (called by ADF Custom Activity — one Batch node per case):
#   bash generate/generate_step01_generate_case.sh <task_json> <github_token> <prompt_path> <output_base>
#
# Arguments:
#   task_json    — Base64-encoded JSON, e.g. {"repo":"https://...","case_index":0,"category":"Logic & Algorithm","difficulty":"L1"}
#   github_token — GitHub Token for Copilot CLI auth
#   prompt_path  — Full path to prompt file on mounted storage (falls back to built-in default_prompt.md)
#   output_base  — Output root directory (mounted storage path)
#
# Architecture:
#   Each case = one ForEach item = one Batch node = one gh copilot instance.
#   Parallelism is controlled by ADF batchCount (= number of Batch nodes).
#   No in-process parallelism — avoids gh copilot single-instance conflicts.
# ============================================================================
set -ex

# Ensure ~/.local/bin is in PATH — pip install --user puts executables there
export PATH="$HOME/.local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

TASK_JSON=$(echo "$1" | base64 -d)
GITHUB_TOKEN="$2"
PROMPT_PATH="$3"
OUTPUT_BASE="$4"

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

# Ensure pip is available — handle PEP 668 externally-managed environments
if ! $PY -m pip --version 2>/dev/null; then
  echo "pip not found for $PY — bootstrapping..."
  # Try ensurepip first
  $PY -m ensurepip --user 2>&1 || $PY -m ensurepip 2>&1 || true
  # If still no pip, use get-pip.py with --break-system-packages (PEP 668)
  if ! $PY -m pip --version 2>/dev/null; then
    echo "ensurepip unavailable — downloading get-pip.py (--break-system-packages)..."
    curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py \
      && $PY /tmp/get-pip.py --user --break-system-packages 2>&1 || true
  fi
fi
echo "pip: $($PY -m pip --version 2>&1 || echo 'STILL NOT FOUND')"

# Ensure pytest is installed
$PY -m pip install --user --break-system-packages pytest 2>&1 \
  || $PY -m pip install --user pytest 2>&1 \
  || $PY -m pip install pytest 2>&1 || true
if ! $PY -c "import pytest" 2>/dev/null; then
  echo "WARN: pytest not importable — force reinstall..." >&2
  $PY -m pip install --user --break-system-packages --force-reinstall pytest 2>&1 || true
fi
echo "pytest: $($PY -m pytest --version 2>&1 || echo 'NOT FOUND')"

# ── Preflight: verify GitHub auth BEFORE doing any real work ─────────────────
echo "=== Preflight: checking GitHub auth ==="
if ! timeout 30 gh auth status 2>&1; then
    echo "ERROR: GitHub auth failed — GH_TOKEN is invalid or expired. Failing fast." >&2
    exit 1
fi
echo "=== Preflight: auth OK ==="

# ── Prompt file: prefer provided path, fall back to built-in default_prompt.md
if [ -f "$PROMPT_PATH" ]; then
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
mkdir -p "$TARGZ_DIR" "$JSONL_DIR" "$METRICS_DIR"

# ── Temporary workspace ──────────────────────────────────────────────────────
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

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
        self.collected = [
            item.nodeid for item in items
            if "test_synthetic_" not in item.nodeid
        ]

    def pytest_runtest_logreport(self, report):
        if "test_synthetic_" in report.nodeid:
            return
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
for f in sorted(glob.glob(jsonl_dir + '/feature-add-*.jsonl')):
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
for PREV_JSONL in $(find "$JSONL_DIR" -name "feature-add-*.jsonl" 2>/dev/null | sort); do
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

# Install dependencies (runtime + test)
# Strategy 0: if uv.lock exists, use uv to install locked dependencies into current python
UV_USED=false
if [ -f "uv.lock" ]; then
    echo "=== Detected uv.lock — using uv for precise dependency installation ==="
    $PY -m pip install uv 2>/dev/null || true
    if command -v uv &>/dev/null; then
        # Use uv export to dump all locked dependencies (including all groups) as requirements.txt
        # Then install with pip into the current python environment
        if uv export --frozen --no-hashes --all-groups --all-extras -o /tmp/_uv_requirements.txt 2>/dev/null; then
            echo "=== Installing locked dependencies from uv.lock (all groups + extras) ==="
            $PY -m pip install -r /tmp/_uv_requirements.txt 2>/dev/null && UV_USED=true && echo "=== uv locked deps installed ===" || echo "WARN: pip install from uv export failed" >&2
            rm -f /tmp/_uv_requirements.txt
        else
            echo "WARN: uv export failed, falling back to pip" >&2
        fi
        # Also install the project itself
        $PY -m pip install -e . 2>/dev/null || SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 $PY -m pip install -e . 2>/dev/null || true
    else
        echo "WARN: uv not available after install, falling back to pip" >&2
    fi
fi

# Always run pip-based installation (uv may have covered some deps, pip fills the rest)
BASE_INSTALL_OK=false
if [ "$UV_USED" = "true" ]; then BASE_INSTALL_OK=true; fi  # uv already installed the project
if $PY -m pip install -e . 2>/dev/null; then
    BASE_INSTALL_OK=true
elif SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 $PY -m pip install -e . 2>/dev/null; then
    BASE_INSTALL_OK=true
else
    echo "WARN: Base package install failed (may need system libs or C compiler). Trying pip install without editable..." >&2
    if $PY -m pip install . 2>/dev/null || SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 $PY -m pip install . 2>/dev/null; then
        BASE_INSTALL_OK=true
    else
        echo "WARN: Package install failed entirely. Tests will likely not work." >&2
    fi
fi

# Install test/dev dependencies — only try extras if base package installed successfully
# Skip entirely if uv.lock already installed all deps (PEP 735 groups included)
echo "=== Installing test dependencies ==="

if [ "$UV_USED" = "true" ]; then
    echo "=== uv.lock already installed all deps (including dev/test groups) — skipping extras ==="
elif [ "$BASE_INSTALL_OK" != "true" ]; then
    echo "WARN: Skipping extras/dependency-groups install because base package failed to build." >&2
else

# Strategy 1a: parse pyproject.toml [project.optional-dependencies] — install via pip extras
OPTIONAL_GROUPS=$(python3 -c "
import sys
try:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    with open('pyproject.toml', 'rb') as f:
        data = tomllib.load(f)
    groups = list(data.get('project', {}).get('optional-dependencies', {}).keys())
    extras = data.get('tool', {}).get('setuptools', {}).get('extras_require', {})
    groups.extend(extras.keys())
    for g in sorted(set(groups)):
        print(g)
except Exception:
    pass
" 2>/dev/null)

if [ -n "$OPTIONAL_GROUPS" ]; then
    echo "Found optional-dependency groups: $OPTIONAL_GROUPS"
    for group in $OPTIONAL_GROUPS; do
        echo "Installing optional group: [$group]"
        $PY -m pip install -e ".[$group]" 2>/dev/null \
            || SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 $PY -m pip install -e ".[$group]" 2>/dev/null \
            || echo "  WARN: failed to install [$group]"
    done
else
    echo "No optional-dependency groups found in pyproject.toml; trying common extra names"
    for extra in test testing tests dev all async; do
        $PY -m pip install -e ".[$extra]" 2>/dev/null && echo "Installed extra: $extra" || true
        SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 $PY -m pip install -e ".[$extra]" 2>/dev/null || true
    done
fi

# Strategy 1b: parse pyproject.toml [dependency-groups] (PEP 735) — install packages directly
DEP_GROUP_PKGS=$(python3 -c "
import sys
try:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    with open('pyproject.toml', 'rb') as f:
        data = tomllib.load(f)
    dep_groups = data.get('dependency-groups', {})
    if not dep_groups:
        sys.exit(0)
    # Install ALL dependency-group packages — don't guess which groups are test-related
    for group_name, pkgs in dep_groups.items():
        for pkg in pkgs:
            if isinstance(pkg, str):
                stripped = pkg.strip()
                if stripped and not stripped.startswith('{'):
                    print(stripped)
            elif isinstance(pkg, dict) and 'include-group' in pkg:
                # Handle {include-group = "xxx"} references
                ref = pkg['include-group']
                for p in dep_groups.get(ref, []):
                    if isinstance(p, str):
                        s = p.strip()
                        if s and not s.startswith('{'):
                            print(s)
except Exception:
    pass
" 2>/dev/null)

if [ -n "$DEP_GROUP_PKGS" ]; then
    echo "Found PEP 735 dependency-group packages:"
    echo "$DEP_GROUP_PKGS"
    echo "$DEP_GROUP_PKGS" | while read -r pkg; do
        echo "Installing dependency-group package: $pkg"
        $PY -m pip install "$pkg" 2>/dev/null || true
    done
fi

fi  # end of UV_USED/BASE_INSTALL_OK check for extras/dependency-groups

# Strategy 2: install from common requirements files (always try, even if base install failed)
# Check root-level requirements files
for reqfile in requirements-test.txt requirements-dev.txt requirements_test.txt requirements_dev.txt test-requirements.txt dev-requirements.txt; do
    if [ -f "$reqfile" ]; then
        echo "Installing from $reqfile"
        $PY -m pip install -r "$reqfile" 2>/dev/null || true
    fi
done
# Check requirements/ subdirectory (common pattern: requirements/tests.txt, requirements/dev.txt, etc.)
if [ -d "requirements" ]; then
    for reqfile in requirements/test.txt requirements/tests.txt requirements/testing.txt requirements/dev.txt requirements/ci.txt; do
        if [ -f "$reqfile" ]; then
            echo "Installing from $reqfile"
            $PY -m pip install -r "$reqfile" 2>/dev/null || true
        fi
    done
fi
# Check docs/requirements.txt and similar nested patterns
for reqfile in $(find . -maxdepth 2 -name 'requirements*.txt' -not -path './build/*' -not -path './.tox/*' 2>/dev/null); do
    echo "Installing from $reqfile"
    $PY -m pip install -r "$reqfile" 2>/dev/null || true
done

# Strategy 3: parse tox.ini deps if present
if [ -f "tox.ini" ]; then
    echo "Parsing tox.ini for test dependencies"
    python3 -c "
import configparser, sys
c = configparser.ConfigParser()
c.read('tox.ini')
for sec in c.sections():
    if 'testenv' in sec:
        deps = c.get(sec, 'deps', fallback='')
        for line in deps.strip().splitlines():
            line = line.strip()
            if line and not line.startswith('-') and not line.startswith('#'):
                print(line)
        break
" 2>/dev/null | while read -r dep; do
        $PY -m pip install "$dep" 2>/dev/null || true
    done
fi

# Strategy 4: parse setup.py extras_require if present and no pyproject.toml groups found
if [ -z "$OPTIONAL_GROUPS" ] && [ -f "setup.py" ]; then
    python3 -c "
import ast, sys
with open('setup.py') as f:
    tree = ast.parse(f.read())
for node in ast.walk(tree):
    if isinstance(node, ast.keyword) and node.arg == 'extras_require':
        if isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant):
                    print(key.value)
" 2>/dev/null | while read -r extra; do
        echo "Installing setup.py extra: [$extra]"
        $PY -m pip install -e ".[$extra]" 2>/dev/null || true
    done
fi

echo "=== Test dependency installation complete ==="

# Re-install the project itself in editable mode to ensure local dev version
# takes precedence over any release version that pip may have pulled in as a
# transitive dependency during test dep installation.
echo "=== Re-installing project (editable) to restore local dev version ==="
$PY -m pip install -e . --no-deps 2>/dev/null \
    || SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 $PY -m pip install -e . --no-deps 2>/dev/null \
    || true

# ── Baseline: require original tests and verify clean pass-to-pass baseline ──
NATIVE_TESTS_ENABLED="${NATIVE_TESTS_ENABLED:-1}"
NATIVE_TESTS_REQUIRED="${NATIVE_TESTS_REQUIRED:-1}"
NATIVE_TESTS_REQUIRE_IF_PRESENT="${NATIVE_TESTS_REQUIRE_IF_PRESENT:-1}"
NATIVE_TEST_TIMEOUT="${NATIVE_TEST_TIMEOUT:-600}"
NATIVE_BASELINE_JSON="$WORK_DIR/native_tests_clean.json"
NATIVE_BASELINE_LOG="$WORK_DIR/native_tests_clean.log"
NATIVE_BASELINE_CONFIRMED=false
NATIVE_TEST_FILE_COUNT=$(find "$WORK_DIR/repo" -type f \( -path '*/tests/*.py' -o -path '*/test/*.py' -o -name 'test_*.py' -o -name '*_test.py' \) \
    ! -name 'test_synthetic_*.py' | wc -l | tr -d ' ')
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
    timeout "$NATIVE_TEST_TIMEOUT" "$PY" "$WORK_DIR/pytest_recorder.py" "$NATIVE_BASELINE_JSON" \
        -q --rootdir "$WORK_DIR/repo" -o addopts= \
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
    fi
    fi
else
    pass_to_pass_fail "Native baseline gate is disabled"
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
git clean -fd 2>/dev/null || true
rm -f "test_synthetic_${CASE_INDEX}.py"

TIMESTAMP=$(date +%Y%m%d%H%M%S)
INSTANCE_HASH=$(echo -n "${REPO_SLUG}-${CASE_INDEX}-${TIMESTAMP}-${ATTEMPT}" | md5sum | cut -c1-8)
INSTANCE_ID="feature-add-${INSTANCE_HASH}"

echo "=== Generating case ${CASE_INDEX} for ${REPO_SLUG} (${INSTANCE_ID}) ==="

# ── Build agent prompt ───────────────────────────────────────────────────────
cat > "$WORK_DIR/full_prompt.md" << PROMPT_EOF
${PROMPT_TEMPLATE}

## Target Repository Context
- Repository: ${REPO_URL}
- Base commit: ${BASE_COMMIT}
- Owner: ${REPO_OWNER}
- Project: ${REPO_NAME}
- Instance ID: ${INSTANCE_ID}
- Case index: ${CASE_INDEX}
$([ -n "$FEATURE_TARGET" ] && echo -e "\n### Assigned Feature Target (MANDATORY)\nYour feature MUST primarily modify \`${FEATURE_TARGET}\`.\nYou may also touch other files if the feature naturally requires it, but the main behavioral change MUST be in this file.\nDo NOT pick a completely unrelated file to modify — the regression should come from changing \`${FEATURE_TARGET}\`.")
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

### Step 3: Run Existing Tests
- Run: \`$PY -m pytest -x --timeout=60\` (or the project's test command)
- If ALL existing tests pass: go back to Step 2 for the next feature (up to 3 features total)
- If ANY existing test fails: move to Step 4
- If you completed all 3 features and all tests still pass: output FEATURES_COMPLETE and stop

### Step 4: Extract Minimal Reproducer
- Examine which existing test(s) failed — understand input, expected output, and actual output
- Create \`test_synthetic_${CASE_INDEX}.py\` in the repo root
- Extract 2-3 minimal, independent test functions that reproduce the observed failure
- Tests must be deterministic — NO \`time.time()\` with tight thresholds
- Test observable behavior, not source code content

### Step 5: Verify FAIL
- Run: \`$PY -m pytest test_synthetic_${CASE_INDEX}.py -xvs\`
- Confirm the synthetic tests FAIL on the current code
- If tests PASS, your reproducer is wrong — revise it

### Step 6: Verify PASS
- Revert all changes: \`git checkout -- .\`
- Run: \`$PY -m pytest test_synthetic_${CASE_INDEX}.py -xvs\`
- Confirm the synthetic tests PASS on the original code
- If tests FAIL, your reproducer is wrong — revise it

### Step 7: Restore modified state
- Re-apply your changes so the repo ends in the modified state
- Verify: \`$PY -m pytest test_synthetic_${CASE_INDEX}.py -xvs\` should FAIL again

### Step 8: Review actual diff and write issue_text (CRITICAL — do this LAST)
- Run: \`git diff -- '*.py' ':!test_synthetic_*'\`
- Read the ACTUAL diff output carefully
- Write issue_text calibrated to the assigned difficulty level:
  - **L1**: Include the error message/exception. Include a minimal repro snippet. Name the specific API.
  - **L2**: Describe the feature area and wrong behavior. Include a repro snippet. Do NOT name internal functions.
  - **L3**: Describe ONLY the symptom. The description must lead a developer to look in the WRONG place first. NO repro code that calls the affected function. NO naming the affected module/function.
  - **L4**: Be vague, describe intermittent or confusing behavior. Optionally blame the wrong subsystem. NO repro code that reveals the root cause. NO naming ANY internal function/module near the change. Write as a confused end-user, not a developer who has narrowed it down.
- NEVER mention source filenames, line numbers, or how to fix — write as a real user
- This ensures your issue_text accurately matches the actual changes AND the assigned difficulty level

### Step 8.5: Self-check (MANDATORY — output SELF_CHECK block)
You MUST output a structured self-check BEFORE the CASE_START block. Output it between SELF_CHECK_START and SELF_CHECK_END markers (see the Self-Check section in the prompt above for the exact JSON format).

Rules:
- Answer honestly: what would a developer investigate first based on your issue_text alone?
- If \`overall_verdict\` is \`NEEDS_REVISION\`, go back and rewrite your issue_text, then re-run the self-check
- Do NOT proceed to Step 9 until overall_verdict is PASS

### Step 9: Output metadata
Output EXACTLY ONE JSON block between CASE_START and CASE_END markers.
Do NOT include \`test_code\` — that will be captured from the actual file changes.

CASE_START
{
  "instance_id": "${INSTANCE_ID}",
  "repo": "${REPO_OWNER}/${REPO_NAME}",
  "base_commit": "${BASE_COMMIT}",
  "source": "synthetic_mutation",
  "setup_command": "<actual shell command to install this project>",
  "test_command": "$PY -m pytest test_synthetic_${CASE_INDEX}.py -xvs",
  "issue_text": "<markdown bug report describing the SYMPTOM only>",
  "hints_text": "",
  "test_filename": "test_synthetic_${CASE_INDEX}.py",
  "mutation_file": "<relative/path/to/mutated/file.py>",
  "mutation_description": "<one sentence: what feature you implemented>",
  "fail_to_pass": ["test_synthetic_${CASE_INDEX}.py::<test_function_name>"],
  "pass_to_pass": [],
  "category": "$([ -n "$CATEGORY" ] && echo "${CATEGORY}" || echo "<choose from the 10 categories above>")",
  "sub_type": "<specific mutation type>",
  "difficulty": "$([ -n "$DIFFICULTY" ] && echo "${DIFFICULTY}" || echo "<L1|L2|L3|L4>")",
  "localization": "<explicit|implicit|cross_file|cross_module>",
  "context_dependency": "<self_contained|local_context|cross_module>",
  "test_modality": "<unit_test|integration_test>",
  "capabilities": ["code_understanding", "debugging"],
  "multi_solution": false
}
CASE_END
PROMPT_EOF

# ── Invoke gh copilot agent (single instance per node) ───────────────────────
echo "=== Running gh copilot agent (timeout=${COPILOT_TIMEOUT}s) ==="

COPILOT_EXIT=0
GENERATE_LOG_DIR="$WORK_DIR/copilot_logs/generate_attempt_${ATTEMPT}"
mkdir -p "$GENERATE_LOG_DIR"
GENERATE_START_MS=$(now_ms)
timeout "$COPILOT_TIMEOUT" gh copilot -- \
    --log-dir "$GENERATE_LOG_DIR" \
    --log-level debug \
    -p "$(cat "$WORK_DIR/full_prompt.md")" \
    --yolo \
    --no-ask-user \
    --model "${COPILOT_MODEL:-claude-sonnet-4.6}" \
    -s \
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

# ── Check if agent completed all features without breaking tests (no bug produced) ─
if grep -q "FEATURES_COMPLETE" "$WORK_DIR/copilot_output.txt" 2>/dev/null; then
    if ! grep -q "CASE_START" "$WORK_DIR/copilot_output.txt" 2>/dev/null; then
        echo "FEATURES_COMPLETE: All features implemented successfully. No existing tests were broken — no bug case produced."
        echo "FEATURES_COMPLETE: All features implemented successfully. No existing tests were broken — no bug case produced." >&2
        exit 1
    fi
fi

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
git add -A -- '*.py' ':!test_synthetic_*' 2>/dev/null || true

# patch = forward diff (model's actual changes, creates the regression)
PATCH=$(git diff HEAD -- '*.py' ':!test_synthetic_*')
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

# Capture test file content
TEST_FILE="$WORK_DIR/repo/test_synthetic_${CASE_INDEX}.py"
if [ ! -f "$TEST_FILE" ]; then
    LAST_FAILURE_REASON="missing_generated_test"
    echo "WARN: Test file not created by agent — retrying" >&2
    continue
fi
TEST_CODE=$(cat "$TEST_FILE")

# Inject patch and test_code into copilot output for generate_step04_package_case_artifacts.py
# If agent didn't produce CASE_START/CASE_END, build metadata from git diff + test file
python3 -c "
import json, sys, re, os

output = open(sys.argv[1]).read()
patch = open(sys.argv[2]).read()
test_code = open(sys.argv[3]).read()
instance_id = sys.argv[4]
repo_slug = sys.argv[5]
base_commit = sys.argv[6]
case_index = sys.argv[7]
category = sys.argv[8] if len(sys.argv) > 8 else ''
difficulty = sys.argv[9] if len(sys.argv) > 9 else ''
feature_target = sys.argv[10] if len(sys.argv) > 10 else ''

m = re.search(r'CASE_START\s*\n(.*?)CASE_END', output, re.DOTALL)
if m:
    # Agent produced metadata — parse and inject real patch
    try:
        case = json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        print('WARN: CASE_START/CASE_END found but JSON invalid, building fallback', file=sys.stderr)
        m = None

if not m:
    # Fallback: build metadata from what we have
    print('WARN: No valid CASE_START/CASE_END in agent output — building metadata from git diff', file=sys.stderr)

    # Extract mutation file from patch
    diff_files = re.findall(r'^diff --git a/(.*?) b/', patch, re.MULTILINE)
    mutation_file = diff_files[0] if diff_files else 'unknown'

    # Extract test function names from test code
    test_funcs = re.findall(r'^def (test_\w+)', test_code, re.MULTILINE)
    test_filename = f'test_synthetic_{case_index}.py'
    fail_to_pass = [f'{test_filename}::{fn}' for fn in test_funcs]

    repo_owner, repo_name = repo_slug.split('__', 1)
    case = {
        'instance_id': instance_id,
        'repo': f'{repo_owner}/{repo_name}',
        'base_commit': base_commit,
        'source': 'synthetic_mutation',
        'setup_command': 'pip install -e .',
        'test_command': f'\$PY -m pytest {test_filename} -xvs',
        'issue_text': f'Tests in {test_filename} are failing. The functions {\", \".join(test_funcs[:3])} report unexpected behavior.',
        'hints_text': '',
        'test_filename': test_filename,
        'mutation_file': mutation_file,
        'mutation_description': f'Bug injected in {mutation_file}',
        'fail_to_pass': fail_to_pass,
        'pass_to_pass': [],
        'category': category or 'Logic & Algorithm',
        'sub_type': 'auto_detected',
        'difficulty': difficulty or 'L2',
        'localization': 'explicit',
        'context_dependency': 'self_contained',
        'test_modality': 'unit_test',
        'capabilities': ['code_understanding', 'debugging'],
        'multi_solution': False,
    }

case['patch'] = patch
case['test_code'] = test_code
# gold_patch removed — Feature Add has no single correct fix

new_block = 'CASE_START\n' + json.dumps(case, indent=2, ensure_ascii=False) + '\nCASE_END'
if m:
    new_output = output[:m.start()] + new_block + output[m.end():]
else:
    new_output = output + '\n' + new_block + '\n'

with open(sys.argv[1], 'w') as f:
    f.write(new_output)
print('OK: Metadata ready (patch + test_code injected)')
" "$WORK_DIR/copilot_output.txt" <(echo "$PATCH") <(echo "$TEST_CODE") \
  "$INSTANCE_ID" "$REPO_SLUG" "$BASE_COMMIT" "$CASE_INDEX" "$CATEGORY" "$DIFFICULTY" "$FEATURE_TARGET"

# ── P1a: Host-side FAIL→PASS verification (independent of agent self-report) ─
echo "=== Host verification: FAIL→PASS check ==="
VERIFY_TIMEOUT="${VERIFY_TIMEOUT:-120}"  # 2 minutes for each pytest run

cd "$WORK_DIR/repo"

# Pre-check: ensure pytest is importable before running verification
if ! $PY -c "import pytest" 2>/dev/null; then
    echo "ERROR: pytest not found for $PY before verification. Re-installing..." >&2
    $PY -m pip install --user pytest 2>&1 || $PY -m pip install pytest 2>&1 || true
    if ! $PY -c "import pytest" 2>/dev/null; then
        echo "ERROR: pytest still not importable by $PY after re-install. Cannot verify." >&2
        continue
    fi
fi
echo "pytest OK: $($PY -m pytest --version 2>&1)"

# Step A: Tests must FAIL on buggy code (current state)
echo "--- Verifying tests FAIL on buggy code ---"
FAIL_EXIT=0
timeout "$VERIFY_TIMEOUT" $PY -m pytest "test_synthetic_${CASE_INDEX}.py" -x --tb=short \
    > "$WORK_DIR/verify_fail.txt" 2>&1 || FAIL_EXIT=$?
tail -20 "$WORK_DIR/verify_fail.txt"

# Guard: exit code 1 could mean "pytest not found" — check output for actual test results
if grep -q "no tests ran\|ModuleNotFoundError\|No module named" "$WORK_DIR/verify_fail.txt" 2>/dev/null; then
    LAST_FAILURE_REASON="verification_fail_check_invalid"
    echo "WARN: FAIL check produced suspicious output (pytest issue, not test failure). Retrying." >&2
    continue
fi

if [ "$FAIL_EXIT" -eq 0 ]; then
    LAST_FAILURE_REASON="verification_fail_check_did_not_fail"
    echo "WARN: Tests PASS on buggy code — mutation is ineffective. Retrying." >&2
    continue
fi
echo "--- FAIL check passed (exit=$FAIL_EXIT) ---"

# Step B: Revert to clean state, tests must PASS
echo "--- Verifying tests PASS on clean code ---"
git checkout HEAD -- . 2>/dev/null || true
git clean -fd -e "test_synthetic_*.py" 2>/dev/null || true

PASS_EXIT=0
timeout "$VERIFY_TIMEOUT" $PY -m pytest "test_synthetic_${CASE_INDEX}.py" -x --tb=short \
    > "$WORK_DIR/verify_pass.txt" 2>&1 || PASS_EXIT=$?
tail -20 "$WORK_DIR/verify_pass.txt"

if [ "$PASS_EXIT" -ne 0 ]; then
    LAST_FAILURE_REASON="verification_pass_check_failed"
    echo "WARN: Tests FAIL on clean code — synthetic test is broken. Retrying." >&2
    continue
fi
echo "--- PASS check passed ---"

# Step C: Re-apply forward patch to restore buggy state for snapshot
git add -A -- '*.py' ':!test_synthetic_*' 2>/dev/null || true
echo "$PATCH" | git apply --allow-empty 2>&1 || true
echo "=== Host verification: FAIL→PASS confirmed ==="

# ── Host-side native pass_to_pass capture ───────────────────────────────────
PASS_TO_PASS_JSON="$WORK_DIR/pass_to_pass_attempt_${ATTEMPT}.json"
echo "[]" > "$PASS_TO_PASS_JSON"
if [ "$NATIVE_BASELINE_CONFIRMED" = "true" ]; then
    echo "=== Host verification: native pass_to_pass check ==="
    NATIVE_BUGGY_JSON="$WORK_DIR/native_tests_buggy_attempt_${ATTEMPT}.json"
    NATIVE_BUGGY_LOG="$WORK_DIR/native_tests_buggy_attempt_${ATTEMPT}.log"
    NATIVE_BUGGY_EXIT=0
    timeout "$NATIVE_TEST_TIMEOUT" "$PY" "$WORK_DIR/pytest_recorder.py" "$NATIVE_BUGGY_JSON" \
        -q --rootdir "$WORK_DIR/repo" -o addopts= \
        > "$NATIVE_BUGGY_LOG" 2>&1 || NATIVE_BUGGY_EXIT=$?

    if [ "$NATIVE_BUGGY_EXIT" -eq 124 ]; then
        LAST_FAILURE_REASON="native_pass_to_pass_timeout"
        echo "WARN: Native pass_to_pass: original tests did not complete on buggy repo within ${NATIVE_TEST_TIMEOUT}s. Mutation too destructive — retrying." >&2
        continue
    fi

    # Calculate pass_to_pass with tolerance: allow up to NATIVE_FAIL_TOLERANCE_PCT% of native tests to break
    NATIVE_FAIL_TOLERANCE_PCT="${NATIVE_FAIL_TOLERANCE_PCT:-5}"
    python3 -c "
import json, sys
baseline = json.load(open(sys.argv[1], encoding='utf-8'))
buggy = json.load(open(sys.argv[2], encoding='utf-8'))
baseline_passed = set(baseline.get('passed', []))
buggy_passed = set(buggy.get('passed', []))
buggy_failed = set(buggy.get('failed', [])) | set(buggy.get('errors', []))

# Tests that passed on clean but failed on buggy = broken by mutation
broken_by_mutation = baseline_passed & buggy_failed
shared = sorted(baseline_passed & buggy_passed)

tolerance_pct = int(sys.argv[4])
baseline_count = len(baseline_passed)
broken_count = len(broken_by_mutation)
max_allowed = max(1, baseline_count * tolerance_pct // 100)

with open(sys.argv[3], 'w', encoding='utf-8') as f:
    json.dump(shared, f, ensure_ascii=False)

# Print regressions (tests that were passing but now fail)
if broken_by_mutation:
    print('--- Regressions (was PASS, now FAIL) ---', file=sys.stderr)
    for t in sorted(broken_by_mutation):
        print(f'  REGRESSION: {t}', file=sys.stderr)
    print(f'--- Total: {broken_count} regression(s) ---', file=sys.stderr)

print(f'{len(shared)}|{broken_count}|{baseline_count}|{max_allowed}')
" "$NATIVE_BASELINE_JSON" "$NATIVE_BUGGY_JSON" "$PASS_TO_PASS_JSON" "$NATIVE_FAIL_TOLERANCE_PCT" > "$WORK_DIR/pass_to_pass_stats.txt"
    P2P_STATS=$(cat "$WORK_DIR/pass_to_pass_stats.txt" 2>/dev/null || echo "0|0|0|0")
    PASS_TO_PASS_COUNT=$(echo "$P2P_STATS" | cut -d'|' -f1)
    BROKEN_COUNT=$(echo "$P2P_STATS" | cut -d'|' -f2)
    BASELINE_TOTAL=$(echo "$P2P_STATS" | cut -d'|' -f3)
    MAX_ALLOWED=$(echo "$P2P_STATS" | cut -d'|' -f4)

    echo "=== Native pass_to_pass: ${PASS_TO_PASS_COUNT} green, ${BROKEN_COUNT} broken by mutation (tolerance: disabled for Feature Add) ==="

    if [ "$PASS_TO_PASS_COUNT" -eq 0 ]; then
        LAST_FAILURE_REASON="native_pass_to_pass_zero_shared"
        echo "WARN: No original tests remained passing. Mutation too destructive — retrying." >&2
        continue
    fi

    # Tolerance gate disabled for Feature Add — regressions are expected
    # if [ "$BROKEN_COUNT" -gt "$MAX_ALLOWED" ]; then
    #     LAST_FAILURE_REASON="native_pass_to_pass_too_many_broken"
    #     echo "WARN: Mutation broke ${BROKEN_COUNT} original test(s), exceeding tolerance of ${MAX_ALLOWED} (${NATIVE_FAIL_TOLERANCE_PCT}%). Retrying." >&2
    #     continue
    # fi
    echo "=== Native pass_to_pass captured (${PASS_TO_PASS_COUNT} test(s) stay green) ==="
fi

python3 -c "
import json, sys, re
output_path, pass_to_pass_path = sys.argv[1], sys.argv[2]
output = open(output_path, encoding='utf-8').read()
pass_to_pass = json.load(open(pass_to_pass_path, encoding='utf-8'))
m = re.search(r'CASE_START\s*\n(.*?)CASE_END', output, re.DOTALL)
if not m:
    print('WARN: CASE_START/CASE_END missing; cannot inject pass_to_pass', file=sys.stderr)
    sys.exit(0)
case = json.loads(m.group(1).strip())
case['pass_to_pass'] = pass_to_pass
new_block = 'CASE_START\n' + json.dumps(case, indent=2, ensure_ascii=False) + '\nCASE_END'
with open(output_path, 'w', encoding='utf-8') as f:
    f.write(output[:m.start()] + new_block + output[m.end():])
print('OK: pass_to_pass injected into metadata')
" "$WORK_DIR/copilot_output.txt" "$PASS_TO_PASS_JSON"

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
import json, sys, re
output = open(sys.argv[1]).read()
m = re.search(r'CASE_START\s*\n(.*?)CASE_END', output, re.DOTALL)
if m:
    try:
        case = json.loads(m.group(1).strip())
        print(case.get('issue_text', ''))
    except: print('')
else: print('')
" "$WORK_DIR/copilot_output.txt" 2>/dev/null)

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
import json, sys, re
output = open(sys.argv[1]).read()
critic = json.loads(sys.argv[2])
m = re.search(r'CASE_START\s*\n(.*?)CASE_END', output, re.DOTALL)
if m:
    try:
        case = json.loads(m.group(1).strip())
        case['critic_review'] = critic
        new_block = 'CASE_START\n' + json.dumps(case, indent=2, ensure_ascii=False) + '\nCASE_END'
        output = output[:m.start()] + new_block + output[m.end():]
        with open(sys.argv[1], 'w') as f:
            f.write(output)
        print('OK: Critic review injected into metadata')
    except Exception as e:
        print(f'WARN: Could not inject critic review: {e}', file=sys.stderr)
" "$WORK_DIR/copilot_output.txt" "$CRITIC_VERDICT"

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

echo "=== Generation complete: ${INSTANCE_ID} ==="
