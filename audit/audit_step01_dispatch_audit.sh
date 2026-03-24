#!/bin/bash
# ============================================================================
# audit_step01_dispatch_audit.sh — Dispatch SWE-bench generated case audits (L1-L7)
# ============================================================================
# Two modes:
#   Single case (pipeline):
#     bash audit/audit_step01_dispatch_audit.sh <jsonl_blob_path> <mount_root> <audit_level> <output_dir>
#   Batch mode (all cases in a folder):
#     bash audit/audit_step01_dispatch_audit.sh --batch <input_folder> <output_folder> [audit_level]
#
# Single-case arguments:
#   jsonl_blob_path — Blob path to the JSONL file (relative to container root)
#   mount_root      — Mounted storage root (e.g. $AZ_BATCH_NODE_MOUNTS_DIR/storage_container)
#   audit_level     — L1..L7 (default: L7)
#   output_dir      — Directory to write audit result JSON
# ============================================================================
set -euo pipefail

# ── Determine Python executable ─────────────────────────────────────────────
find_python() {
  for candidate in python3.11 python3.12 python3 python; do
    if command -v "$candidate" &>/dev/null; then
      if "$candidate" -c "import pip" 2>/dev/null; then
        echo "$candidate"
        return
      fi
    fi
  done
  # Fallback: bootstrap pip on python3
  curl -sS https://bootstrap.pypa.io/get-pip.py | python3 - --break-system-packages 2>/dev/null || true
  echo "python3"
}

PY=$(find_python)
echo "Using Python: $PY ($(${PY} --version 2>&1))"

# Install pytest if missing
if ! $PY -c "import pytest" 2>/dev/null; then
  echo "Installing pytest..."
  $PY -m pip install pytest --break-system-packages 2>/dev/null || \
  $PY -m pip install pytest 2>/dev/null || true
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Batch mode ───────────────────────────────────────────────────────────────
if [ "${1:-}" = "--batch" ]; then
  INPUT_FOLDER="$2"
  OUTPUT_FOLDER="$3"
  AUDIT_LEVEL="${4:-L7}"

  JSONL_DIR="${INPUT_FOLDER}/jsonl"
  TARGZ_DIR="${INPUT_FOLDER}/tar.gz"
  mkdir -p "$OUTPUT_FOLDER"

  if [ -f "${SCRIPT_DIR}/audit_step02_batch_audit_cases.py" ]; then
    $PY "${SCRIPT_DIR}/audit_step02_batch_audit_cases.py" \
        --jsonl-dir "$JSONL_DIR" \
        --targz-dir "$TARGZ_DIR" \
        --output-dir "$OUTPUT_FOLDER" \
        --level "$AUDIT_LEVEL"
  else
    echo "Batch mode: auditing all JSONL files in $JSONL_DIR"
    for JSONL_FILE in "${JSONL_DIR}"/*.jsonl; do
      [ -f "$JSONL_FILE" ] || continue
      BASENAME=$(basename "$JSONL_FILE" .jsonl)
      echo "--- Auditing: $BASENAME ---"

      WORK_DIR=$(mktemp -d)
      INSTANCE_DIR="${WORK_DIR}/instance"
      mkdir -p "$INSTANCE_DIR"

      # Convert JSONL (first line) to proper JSON
      $PY -c "
import json, sys
data = json.loads(open(sys.argv[1]).readline())
with open(sys.argv[2], 'w') as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
" "$JSONL_FILE" "${INSTANCE_DIR}/${BASENAME}.json"

      # Extract tar.gz if available
      TARGZ_FILE="${TARGZ_DIR}/${BASENAME}.tar.gz"
      if [ -f "$TARGZ_FILE" ]; then
        tar xzf "$TARGZ_FILE" -C "$INSTANCE_DIR" 2>/dev/null || true
      fi

      $PY "${SCRIPT_DIR}/audit_step03_validate_instance.py" \
        "${INSTANCE_DIR}/${BASENAME}.json" \
        --level "$AUDIT_LEVEL" \
        --json-output "${OUTPUT_FOLDER}/${BASENAME}-audit.json" \
        2>&1 || true

      rm -rf "$WORK_DIR"
    done
  fi
  echo "=== Audit complete. Results in: ${OUTPUT_FOLDER} ==="
  exit 0
fi

# ── Single-case mode (for ADF pipeline) ─────────────────────────────────────
JSONL_BLOB_PATH="$1"
MOUNT_ROOT="$2"
AUDIT_LEVEL="${3:-L7}"
OUTPUT_DIR="$4"

echo "════════════════════════════════════════════════════════════════"
echo "  Audit Case"
echo "  JSONL:  $JSONL_BLOB_PATH"
echo "  Mount:  $MOUNT_ROOT"
echo "  Level:  $AUDIT_LEVEL"
echo "  Output: $OUTPUT_DIR"
echo "════════════════════════════════════════════════════════════════"

# Locate JSONL file
JSONL_FULL_PATH="${MOUNT_ROOT}/${JSONL_BLOB_PATH}"
if [ ! -f "$JSONL_FULL_PATH" ]; then
  echo "ERROR: JSONL file not found: $JSONL_FULL_PATH"
  exit 1
fi
echo "Found JSONL: $JSONL_FULL_PATH"

# Parse instance_id
INSTANCE_ID=$($PY -c "
import json, sys
data = json.loads(open(sys.argv[1]).readline())
print(data.get('instance_id', 'unknown'))
" "$JSONL_FULL_PATH")
echo "Instance ID: $INSTANCE_ID"

# Prepare work directory
JSONL_BASENAME=$(basename "$JSONL_FULL_PATH" .jsonl)
JSONL_DIR_PATH=$(dirname "$JSONL_FULL_PATH")
TARGZ_DIR_PATH="${JSONL_DIR_PATH}/../tar.gz"
TARGZ_FILE="${TARGZ_DIR_PATH}/${JSONL_BASENAME}.tar.gz"

WORK_DIR=$(mktemp -d)
INSTANCE_DIR="${WORK_DIR}/instance"
mkdir -p "$INSTANCE_DIR"

# Convert JSONL to JSON
$PY -c "
import json, sys
data = json.loads(open(sys.argv[1]).readline())
with open(sys.argv[2], 'w') as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
" "$JSONL_FULL_PATH" "${INSTANCE_DIR}/${JSONL_BASENAME}.json"

# Extract snapshot tar.gz
if [ -f "$TARGZ_FILE" ]; then
  echo "Extracting snapshot: $TARGZ_FILE"
  tar xzf "$TARGZ_FILE" -C "$INSTANCE_DIR" 2>/dev/null || {
    echo "WARN: tar extraction failed, L2-L4/L7 checks may fail"
  }
else
  echo "WARN: No tar.gz at $TARGZ_FILE — L2/L3/L4/L7 may fail"
fi

# Copy audit script
if [ -f "${SCRIPT_DIR}/audit_step03_validate_instance.py" ]; then
  cp "${SCRIPT_DIR}/audit_step03_validate_instance.py" "${WORK_DIR}/"
else
  echo "ERROR: audit_step03_validate_instance.py not found in ${SCRIPT_DIR}"
  exit 1
fi

# Run audit
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  Running audit (level: $AUDIT_LEVEL)"
echo "════════════════════════════════════════════════════════════════"
mkdir -p "$OUTPUT_DIR"

RESULT_FILE="${OUTPUT_DIR}/${JSONL_BASENAME}-audit.json"

$PY "${WORK_DIR}/audit_step03_validate_instance.py" \
  "${INSTANCE_DIR}/${JSONL_BASENAME}.json" \
  --level "$AUDIT_LEVEL" \
  --json-output "$RESULT_FILE" \
  2>&1 || {
    echo "WARN: audit_step03_validate_instance.py exited with code $?"
  }

if [ -f "$RESULT_FILE" ]; then
  echo ""
  echo "════════════════════════════════════════════════════════════════"
  echo "  Audit complete: $RESULT_FILE"
  echo "════════════════════════════════════════════════════════════════"
  cat "$RESULT_FILE"
else
  echo "ERROR: No audit result file generated"
  exit 1
fi

rm -rf "$WORK_DIR"
echo "Done."
