#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# run_bug_bash.sh — Unified entry point for the Bug Bash pipeline
#
# Replaces the old push_if_empty.sh + run_models.sh two-step workflow.
# Ensures dependencies are available, then delegates to run_pipeline.py.
#
# Usage:
#   ./run_bug_bash.sh <jsonl_file> \
#       --images-dir /path/to/images \
#       --repos-dir  /path/to/tar.gz \
#       --model GPT-5.2 [--model claude-opus-4.6] \
#       [--output-dir ./output] \
#       [--github-token TOKEN] \
#       [--push --github-org ghcpd]
#
# All arguments are forwarded to run_pipeline.py.
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Dependency checks ────────────────────────────────────────────────────────

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
      echo "Please install '$cmd' manually or run as root."
      exit 1
    fi
  fi
  export DEBIAN_FRONTEND=noninteractive

  case "$cmd" in
    git)
      $SUDO apt-get update -qq && $SUDO apt-get install -y -qq git
      ;;
    docker)
      echo "ERROR: Docker must be pre-installed."
      exit 1
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
    python3)
      $SUDO apt-get update -qq && $SUDO apt-get install -y -qq python3
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

ensure_command docker
ensure_command git
ensure_command python3
ensure_command copilot

# gh is only needed for --push (PR creation), check lazily
if echo "$@" | grep -q -- '--push'; then
  ensure_command gh
fi

# ── Run pipeline ─────────────────────────────────────────────────────────────

exec python3 "$SCRIPT_DIR/run_pipeline.py" "$@"
