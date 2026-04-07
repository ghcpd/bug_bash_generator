#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# push_if_empty.sh — Push local repo to remote only if remote is empty
#
# Usage:
#   ./push_if_empty.sh <CASE_NAME> <ARCHIVE_DIR> <CASE_DIR> <GITHUB_TOKEN>
#
# Arguments:
#   CASE_NAME     - Name of the case (used to derive file names)
#   ARCHIVE_DIR   - Directory containing tar.gz archives (-> CASE_NAME.tar.gz)
#   CASE_DIR      - Directory containing case JSON files (-> CASE_NAME.json)
#                   JSON format: {"name":"...","git_url":"..."} or an array
#   GITHUB_TOKEN  - GitHub OAuth token or fine-grained PAT
#
# Behavior:
#   - Derives archive path: ARCHIVE_DIR/CASE_NAME.tar.gz
#   - Derives JSON path: CASE_DIR/CASE_NAME.json
#   - Parses the JSON file to get case name(s) and git URL(s)
#   - Extracts the tar.gz archive to a temporary directory
#   - For each case: if remote has no branches (empty repo), push; otherwise skip
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/parse_cases.sh"

if [[ $# -lt 4 ]]; then
  echo "Usage: $0 <CASE_NAME> <ARCHIVE_DIR> <CASE_DIR> <GITHUB_TOKEN>"
  exit 1
fi

CASE_NAME="$1"
ARCHIVE_DIR="$2"
CASE_DIR="$3"
GITHUB_TOKEN="$4"

# Derive file paths from directories + case name
LOCAL_ARCHIVE="${ARCHIVE_DIR}/${CASE_NAME}.tar.gz"
CASE_JSON="${CASE_DIR}/${CASE_NAME}.json"

# Validate that the archive exists
if [[ ! -f "$LOCAL_ARCHIVE" ]]; then
  echo "ERROR: Archive not found: $LOCAL_ARCHIVE"
  exit 1
fi

# If case JSON doesn't exist, create one with default git_url under ghcpd org
if [[ ! -f "$CASE_JSON" ]]; then
  echo "Case JSON not found: $CASE_JSON — creating with defaults..."
  mkdir -p "$(dirname "$CASE_JSON")"
  cat > "$CASE_JSON" <<EOF
{"name":"${CASE_NAME}","git_url":"https://github.com/ghcpd/${CASE_NAME}.git"}
EOF
  echo "  Created $CASE_JSON"
fi

# Parse case JSON to get git URLs
parse_cases "$CASE_JSON"

for i in $(seq 0 $(($(get_case_count) - 1))); do
  CASE_NAME="${CASE_NAMES[$i]}"
  REPO_URL="${CASE_GIT_URLS[$i]}"

  echo "================================================================"
  echo "  Case: $CASE_NAME"
  echo "  URL:  $REPO_URL"
  echo "================================================================"

  # Extract tar.gz to a temporary directory
  TMP_DIR=$(mktemp -d)
  trap 'rm -rf "$TMP_DIR"' EXIT

  echo "Extracting $LOCAL_ARCHIVE..."
  tar -xzf "$LOCAL_ARCHIVE" -C "$TMP_DIR"

  # Determine the actual repo directory:
  # If the archive has a single top-level directory, use it; otherwise use TMP_DIR itself
  ENTRIES=( "$TMP_DIR"/* )
  if [[ ${#ENTRIES[@]} -eq 1 && -d "${ENTRIES[0]}" ]]; then
    LOCAL_DIR="${ENTRIES[0]}"
  else
    LOCAL_DIR="$TMP_DIR"
  fi

  # Build auth URL
  if [[ "$GITHUB_TOKEN" == gho_* ]]; then
    AUTH_URL="${REPO_URL/https:\/\//https:\/\/oauth2:${GITHUB_TOKEN}@}"
  else
    AUTH_URL="${REPO_URL/https:\/\//https:\/\/x-access-token:${GITHUB_TOKEN}@}"
  fi

  # Check if remote repo exists and has any branches
  REMOTE_REFS=$(git ls-remote --heads "$AUTH_URL" 2>&1 || true)

  if echo "$REMOTE_REFS" | grep -qi "repository not found\|does not exist\|not found"; then
    # Remote repo doesn't exist — try to create it
    REPO_NAME=$(basename "${REPO_URL%.git}")
    echo "Remote repository not found. Creating ghcpd/$REPO_NAME ..."
    HTTP_CODE=$(curl -s -o /tmp/gh_create_resp.json -w "%{http_code}" \
      -X POST \
      -H "Authorization: Bearer ${GITHUB_TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/orgs/ghcpd/repos" \
      -d "{\"name\":\"${REPO_NAME}\",\"private\":false,\"auto_init\":false}")
    if [[ "$HTTP_CODE" == "201" ]]; then
      echo "  ✔ Repository ghcpd/$REPO_NAME created successfully"
    else
      echo "  ✘ Failed to create repository (HTTP $HTTP_CODE):"
      cat /tmp/gh_create_resp.json
      echo
      rm -rf "$TMP_DIR"
      continue
    fi
  elif [[ -n "$REMOTE_REFS" ]] && ! echo "$REMOTE_REFS" | grep -qi "error\|fatal"; then
    echo "SKIP: Remote repository is not empty (branches already exist)."
    rm -rf "$TMP_DIR"
    continue
  fi

  echo "Remote repository is empty. Pushing local repo..."

  cd "$LOCAL_DIR"

  # Initialize git repo if not already one (archives may not include .git)
  if [[ ! -d .git ]]; then
    git init -b main
  fi

  # Set remote (replace if already exists)
  if git remote get-url origin &>/dev/null; then
    git remote set-url origin "$AUTH_URL"
  else
    git remote add origin "$AUTH_URL"
  fi

  # Configure identity if not set
  git config user.email &>/dev/null || git config user.email "copilot-cli@github.com"
  git config user.name &>/dev/null  || git config user.name "Copilot CLI"

  # Commit all if there are uncommitted changes
  git add -A
  if ! git diff --cached --quiet; then
    git commit -m "Initial commit"
  fi

  git push -u origin main

  echo "✓ Pushed $CASE_NAME to $REPO_URL"
  rm -rf "$TMP_DIR"
done

echo "Done. Processed $(get_case_count) case(s)."
