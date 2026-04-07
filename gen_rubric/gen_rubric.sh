#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# gen_rubric.sh
#
# Usage (mode 1 – JSONL batch):
#   ./gen_rubric.sh --jsonl <jsonl_file> <output_dir> <prompt_dir> <github_token>
#
# Usage (mode 2 – single repo via git):
#   ./gen_rubric.sh --repo <repo_url> <branch> <output_dir> <prompt_dir> <github_token>
#
# Usage (mode 3 – single repo via tar.gz):
#   ./gen_rubric.sh --tar <case_name> <archive_dir> <output_dir> <prompt_file> <github_token> [<fix_prompt_dir> [<baseline_model>]]
#
# Mode 1: For each record in the JSONL file this script will:
#   1. Clone the repo (key: "repo") from GitHub
#   2. Checkout the branch (key: "branch")
#   3. Call copilot-cli to generate a rubric
#   4. Copy the generated "rubric" file to <output_dir>/<repo_name>/<bug_id>_rubric.json
#
# Mode 2: For a single repo + branch:
#   1. Clone the repo from the given URL
#   2. Checkout the given branch
#   3. Call copilot-cli to generate a rubric
#   4. Copy the generated "rubric" file to <output_dir>/rubric.json
#
# Mode 3: For a single tar.gz archive:
#   1. Extract the tar.gz to a temp directory
#   2. (Optional) If fix_prompt_file is provided, init a local git repo,
#      run copilot-cli with baseline model (default: claude-opus-4.6) to
#      produce a baseline fix, and commit locally (no push).
#   3. Call copilot-cli to generate a rubric
#   4. Copy the generated "rubric" file to <output_dir>/rubric.json
#
# The JSONL mode also auto-detects tar.gz: if the "repo" field ends with
# .tar.gz or .tgz it will extract instead of git-cloning.
###############################################################################

usage() {
  echo "Usage:"
  echo "  $0 --jsonl <jsonl_file> <output_dir> <prompt_dir> <github_token>"
  echo "  $0 --repo  <repo_url>  <branch>     <output_dir>  <prompt_dir> <github_token>"
  echo "  $0 --tar   <case_name> <archive_dir> <output_dir> <prompt_file> <github_token> [<fix_prompt_dir> [<baseline_model>]]"
  exit 1
}

if [[ $# -lt 1 ]]; then usage; fi

MODE="$1"
shift

###############################################################################
# Common helpers
###############################################################################
WORK_DIR="$(mktemp -d)"

cleanup() {
  echo "Cleaning up work dir: $WORK_DIR"
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

# process_repo <source> <branch> <clone_dir> <prompt> <label>
#   source  — a git URL  OR  a path to a .tar.gz / .tgz archive
#   branch  — git branch to checkout (ignored for tar.gz)
#   Clones/extracts, checks out, runs copilot-cli, and sets RUBRIC_FILE.
process_repo() {
  local source="$1" branch="$2" clone_dir="$3" prompt="$4" label="$5"

  if [[ "$source" == *.tar.gz || "$source" == *.tgz ]]; then
    # ── tar.gz mode ──────────────────────────────────────────────────────
    echo "[1/4] Extracting $source ..."
    mkdir -p "$clone_dir"
    tar -xzf "$source" -C "$clone_dir" --strip-components=1
    if [[ $? -ne 0 ]]; then echo "ERROR: tar extract failed for $source"; exit 1; fi

    echo "[2/4] (skipped – tar.gz, no branch checkout)"
  else
    # ── git mode ─────────────────────────────────────────────────────────
    echo "[1/4] Cloning $source ..."
    git clone --quiet "$source" "$clone_dir"
    if [[ $? -ne 0 ]]; then echo "ERROR: git clone failed for $source"; exit 1; fi

    echo "[2/4] Checking out branch: $branch ..."
    pushd "$clone_dir" > /dev/null
    git checkout --quiet "$branch"
    if [[ $? -ne 0 ]]; then echo "ERROR: git checkout failed for branch $branch"; exit 1; fi
    popd > /dev/null
  fi

  echo "[3/4] Running copilot-cli ..."
  pushd "$clone_dir" > /dev/null
  export GITHUB_TOKEN
  copilot -p "$prompt" --allow-all --no-ask-user --model gpt-5.2 --allow-tool 'write' --allow-all-paths
  if [[ $? -ne 0 ]]; then echo "ERROR: copilot-cli failed for $label"; exit 1; fi
  popd > /dev/null

  # Locate the generated rubric
  RUBRIC_FILE=$(find "$clone_dir" -name "rubric.json" -type f | head -n 1)
}

###############################################################################
# Mode 1: JSONL batch
###############################################################################
if [[ "$MODE" == "--jsonl" ]]; then
  if [[ $# -lt 4 ]]; then usage; fi

  JSONL_FILE="$1"
  OUTPUT_DIR="$2"
  PROMPT_DIR="$3"
  GITHUB_TOKEN="$4"

  mkdir -p "$OUTPUT_DIR"

  if [[ ! -d "$PROMPT_DIR" ]]; then
    echo "ERROR: Prompt directory not found: $PROMPT_DIR"; exit 1
  fi

  echo "=== gen_rubric.sh (jsonl mode) ==="
  echo "JSONL file : $JSONL_FILE"
  echo "Output dir : $OUTPUT_DIR"
  echo "Work dir   : $WORK_DIR"
  echo "Prompt dir : $PROMPT_DIR"
  echo ""

  line_num=0
  while IFS= read -r line; do
    line_num=$((line_num + 1))

    # ── Parse fields from the JSON line ────────────────────────────────────
    bug_id=$(echo "$line"   | jq -r '.bug_id')
    if [[ $? -ne 0 ]]; then echo "ERROR: Failed to parse bug_id from line $line_num"; exit 1; fi
    repo=$(echo "$line"     | jq -r '.repo')
    if [[ $? -ne 0 ]]; then echo "ERROR: Failed to parse repo from line $line_num"; exit 1; fi
    branch=$(echo "$line"   | jq -r '.branch')
    if [[ $? -ne 0 ]]; then echo "ERROR: Failed to parse branch from line $line_num"; exit 1; fi

    echo "────────────────────────────────────────"
    echo "[$line_num] bug_id=$bug_id  repo=$repo  branch=$branch"
    echo "────────────────────────────────────────"

    # Detect whether repo is a tar.gz path or a GitHub org/repo
    if [[ "$repo" == *.tar.gz || "$repo" == *.tgz ]]; then
      repo_url="$repo"
      # Derive repo name from tar.gz filename
      repo_name_for_prompt="$(basename "${repo%.tar.gz}")"
      repo_name_for_prompt="${repo_name_for_prompt%.tgz}"
    else
      repo_url="https://github.com/${repo}.git"
      repo_name_for_prompt="$(basename "$repo")"
    fi
    clone_dir="${WORK_DIR}/${bug_id}"

    # Look up prompt file in prompt directory by repo name
    PROMPT_FILE="${PROMPT_DIR}/${repo_name_for_prompt}.md"
    if [[ ! -f "$PROMPT_FILE" ]]; then
      echo "ERROR: Prompt file not found: $PROMPT_FILE"; exit 1
    fi
    PROMPT=$(<"$PROMPT_FILE")

    process_repo "$repo_url" "$branch" "$clone_dir" "$PROMPT" "bug_id=$bug_id"

    # ── Copy the generated rubric file to the output directory ─────────────
    repo_name="${repo##*/}"
    dest_dir="${OUTPUT_DIR}/${repo_name}"
    mkdir -p "$dest_dir"
    dest_name="${bug_id}_rubric.json"

    if [[ -n "${RUBRIC_FILE:-}" ]]; then
      echo "[4/4] Found rubric: $RUBRIC_FILE"
      echo "       Copying -> ${dest_dir}/${dest_name}"
      cp "$RUBRIC_FILE" "${dest_dir}/${dest_name}"
      if [[ $? -ne 0 ]]; then echo "ERROR: cp failed"; exit 1; fi
    else
      echo "[4/4] WARNING: rubric.json not found anywhere in $clone_dir"
    fi

    echo ""
  done < "$JSONL_FILE"

  echo "=== Done. Processed $line_num record(s). ==="

###############################################################################
# Mode 2: Single repo
###############################################################################
elif [[ "$MODE" == "--repo" ]]; then
  if [[ $# -lt 5 ]]; then usage; fi

  REPO_URL="$1"
  BRANCH="$2"
  OUTPUT_DIR="$3"
  PROMPT_DIR="$4"
  GITHUB_TOKEN="$5"

  mkdir -p "$OUTPUT_DIR"

  if [[ ! -d "$PROMPT_DIR" ]]; then
    echo "ERROR: Prompt directory not found: $PROMPT_DIR"; exit 1
  fi

  # Derive a short name from the repo URL for the clone dir and prompt lookup
  repo_basename="$(basename "${REPO_URL%.git}")"
  clone_dir="${WORK_DIR}/${repo_basename}"

  PROMPT_FILE="${PROMPT_DIR}/${repo_basename}.md"
  if [[ ! -f "$PROMPT_FILE" ]]; then
    echo "ERROR: Prompt file not found: $PROMPT_FILE"; exit 1
  fi
  PROMPT=$(<"$PROMPT_FILE")

  echo "=== gen_rubric.sh (repo mode) ==="
  echo "Repo URL   : $REPO_URL"
  echo "Branch     : $BRANCH"
  echo "Output dir : $OUTPUT_DIR"
  echo "Work dir   : $WORK_DIR"
  echo "Prompt dir : $PROMPT_DIR"
  echo "Prompt file: $PROMPT_FILE"
  echo ""

  process_repo "$REPO_URL" "$BRANCH" "$clone_dir" "$PROMPT" "$REPO_URL@$BRANCH"

  if [[ -n "${RUBRIC_FILE:-}" ]]; then
    echo "[4/4] Found rubric: $RUBRIC_FILE"
    echo "       Copying -> ${OUTPUT_DIR}/rubric.json"
    cp "$RUBRIC_FILE" "${OUTPUT_DIR}/rubric.json"
    if [[ $? -ne 0 ]]; then echo "ERROR: cp failed"; exit 1; fi
  else
    echo "[4/4] WARNING: rubric.json not found anywhere in $clone_dir"
  fi

  echo ""
  echo "=== Done. ==="

###############################################################################
# Mode 3: tar.gz archive
###############################################################################
elif [[ "$MODE" == "--tar" ]]; then
  if [[ $# -lt 5 ]]; then usage; fi

  CASE_NAME="$1"
  ARCHIVE_DIR="$2"
  OUTPUT_DIR="$3"
  PROMPT_FILE="$4"
  GITHUB_TOKEN="$5"
  FIX_PROMPT_DIR="${6:-}"
  BASELINE_MODEL="${7:-claude-opus-4.6}"

  # Derive file paths from directories + case name
  TAR_FILE="${ARCHIVE_DIR}/${CASE_NAME}.tar.gz"
  if [[ -n "$FIX_PROMPT_DIR" ]]; then
    FIX_PROMPT_FILE="${FIX_PROMPT_DIR}/${CASE_NAME}.md"
  else
    FIX_PROMPT_FILE=""
  fi

  if [[ ! -f "$TAR_FILE" ]]; then
    echo "ERROR: tar.gz file not found: $TAR_FILE"; exit 1
  fi

  mkdir -p "$OUTPUT_DIR"

  clone_dir="${WORK_DIR}/${CASE_NAME}"

  if [[ ! -f "$PROMPT_FILE" ]]; then
    echo "ERROR: Prompt file not found: $PROMPT_FILE"; exit 1
  fi
  PROMPT=$(<"$PROMPT_FILE")

  echo "=== gen_rubric.sh (tar mode) ==="
  echo "Case name      : $CASE_NAME"
  echo "Archive        : $TAR_FILE"
  echo "Output dir     : $OUTPUT_DIR"
  echo "Work dir       : $WORK_DIR"
  echo "Prompt file    : $PROMPT_FILE"
  if [[ -n "$FIX_PROMPT_FILE" ]]; then
    echo "Fix prompt file: $FIX_PROMPT_FILE"
    echo "Baseline model : $BASELINE_MODEL"
  fi
  echo ""

  # [1/5] Extract tar.gz
  echo "[1/5] Extracting $TAR_FILE ..."
  mkdir -p "$clone_dir"
  tar -xzf "$TAR_FILE" -C "$clone_dir" --strip-components=1
  if [[ $? -ne 0 ]]; then echo "ERROR: tar extract failed for $TAR_FILE"; exit 1; fi

  # [2/5] & [3/5] Run baseline fix if fix_prompt_file is provided
  if [[ -n "$FIX_PROMPT_FILE" ]]; then
    if [[ ! -f "$FIX_PROMPT_FILE" ]]; then
      echo "ERROR: Fix prompt file not found: $FIX_PROMPT_FILE"; exit 1
    fi
    FIX_PROMPT=$(<"$FIX_PROMPT_FILE")

    echo "[2/5] Initializing local git repo ..."
    pushd "$clone_dir" > /dev/null
    git init --quiet
    git config user.email "copilot-cli@github.com"
    git config user.name "Copilot CLI"
    git add -A
    git commit --quiet -m "Initial commit"

    echo "[3/5] Running baseline fix with model $BASELINE_MODEL ..."
    export GITHUB_TOKEN
    copilot -p "$FIX_PROMPT" \
      --allow-all \
      --no-ask-user \
      --share "${BASELINE_MODEL}.txt" \
      --log-level debug \
      --log-dir "." \
      --model "$BASELINE_MODEL"
    if [[ $? -ne 0 ]]; then echo "ERROR: copilot-cli baseline failed"; exit 1; fi

    # Rename log file to track.jsonl / track.log
    if [[ ! -f track.jsonl && ! -f track.log ]]; then
      LOG_FILE=$(find . -maxdepth 1 -name "process-*.jsonl" -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | head -1 | cut -d' ' -f2-)
      if [[ -n "$LOG_FILE" ]]; then
        mv "$LOG_FILE" track.jsonl
        echo "       Renamed $LOG_FILE -> track.jsonl"
      else
        LOG_FILE=$(find . -maxdepth 1 -name "process-*.log" -printf '%T@ %p\n' 2>/dev/null \
          | sort -rn | head -1 | cut -d' ' -f2-)
        if [[ -n "$LOG_FILE" ]]; then
          mv "$LOG_FILE" track.log
          echo "       Renamed $LOG_FILE -> track.log"
        else
          echo "       WARNING: No .jsonl or .log log file found"
        fi
      fi
    fi

    # Commit baseline changes locally (no push)
    git add -A
    if ! git diff --cached --quiet; then
      git commit --quiet -m "Baseline fix ($BASELINE_MODEL)"
      echo "       Baseline changes committed locally."
    else
      echo "       WARNING: No changes produced by baseline model."
    fi
    popd > /dev/null
  else
    echo "[2/5] (skipped – no fix prompt file provided)"
    echo "[3/5] (skipped – no baseline run)"
  fi

  # [4/5] Run copilot-cli for rubric generation
  echo "[4/5] Running copilot-cli for rubric generation ..."
  pushd "$clone_dir" > /dev/null
  export GITHUB_TOKEN
  copilot -p "$PROMPT" --allow-all --no-ask-user --model gpt-5.2 --allow-tool 'write' --allow-all-paths
  if [[ $? -ne 0 ]]; then echo "ERROR: copilot-cli failed for rubric generation"; exit 1; fi
  popd > /dev/null

  # Locate the generated rubric
  RUBRIC_FILE=$(find "$clone_dir" -name "rubric.json" -type f | head -n 1)

  dest_dir="${OUTPUT_DIR}/${CASE_NAME}"
  mkdir -p "$dest_dir"

  if [[ -n "${RUBRIC_FILE:-}" ]]; then
    echo "[5/5] Found rubric: $RUBRIC_FILE"
    echo "       Copying -> ${dest_dir}/rubric.json"
    cp "$RUBRIC_FILE" "${dest_dir}/rubric.json"
    if [[ $? -ne 0 ]]; then echo "ERROR: cp failed"; exit 1; fi
  else
    echo "[5/5] WARNING: rubric.json not found anywhere in $clone_dir"
  fi

  echo ""
  echo "=== Done. ==="

else
  echo "ERROR: Unknown mode '$MODE'"
  usage
fi
