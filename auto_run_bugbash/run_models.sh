#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# run_models.sh — Run copilot-cli for ONE model against a repo
#
# Designed to be called by ADF ForEach → Azure Batch Custom Activity,
# once per model.
#
# Usage:
#   ./run_models.sh <CASE_NAME> <CASE_DIR> <PROMPT_DIR> <MODEL_NAME> <GITHUB_TOKEN>
#
# Arguments:
#   CASE_NAME     - Name of the case (used to derive file names)
#   CASE_DIR      - Directory containing case JSON files (-> CASE_NAME.json)
#   PROMPT_DIR    - Directory containing prompt files (-> CASE_NAME.md)
#   MODEL_NAME    - Human-friendly model name (e.g. "GPT-5.2")
#   GITHUB_TOKEN  - GitHub OAuth token (gho_*) or fine-grained PAT (github_pat_*)
#                   Note: Classic PATs (ghp_*) are NOT supported by copilot-cli
#
# Example:
#   ./run_models.sh \
#     "my_case" \
#     "./cases" \
#     "./prompts" \
#     "GPT-5.2" \
#     "github_pat_xxxxxxxxxxxx"
# =============================================================================

# --- Auto-install missing dependencies ----------------------------------------
ensure_command() {
  local cmd="$1"
  if command -v "$cmd" &>/dev/null; then
    return 0
  fi
  echo "Command '$cmd' not found. Installing..."

  # Determine if we can use sudo (non-interactive only)
  local SUDO=""
  if [[ $(id -u) -ne 0 ]]; then
    if sudo -n true 2>/dev/null; then
      SUDO="sudo -n"
    else
      echo "ERROR: '$cmd' is not installed and sudo requires a password."
      echo "Please install '$cmd' manually or run this script as root."
      exit 1
    fi
  fi
  export DEBIAN_FRONTEND=noninteractive

  case "$cmd" in
    git)
      $SUDO apt-get update -qq && $SUDO apt-get install -y -qq git
      ;;
    gh)
      curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | $SUDO dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | $SUDO tee /etc/apt/sources.list.d/github-cli.list > /dev/null
      $SUDO apt-get update -qq && $SUDO apt-get install -y -qq gh
      ;;
    copilot)
      curl -fsSL https://gh.io/copilot-install | bash
      export PATH="$HOME/.local/bin:$PATH"
      ;;
    *)
      echo "ERROR: Don't know how to install '$cmd'"
      exit 1
      ;;
  esac
  if ! command -v "$cmd" &>/dev/null; then
    echo "ERROR: Failed to install '$cmd'"
    exit 1
  fi
  echo "'$cmd' installed successfully."
}

ensure_command git
ensure_command gh
ensure_command copilot

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/parse_cases.sh"

# --- Validate arguments -------------------------------------------------------
if [[ $# -lt 5 ]]; then
  echo "Usage: $0 <CASE_NAME> <CASE_DIR> <PROMPT_DIR> <MODEL_NAME> <GITHUB_TOKEN>"
  exit 1
fi
CASE_NAME="$1"
CASE_DIR="$2"
PROMPT_DIR="$3"
MODEL_NAME="$4"
export GITHUB_TOKEN="$5"

# --- Derive file paths from directories + case name ---------------------------
CASE_JSON="${CASE_DIR}/${CASE_NAME}.json"
PROMPT_FILE="${PROMPT_DIR}/${CASE_NAME}.md"

# --- Parse case JSON to get git URL -------------------------------------------
parse_cases "$CASE_JSON"
REPO_URL="${CASE_GIT_URLS[0]}"

# --- Validate prompt file exists -----------------------------------------------
if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "ERROR: Prompt file not found: $PROMPT_FILE"
  exit 1
fi

# --- Validate token format ----------------------------------------------------
# copilot-cli only supports OAuth tokens (gho_*) and fine-grained PATs (github_pat_*)
# Classic PATs (ghp_*) are NOT supported by copilot-cli
if [[ "$GITHUB_TOKEN" == ghp_* ]]; then
  echo "ERROR: Classic personal access tokens (ghp_*) are not supported by copilot-cli."
  echo "Please use an OAuth token (gho_*) or a fine-grained PAT (github_pat_*)."
  exit 1
fi

# Branch name: replace spaces with hyphens
BRANCH_NAME=$(echo "$MODEL_NAME" | tr ' ' '-')

echo "================================================================"
echo "  Case:   $CASE_NAME"
echo "  Model:  $MODEL_NAME"
echo "  Branch: $BRANCH_NAME"
echo "  Repo:   $REPO_URL"
echo "================================================================"

# --- Clone the repo using token for push auth ---------------------------------
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

# Build auth URL based on token format:
#   OAuth tokens (gho_*)              -> https://oauth2:<token>@github.com/...
#   Fine-grained PATs (github_pat_*)  -> https://x-access-token:<token>@github.com/...
if [[ "$GITHUB_TOKEN" == gho_* ]]; then
  AUTH_URL="${REPO_URL/https:\/\//https:\/\/oauth2:${GITHUB_TOKEN}@}"
else
  AUTH_URL="${REPO_URL/https:\/\//https:\/\/x-access-token:${GITHUB_TOKEN}@}"
fi

echo "Cloning $REPO_URL ..."
git clone "$AUTH_URL" "$WORK_DIR/repo"
cd "$WORK_DIR/repo"

# --- Configure git identity (required for commits) ----------------------------
git config user.email "copilot-cli@github.com"
git config user.name "Copilot CLI"

# --- Create branch from main -------------------------------------------------
git checkout main
git checkout -b "$BRANCH_NAME"

# --- Run copilot-cli ----------------------------------------------------------
echo "Running copilot-cli with model $MODEL_NAME ..."
copilot -p "$PROMPT_FILE" \
  --allow-all \
  --no-ask-user \
  --share "${MODEL_NAME}.txt" \
  --log-level debug \
  --log-dir "." \
  --model "$MODEL_NAME"

# Ensure log file is named track.jsonl (or track.log for newer CLI versions)
if [[ ! -f track.jsonl && ! -f track.log ]]; then
  # Try .jsonl first (older CLI versions)
  LOG_FILE=$(find . -maxdepth 1 -name "process-*.jsonl" -newer .git -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | head -1 | cut -d' ' -f2-)
  if [[ -n "$LOG_FILE" ]]; then
    mv "$LOG_FILE" track.jsonl
    echo "Renamed $LOG_FILE -> track.jsonl"
  else
    # Fall back to .log (newer CLI versions)
    LOG_FILE=$(find . -maxdepth 1 -name "process-*.log" -newer .git -printf '%T@ %p\n' 2>/dev/null \
      | sort -rn | head -1 | cut -d' ' -f2-)
    if [[ -n "$LOG_FILE" ]]; then
      mv "$LOG_FILE" track.log
      echo "Renamed $LOG_FILE -> track.log"
    else
      echo "WARNING: No .jsonl or .log log file found"
    fi
  fi
fi

# --- Save prompt to file ------------------------------------------------------
cp "$PROMPT_FILE" final_prompt.txt

# --- Commit & push ------------------------------------------------------------
git add -A
# Force-add log and share files in case .gitignore excludes them
git add -f *.jsonl *.log *.txt 2>/dev/null || true
if git diff --cached --quiet; then
  echo "ERROR: No changes produced by copilot-cli for model $MODEL_NAME"
  exit 1
fi

git commit -m "$MODEL_NAME"

git push -u origin "$BRANCH_NAME"

# --- Create pull request ------------------------------------------------------
gh pr create \
  --base main \
  --head "$BRANCH_NAME" \
  --title "$MODEL_NAME" \
  --body-file "$PROMPT_FILE"

echo "✓ Completed: $MODEL_NAME"
