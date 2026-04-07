#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# evaluate_models.sh — Use copilot-cli to evaluate all model branches
#
# After run_models.sh creates branches (one per model), this script clones
# the repo, collects diffs from each model branch vs main, and asks
# copilot-cli to evaluate them against a prompt and rubric.
#
# Outputs:
#   <OUTPUT_DIR>/evaluation.json — JSON scores per model
#   <OUTPUT_DIR>/evaluation.md   — Markdown report
#
# Usage:
#   ./evaluate_models.sh <CASE_NAME> <CASE_DIR> <PROMPT_DIR> <RUBRIC_DIR> <GITHUB_TOKEN> <OUTPUT_DIR> [EVAL_MODEL]
#
# Arguments:
#   CASE_NAME     - Name of the case (used to derive file names)
#   CASE_DIR      - Directory containing case JSON files (-> CASE_NAME.json)
#   PROMPT_DIR    - Directory containing prompt files (-> CASE_NAME.md)
#   RUBRIC_DIR    - Directory containing rubric files (-> CASE_NAME.json)
#   GITHUB_TOKEN  - GitHub OAuth token (gho_*) or fine-grained PAT (github_pat_*)
#   OUTPUT_DIR    - Directory to write evaluation.json and evaluation.md
#   EVAL_MODEL    - (Optional) Model for evaluation, default: claude-opus-4.6
#
# Example:
#   ./evaluate_models.sh \
#     "my_case" \
#     "./cases" \
#     "./prompts" \
#     "./rubrics" \
#     "github_pat_xxxxxxxxxxxx" \
#     "./results" \
#     "claude-opus-4.6"
# =============================================================================

# --- Auto-install missing dependencies ----------------------------------------
ensure_command() {
  local cmd="$1"
  if command -v "$cmd" &>/dev/null; then
    return 0
  fi
  echo "Command '$cmd' not found. Installing..."

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
ensure_command copilot

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/parse_cases.sh"

# --- Validate arguments -------------------------------------------------------
if [[ $# -lt 6 ]]; then
  echo "Usage: $0 <CASE_NAME> <CASE_DIR> <PROMPT_DIR> <RUBRIC_DIR> <GITHUB_TOKEN> <OUTPUT_DIR> [EVAL_MODEL]"
  exit 1
fi

CASE_NAME="$1"
CASE_DIR="$2"
PROMPT_DIR="$3"
RUBRIC_DIR="$4"
export GITHUB_TOKEN="$5"
OUTPUT_DIR="$6"
EVAL_MODEL="${7:-claude-opus-4.6}"

# --- Derive file paths from directories + case name ---------------------------
CASE_JSON="${CASE_DIR}/${CASE_NAME}.json"
PROMPT_FILE="${PROMPT_DIR}/${CASE_NAME}.md"
RUBRIC_FILE="${RUBRIC_DIR}/${CASE_NAME}/rubric.json"

# --- Parse case JSON to get git URL -------------------------------------------
parse_cases "$CASE_JSON"
REPO_URL="${CASE_GIT_URLS[0]}"

# Validate token format
if [[ "$GITHUB_TOKEN" == ghp_* ]]; then
  echo "ERROR: Classic personal access tokens (ghp_*) are not supported by copilot-cli."
  echo "Please use an OAuth token (gho_*) or a fine-grained PAT (github_pat_*)."
  exit 1
fi

# Validate input files exist
for f in "$PROMPT_FILE" "$RUBRIC_FILE"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: File not found: $f"
    exit 1
  fi
done

# Create output directory with case name isolation
OUTPUT_DIR="${OUTPUT_DIR}/${CASE_NAME}"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

PROMPT_CONTENT=$(cat "$PROMPT_FILE")
RUBRIC_CONTENT=$(cat "$RUBRIC_FILE")

echo "================================================================"
echo "  Evaluation Model: $EVAL_MODEL"
echo "  Repo:             $REPO_URL"
echo "================================================================"

# --- Clone the repo -----------------------------------------------------------
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

if [[ "$GITHUB_TOKEN" == gho_* ]]; then
  AUTH_URL="${REPO_URL/https:\/\//https:\/\/oauth2:${GITHUB_TOKEN}@}"
else
  AUTH_URL="${REPO_URL/https:\/\//https:\/\/x-access-token:${GITHUB_TOKEN}@}"
fi

echo "Cloning $REPO_URL ..."
git clone "$AUTH_URL" "$WORK_DIR/repo"
cd "$WORK_DIR/repo"

# --- Discover model branches --------------------------------------------------
# Get all remote branches except main/HEAD
BRANCHES=()
while IFS= read -r branch; do
  branch="${branch#origin/}"
  if [[ "$branch" != "main" && "$branch" != "HEAD" && ! "$branch" =~ ^HEAD ]]; then
    BRANCHES+=("$branch")
  fi
done < <(git branch -r --format='%(refname:short)' | sed 's|^origin/||' | sort -u)

if [[ ${#BRANCHES[@]} -eq 0 ]]; then
  echo "ERROR: No model branches found (only main exists)."
  exit 1
fi

echo "Found ${#BRANCHES[@]} model branch(es):"
printf '  - %s\n' "${BRANCHES[@]}"

# --- Collect diffs for each branch --------------------------------------------
DIFFS_DIR="$WORK_DIR/diffs"
mkdir -p "$DIFFS_DIR"

for branch in "${BRANCHES[@]}"; do
  echo "Collecting diff for branch: $branch ..."
  git --no-pager diff "origin/main...origin/$branch" > "$DIFFS_DIR/${branch}.diff" 2>/dev/null || true
done

# --- Build the evaluation prompt ----------------------------------------------
EVAL_PROMPT_FILE="$WORK_DIR/eval_prompt.md"

cat > "$EVAL_PROMPT_FILE" <<'HEADER'
# Model Evaluation Task

You are an expert code reviewer evaluating multiple AI models' solutions to the same programming task.

## Original Task Prompt
HEADER

echo '```' >> "$EVAL_PROMPT_FILE"
echo "$PROMPT_CONTENT" >> "$EVAL_PROMPT_FILE"
echo '```' >> "$EVAL_PROMPT_FILE"

cat >> "$EVAL_PROMPT_FILE" <<'RUBRIC_HEADER'

## Evaluation Rubric
RUBRIC_HEADER

echo '```' >> "$EVAL_PROMPT_FILE"
echo "$RUBRIC_CONTENT" >> "$EVAL_PROMPT_FILE"
echo '```' >> "$EVAL_PROMPT_FILE"

cat >> "$EVAL_PROMPT_FILE" <<'MODELS_HEADER'

## Model Solutions (diffs against main branch)

Below are the diffs produced by each model. Evaluate each one according to the rubric.

MODELS_HEADER

for branch in "${BRANCHES[@]}"; do
  echo "### Model: $branch" >> "$EVAL_PROMPT_FILE"
  echo '```diff' >> "$EVAL_PROMPT_FILE"
  cat "$DIFFS_DIR/${branch}.diff" >> "$EVAL_PROMPT_FILE"
  echo '```' >> "$EVAL_PROMPT_FILE"
  echo "" >> "$EVAL_PROMPT_FILE"
done

cat >> "$EVAL_PROMPT_FILE" <<'INSTRUCTIONS'

## Output Requirements

Produce TWO outputs:

### 1. evaluation.json
A JSON file with this structure:
```json
{
  "models": [
    {
      "name": "<branch/model name>",
      "total_score": <number>,
      "rubric_scores": {
        "<criterion>": { "score": <number>, "reason": "<brief explanation>" }
      },
      "strengths": ["..."],
      "weaknesses": ["..."]
    }
  ],
  "ranking": ["<best model>", "<second best>", "..."],
  "summary": "<one paragraph overall comparison>"
}
```

### 2. evaluation.md
A human-readable markdown report containing:
- A summary table with models ranked by total score
- Per-model detailed breakdown with rubric scores and commentary
- Overall recommendation

Please create both files (evaluation.json and evaluation.md) in the repository root.
INSTRUCTIONS

# --- Run copilot-cli for evaluation -------------------------------------------
echo "Running copilot-cli evaluation with model $EVAL_MODEL ..."
copilot -p "$(cat "$EVAL_PROMPT_FILE")" \
  --allow-all \
  --no-ask-user \
  --share "evaluation-session.txt" \
  --model "$EVAL_MODEL"

# --- Copy outputs to OUTPUT_DIR -----------------------------------------------
for f in evaluation.json evaluation.md evaluation-session.txt; do
  if [[ -f "$f" ]]; then
    cp "$f" "$OUTPUT_DIR/$f"
    echo "✓ $f -> $OUTPUT_DIR/$f"
  else
    echo "WARNING: $f was not created"
  fi
done

echo "✓ Evaluation complete"
