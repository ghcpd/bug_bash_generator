#!/usr/bin/env python3
"""
Bug Bash Pipeline Runner — run copilot-cli for one or more models against a
repo inside a Docker container, then optionally push results to a remote.

Replaces the old push_if_empty.sh + run_models.sh two-step workflow with a
single pipeline that:
  1. Loads a Docker image from deps.tar
  2. Extracts the repo from a tar.gz archive
  3. Runs copilot-cli per model in isolated containers (parallel)
  4. Collects patches and artifacts
  5. Optionally pushes branches + creates PRs

Inputs:
  - JSONL file: one JSON record with case metadata (instance_id, repo,
    issue_text, test_command, setup_command, fail_to_pass, pass_to_pass …)
  - Images directory: contains <owner>/<repo_name>/deps.tar
  - Repos directory: contains <workspace_dir>.tar.gz (the source repo)

Usage:
    python3 run_pipeline.py <jsonl_file> \\
        --images-dir /path/to/images \\
        --repos-dir  /path/to/tar.gz \\
        --model GPT-5.2 --model claude-opus-4.6 \\
        [--output-dir ./output] \\
        [--github-token TOKEN] \\
        [--push --github-org ghcpd] \\
        [--workers 4] \\
        [--prompt-version v1|v2]
"""

import argparse
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import random
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────
CONTAINER_WORKDIR = "/repo"
FALLBACK_TOKENS_PATH = (
    "/mnt/batch/tasks/fsmounts/genaitextdatawu2_code/scripts/quansen/tokens.txt"
)
MODEL_ALIASES: dict[str, str] = {
}

_fallback_tokens_cache: list[str] | None = None
_fallback_tokens_lock = threading.Lock()
_log_lock = threading.Lock()


# ─────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────
def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )


def log(tag: str, msg: str):
    with _log_lock:
        logging.info(f"[{tag}] {msg}")


# ─────────────────────────────────────────────────────────
# Fallback tokens (for copilot auth retry)
# ─────────────────────────────────────────────────────────
def load_fallback_tokens() -> list[str]:
    global _fallback_tokens_cache
    with _fallback_tokens_lock:
        if _fallback_tokens_cache is not None:
            return list(_fallback_tokens_cache)
        try:
            with open(FALLBACK_TOKENS_PATH) as f:
                tokens = [line.strip() for line in f if line.strip()]
            _fallback_tokens_cache = tokens
            logging.info(f"[init] Loaded {len(tokens)} fallback token(s)")
            return list(tokens)
        except FileNotFoundError:
            _fallback_tokens_cache = []
            return []


def _is_auth_failure(returncode: int, stderr: str) -> bool:
    if returncode == 0:
        return False
    lo = stderr.lower()
    return any(kw in lo for kw in [
        "auth", "unauthorized", "401", "403", "forbidden",
        "token", "credential", "login", "permission denied",
        "bad credentials", "not logged in",
    ])


def resolve_model_id(model: str) -> str:
    return MODEL_ALIASES.get(model, model)


# ─────────────────────────────────────────────────────────
# JSONL
# ─────────────────────────────────────────────────────────
def load_jsonl(path: str) -> dict:
    """Load a single JSON record from a JSONL file."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                return json.loads(line)
    raise ValueError(f"Empty JSONL file: {path}")


# ─────────────────────────────────────────────────────────
# Docker image management
# ─────────────────────────────────────────────────────────
def load_docker_image(tar_path: str) -> str:
    """Load a Docker image from a tar archive. Returns the image tag."""
    # Read tag from manifest.json inside the tar
    tag_result = subprocess.run(
        ["tar", "xf", tar_path, "-O", "manifest.json"],
        capture_output=True, text=True, timeout=30,
    )
    image_tag = None
    if tag_result.returncode == 0:
        try:
            manifest = json.loads(tag_result.stdout)
            tags = manifest[0].get("RepoTags") or []
            if tags:
                image_tag = tags[0]
        except (json.JSONDecodeError, IndexError, KeyError):
            pass

    # Check if image is already loaded
    if image_tag:
        check = subprocess.run(
            ["docker", "image", "inspect", image_tag],
            capture_output=True, timeout=15,
        )
        if check.returncode == 0:
            logging.info(f"[init] Image already loaded: {image_tag}")
            return image_tag

    logging.info(f"[init] Loading image from {tar_path} ...")
    with open(tar_path, "rb") as f:
        result = subprocess.run(
            ["docker", "load"],
            stdin=f,
            capture_output=True, text=True, timeout=600,
        )
    if result.returncode != 0:
        raise RuntimeError(f"docker load failed: {result.stderr}")

    # Parse output if tag was not in manifest
    if not image_tag:
        m = re.search(r"Loaded image:\s*(.+)", result.stdout)
        if m:
            image_tag = m.group(1).strip()
        else:
            raise RuntimeError(
                f"Could not determine image tag from: {result.stdout}")

    logging.info(f"[init] Image loaded: {image_tag}")
    return image_tag


# ─────────────────────────────────────────────────────────
# Repo management
# ─────────────────────────────────────────────────────────
def extract_repo(tar_gz_path: str, dest_dir: str) -> str:
    """Extract a repo tar.gz archive. Returns path to the repo root."""
    os.makedirs(dest_dir, exist_ok=True)
    subprocess.run(
        ["tar", "xzf", tar_gz_path, "-C", dest_dir],
        check=True, capture_output=True, timeout=120,
    )
    entries = list(pathlib.Path(dest_dir).iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return str(entries[0])
    return dest_dir


def ensure_git_repo(path: str):
    """Initialize a git repo with an initial commit if one doesn't exist."""
    check = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True, timeout=10, cwd=path,
    )
    if check.returncode == 0:
        return
    subprocess.run(
        ["git", "init", "-b", "main"],
        capture_output=True, timeout=10, cwd=path,
    )
    subprocess.run(
        ["git", "config", "user.email", "copilot-cli@github.com"],
        capture_output=True, timeout=10, cwd=path,
    )
    subprocess.run(
        ["git", "config", "user.name", "Copilot CLI"],
        capture_output=True, timeout=10, cwd=path,
    )
    subprocess.run(
        ["git", "add", "-A"],
        capture_output=True, timeout=30, cwd=path,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit", "--allow-empty"],
        capture_output=True, timeout=30, cwd=path,
    )


# ─────────────────────────────────────────────────────────
# Container management
# ─────────────────────────────────────────────────────────
def start_container(image_tag: str, repo_dir: str,
                    workdir: str = CONTAINER_WORKDIR) -> str:
    """Start a container with the repo bind-mounted at *workdir*."""
    name = f"bugbash-{uuid.uuid4().hex[:8]}"
    r = subprocess.run(
        [
            "docker", "run", "-d",
            "--name", name,
            "-v", f"{os.path.abspath(repo_dir)}:{workdir}",
            "-w", workdir,
            "--entrypoint", "bash",
            image_tag, "-c", "sleep infinity",
        ],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Container start failed: {r.stderr}")
    return name


def stop_container(name: str):
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True, timeout=30,
    )


# ─────────────────────────────────────────────────────────
# Prompt builders
# ─────────────────────────────────────────────────────────
def build_prompt_v1(data: dict, local_repo_dir: str,
                    container_name: str) -> str:
    """Straightforward fix-and-test prompt."""
    repo = data.get("repo", "")
    issue_text = data.get("issue_text", "")
    setup_cmd = data.get("setup_command", "")
    test_cmd = data.get("test_command", "")

    return f"""You are an expert software engineer. Fix the bug described below.

## Repository
- Repo: {repo}
- Local repo: {local_repo_dir}  (read & edit files here)
- Docker container: {container_name}  (test here, repo mounted at {CONTAINER_WORKDIR})

## Problem Statement
{issue_text}

## Setup (if needed)
```bash
docker exec {container_name} bash -c "cd {CONTAINER_WORKDIR} && {setup_cmd}"
```

## Testing
Files are bind-mounted — edits in {local_repo_dir} are immediately visible
in the container. No sync step is needed.
```bash
docker exec {container_name} bash -c "cd {CONTAINER_WORKDIR} && {test_cmd}"
```

## Workflow
1. Explore the local repo at {local_repo_dir} to find the buggy code.
   Do NOT read test files (anything under `tests/`, `test/`, or files starting with `test_`). Locate the bug from the issue description and source code logic, not from test expectations.
2. Analyze the problem statement to identify the root cause.
3. Edit files in {local_repo_dir} to fix the bug.
4. Run tests to verify:
   docker exec {container_name} bash -c "cd {CONTAINER_WORKDIR} && {test_cmd}"
5. Iterate until tests pass.
6. When done, run `cd {local_repo_dir} && git diff` to output the final patch.

IMPORTANT:
- Only modify files necessary to fix the bug.
- Do NOT refactor or change unrelated code.
- Ensure ALL existing tests still pass.
"""


def build_prompt_v2(data: dict, local_repo_dir: str,
                    container_name: str) -> str:
    """Prompt with verification-test writing step."""
    repo = data.get("repo", "")
    issue_text = data.get("issue_text", "")
    setup_cmd = data.get("setup_command", "")
    test_cmd = data.get("test_command", "")

    return f"""You are an expert software engineer working in a repository to fix a reported issue.

Your task is to read the issue description, inspect the relevant source code, and implement the smallest correct patch.

## Repository
- Repo: {repo}
- Local repo: {local_repo_dir}  (read & edit files here)
- Docker container: {container_name}  (test here, repo mounted at {CONTAINER_WORKDIR})

## Problem Statement
{issue_text}

## Setup (if needed)
```bash
docker exec {container_name} bash -c "cd {CONTAINER_WORKDIR} && {setup_cmd}"
```

## Testing
Files are bind-mounted — edits in {local_repo_dir} are immediately visible
in the container. No sync step is needed.
```bash
docker exec {container_name} bash -c "cd {CONTAINER_WORKDIR} && {test_cmd}"
```

## Workflow
1. **Explore**: Read and explore the source code at {local_repo_dir}.
   Do NOT read test files (anything under `tests/`, `test/`, or files starting with `test_`). Locate the bug from the issue description and source code logic, not from test expectations.
2. **Root Cause**: Analyze the problem statement to identify the root cause.
3. **Fix the Bug**: Edit the necessary files in {local_repo_dir}.
4. **Validate**: Run tests to confirm your fix works and nothing else breaks:
   docker exec {container_name} bash -c "cd {CONTAINER_WORKDIR} && {test_cmd}"
5. **Iterate** until all tests pass.
6. **Final Patch**: Run `cd {local_repo_dir} && git diff` to output the final patch.

IMPORTANT:
- Only modify files necessary to fix the bug.
- Do NOT refactor or change unrelated code.
- Ensure ALL existing tests still pass.
"""


def build_prompt(data: dict, local_repo_dir: str,
                 container_name: str, version: str = "v2") -> str:
    builders = {"v1": build_prompt_v1, "v2": build_prompt_v2}
    fn = builders.get(version, build_prompt_v2)
    return fn(data, local_repo_dir, container_name)


# ─────────────────────────────────────────────────────────
# Copilot CLI invocation
# ─────────────────────────────────────────────────────────
def _run_copilot_once(cmd: list, model: str, cwd: str,
                      env: dict) -> tuple[str, bool]:
    """Run copilot once. Returns (output, is_auth_failure)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=1800, cwd=cwd, env=env,
        )
        result = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode != 0 and _is_auth_failure(proc.returncode, stderr):
            log(model, f"Auth failure: {stderr[-300:]}")
            return result, True
        if proc.returncode != 0 and not result:
            log(model, f"Copilot stderr:\n{stderr[-500:]}")
        return result, False
    except subprocess.TimeoutExpired:
        log(model, "Copilot timed out (30 min)")
        return "", False
    except Exception as e:
        log(model, f"Copilot error: {e}")
        return "", False


def call_copilot(prompt: str, model: str, local_repo_dir: str,
                 github_token: str = None,
                 share_path: str = None,
                 log_dir: str = None) -> str:
    """Write prompt to temp file, invoke copilot-cli, return output."""
    prompt_file = os.path.join(
        tempfile.gettempdir(), f"prompt_{uuid.uuid4().hex[:8]}.txt",
    )
    with open(prompt_file, "w") as f:
        f.write(prompt)

    backend_model = resolve_model_id(model)
    log(model, f"Prompt: {len(prompt)} chars → {prompt_file}")

    cmd = [
        "copilot",
        "-p", (f"Read the file {prompt_file} for your full task "
               "instructions. Follow them precisely."),
        "--model", backend_model,
        "--yolo",
        "--output-format", "text",
        "--add-dir", local_repo_dir,
        "--add-dir", "/tmp",
    ]
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        cmd.extend(["--log-dir", log_dir, "--log-level", "debug"])
    if share_path:
        cmd.extend(["--share", share_path])

    env = os.environ.copy()
    if github_token:
        env["GITHUB_TOKEN"] = github_token

    start = time.time()
    result = ""
    try:
        result, auth_failed = _run_copilot_once(
            cmd, model, local_repo_dir, env)

        if auth_failed:
            tokens = load_fallback_tokens()
            if github_token and github_token in tokens:
                tokens.remove(github_token)
            random.shuffle(tokens)
            for i, tok in enumerate(tokens):
                log(model, f"Retry with fallback token "
                    f"{i + 1}/{len(tokens)}...")
                env["GITHUB_TOKEN"] = tok
                result, auth_failed = _run_copilot_once(
                    cmd, model, local_repo_dir, env)
                if not auth_failed:
                    log(model, f"Fallback token {i + 1} succeeded")
                    break
            else:
                msg = ("All fallback tokens exhausted"
                       if tokens else "No fallback tokens")
                log(model, msg)
    finally:
        elapsed = time.time() - start
        if os.path.exists(prompt_file):
            os.remove(prompt_file)

    log(model, f"Copilot finished in {elapsed:.1f}s ({len(result)} chars)")
    return result


# ─────────────────────────────────────────────────────────
# Patch extraction
# ─────────────────────────────────────────────────────────
def extract_patch(local_repo_dir: str, baseline_commit: str = None) -> str:
    subprocess.run(
        ["git", "add", "-N", "."],
        capture_output=True, timeout=10, cwd=local_repo_dir,
    )
    # Compare against the buggy baseline commit to capture all model changes,
    # regardless of whether the model committed its fixes or not.
    diff_target = baseline_commit or "HEAD"
    r = subprocess.run(
        ["git", "diff", diff_target],
        capture_output=True, text=True, timeout=60,
        cwd=local_repo_dir,
    )
    patch = r.stdout
    # Ensure patch ends with newline (git apply requires it)
    if patch and not patch.endswith("\n"):
        patch += "\n"
    return patch


# ─────────────────────────────────────────────────────────
# Artifact collection
# ─────────────────────────────────────────────────────────
def collect_artifacts(local_repo_dir: str, model: str,
                      share_path: str = None,
                      log_dir: str = None) -> dict[str, str]:
    """Collect copilot artifacts. Returns {filename: source_path}.

    If *log_dir* (copilot_logs/) exists, all log files inside it are
    concatenated into a single ``track.log`` in the parent directory,
    then the log_dir is removed to avoid confusion across runs.
    """
    artifacts: dict[str, str] = {}

    # Merge all copilot log files into one track.log, then clean up
    if log_dir and os.path.isdir(log_dir):
        merged_log = os.path.join(os.path.dirname(log_dir), "track.log")
        log_files = sorted(
            pathlib.Path(log_dir).glob("*"),
            key=lambda p: p.stat().st_mtime,
        )
        with open(merged_log, "w", encoding="utf-8", errors="replace") as out:
            for lf in log_files:
                if lf.is_file():
                    out.write(f"\n{'=' * 60}\n")
                    out.write(f"=== {lf.name}\n")
                    out.write(f"{'=' * 60}\n")
                    try:
                        out.write(lf.read_text(encoding="utf-8", errors="replace"))
                    except Exception as e:
                        out.write(f"[Error reading {lf.name}: {e}]\n")
                    out.write("\n")
        artifacts["track.log"] = merged_log
        # Remove copilot_logs/ directory
        shutil.rmtree(log_dir, ignore_errors=True)
    else:
        # Fallback: search for individual log files
        search_dirs = [local_repo_dir]
        for search_dir in search_dirs:
            for pattern, target in [("process-*.log", "track.log"),
                                    ("process-*.jsonl", "track.jsonl")]:
                if target in artifacts:
                    continue
                matches = sorted(
                    pathlib.Path(search_dir).glob(pattern),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if matches:
                    artifacts[target] = str(matches[0])

    # Share file (session transcript)
    if share_path and os.path.isfile(share_path):
        artifacts[os.path.basename(share_path)] = share_path

    return artifacts


# ─────────────────────────────────────────────────────────
# Per-model worker (runs in its own thread)
# ─────────────────────────────────────────────────────────
def run_model_worker(
    model: str,
    data: dict,
    image_tag: str,
    repo_base_dir: str,
    github_token: str = None,
    output_dir: str = None,
    prompt_version: str = "v2",
    baseline_commit: str = None,
) -> dict:
    """Fully isolated worker for a single model."""
    instance_id = data.get("instance_id", "unknown")
    safe_model = model.replace("/", "_").replace(":", "_").replace(" ", "-")
    work_dir = tempfile.mkdtemp(
        prefix=f"bugbash_{instance_id}_{safe_model}_")
    container_name = None

    # Per-model output directory
    model_out = None
    if output_dir:
        model_out = os.path.join(output_dir, instance_id, safe_model)
        os.makedirs(model_out, exist_ok=True)

    try:
        # Copy repo for isolation
        local_repo = os.path.join(work_dir, "repo")
        shutil.copytree(repo_base_dir, local_repo, symlinks=True)
        ensure_git_repo(local_repo)
        log(model, f"Repo copied to {local_repo}")

        # Start container with bind mount
        container_name = start_container(image_tag, local_repo)
        log(model, f"Container: {container_name}")

        # Build prompt from JSONL issue_text
        prompt = build_prompt(
            data, local_repo, container_name, prompt_version)
        log(model, f"Prompt: {len(prompt)} chars")

        # Save prompt as artifact
        prompt_path = os.path.join(local_repo, "final_prompt.txt")
        with open(prompt_path, "w") as f:
            f.write(prompt)

        # Share file path
        share_path = os.path.join(local_repo, f"{safe_model}.txt")

        # Copilot log directory (inside output dir for persistence)
        copilot_log_dir = os.path.join(model_out, "copilot_logs") if model_out else None

        # Call copilot-cli
        log(model, "Invoking copilot-cli...")
        copilot_output = call_copilot(
            prompt, model, local_repo, github_token, share_path,
            log_dir=copilot_log_dir)
        if copilot_output:
            log(model, f"Output tail: {copilot_output[-300:]}")

        # Extract patch
        patch = extract_patch(local_repo, baseline_commit=baseline_commit)
        log(model, f"Patch: {len(patch)} chars, "
            f"{patch.count(chr(10))} lines")

        # Collect artifacts
        artifacts = collect_artifacts(
            local_repo, model, share_path, log_dir=copilot_log_dir)
        log(model, f"Artifacts: {list(artifacts.keys())}")

        # Save to output directory
        if model_out:
            with open(os.path.join(model_out, "patch.diff"), "w") as f:
                f.write(patch)
            for name, src in artifacts.items():
                dst = os.path.join(model_out, name)
                if os.path.abspath(src) != os.path.abspath(dst):
                    shutil.copy2(src, dst)
            if os.path.isfile(prompt_path):
                shutil.copy2(
                    prompt_path, os.path.join(model_out, "final_prompt.txt"))

        return {
            "model": model,
            "patch": patch,
            "artifacts": {
                name: (os.path.join(model_out, name) if model_out else src)
                for name, src in artifacts.items()
            },
            "test_passed": None,
            "generated_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": None,
            "work_dir": work_dir,
        }

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log(model, f"Error: {e}\n{tb}")
        patch = ""
        repo_path = os.path.join(work_dir, "repo")
        if os.path.isdir(repo_path):
            try:
                patch = extract_patch(repo_path)
            except Exception:
                pass
        return {
            "model": model,
            "patch": patch,
            "artifacts": {},
            "test_passed": False,
            "generated_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": str(e),
            "work_dir": work_dir,
        }

    finally:
        if container_name:
            stop_container(container_name)
            log(model, f"Container removed: {container_name}")


# ─────────────────────────────────────────────────────────
# Push to remote
# ─────────────────────────────────────────────────────────
def _build_auth_url(repo_url: str, token: str) -> str:
    if token.startswith("gho_"):
        return repo_url.replace(
            "https://", f"https://oauth2:{token}@")
    return repo_url.replace(
        "https://", f"https://x-access-token:{token}@")


def push_results(
    repo_base_dir: str,
    model_results: list[dict],
    git_url: str,
    github_token: str,
    output_dir: str = None,
    instance_id: str = None,
):
    """Push main branch + per-model branches to remote."""
    auth_url = _build_auth_url(git_url, github_token)

    def _git(*args):
        return subprocess.run(
            ["git"] + list(args),
            capture_output=True, text=True,
            timeout=60, cwd=repo_base_dir,
        )

    # Check remote state
    refs_proc = _git("ls-remote", "--heads", auth_url)
    refs = refs_proc.stdout.strip()
    refs_err = refs_proc.stderr.strip().lower()

    remote_missing = any(
        kw in refs_err for kw in ["not found", "does not exist"])
    remote_empty = not refs and not any(
        kw in refs_err for kw in ["error", "fatal"])

    repo_name = os.path.basename(
        git_url.rstrip("/").removesuffix(".git"))
    org = git_url.rstrip("/").removesuffix(".git").split("/")[-2]

    # If remote repo exists, delete it first for a clean push
    if not remote_missing:
        logging.info(f"[push] Deleting existing repo {org}/{repo_name} ...")
        r = subprocess.run(
            [
                "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "-X", "DELETE",
                "-H", f"Authorization: Bearer {github_token}",
                "-H", "Accept: application/vnd.github+json",
                f"https://api.github.com/repos/{org}/{repo_name}",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.stdout.strip() in ("204", "200"):
            logging.info(f"[push] Deleted {org}/{repo_name}")
            import time
            time.sleep(3)  # GitHub needs a moment after deletion
        else:
            logging.warning(
                f"[push] Failed to delete repo (HTTP {r.stdout.strip()})")

    # Create fresh repo
    logging.info(f"[push] Creating {org}/{repo_name} ...")
    r = subprocess.run(
        [
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            "-X", "POST",
            "-H", f"Authorization: Bearer {github_token}",
            "-H", "Accept: application/vnd.github+json",
            f"https://api.github.com/orgs/{org}/repos",
            "-d", json.dumps({
                "name": repo_name,
                "private": False,
                "auto_init": False,
            }),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if r.stdout.strip() == "201":
        logging.info(f"[push] Repository created: {org}/{repo_name}")
    else:
        logging.warning(
            f"[push] Failed to create repo (HTTP {r.stdout.strip()})")

    # Set up local git
    ensure_git_repo(repo_base_dir)
    if _git("remote", "get-url", "origin").returncode == 0:
        _git("remote", "set-url", "origin", auth_url)
    else:
        _git("remote", "add", "origin", auth_url)

    # Push main (repo is always freshly created)
    logging.info("[push] Pushing main branch ...")
    r = _git("push", "-u", "origin", "main")
    if r.returncode != 0:
        logging.warning(f"[push] main push failed: {r.stderr}")

    # Push per-model branches
    for result in model_results:
        model = result["model"]
        patch = result.get("patch", "")
        safe_model = model.replace(
            "/", "_").replace(":", "_").replace(" ", "-")
        branch = safe_model
        logging.info(f"[push] Creating branch: {branch}")

        _git("checkout", "main")
        _git("clean", "-fd")
        _git("checkout", "-B", branch)

        # Apply patch
        if patch:
            proc = subprocess.run(
                ["git", "apply", "--whitespace=nowarn"],
                input=patch, capture_output=True, text=True,
                timeout=30, cwd=repo_base_dir,
            )
            if proc.returncode != 0:
                logging.warning(
                    f"[push] Patch apply failed for {model}: "
                    f"{proc.stderr[:300]}")

        # Copy artifacts from output directory (files only, skip subdirs)
        if output_dir and instance_id:
            model_out = os.path.join(output_dir, instance_id, safe_model)
            if os.path.isdir(model_out):
                for fname in os.listdir(model_out):
                    src = os.path.join(model_out, fname)
                    if fname == "patch.diff" or os.path.isdir(src):
                        continue
                    shutil.copy2(src, os.path.join(repo_base_dir, fname))

        # Commit
        _git("add", "-A")
        # Force-add log/share files that .gitignore might exclude
        for ext in ("*.jsonl", "*.log", "*.txt"):
            for p in pathlib.Path(repo_base_dir).glob(ext):
                _git("add", "-f", str(p))

        r = _git("diff", "--cached", "--quiet")
        if r.returncode != 0:
            _git("commit", "-m", model)
        else:
            logging.warning(f"[push] No changes for {model}")

        r = _git("push", "-u", "origin", branch)
        if r.returncode != 0:
            logging.warning(
                f"[push] Push failed for {branch}: {r.stderr[:300]}")

    # Create PRs
    for result in model_results:
        model = result["model"]
        safe_model = model.replace(
            "/", "_").replace(":", "_").replace(" ", "-")
        branch = safe_model

        # Read prompt for PR body
        pr_body = "Bug fix by copilot-cli"
        if output_dir and instance_id:
            pf = os.path.join(
                output_dir, instance_id, safe_model, "final_prompt.txt")
            if os.path.isfile(pf):
                with open(pf) as f:
                    pr_body = f.read()[:65535]

        logging.info(f"[push] Creating PR for {branch} ...")
        repo_path = git_url.rstrip("/").removesuffix(".git")
        parts = repo_path.split("github.com/")[-1]

        env = os.environ.copy()
        env["GITHUB_TOKEN"] = github_token
        env["GH_REPO"] = parts

        r = subprocess.run(
            [
                "gh", "pr", "create",
                "--base", "main",
                "--head", branch,
                "--title", model,
                "--body", pr_body,
            ],
            capture_output=True, text=True,
            timeout=30, cwd=repo_base_dir, env=env,
        )
        if r.returncode != 0:
            logging.warning(
                f"[push] PR creation failed for {model}: {r.stderr[:300]}")
        else:
            logging.info(f"[push] PR created: {r.stdout.strip()}")

    _git("checkout", "main")


# ─────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────
def run_pipeline(
    jsonl_file: str,
    images_dir: str,
    repos_dir: str = None,
    models: list[str] = None,
    output_dir: str = None,
    base_dir: str = None,
    workers: int = None,
    copilot_token: str = None,
    push_token: str = None,
    prompt_version: str = "v2",
    push: bool = False,
    github_org: str = None,
    git_url: str = None,
):
    _setup_logging()

    cop_token = copilot_token or os.environ.get("COPILOT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    git_push_token = push_token or os.environ.get("GH_PUSH_TOKEN") or cop_token
    if cop_token:
        logging.info("[init] Copilot token: provided")
    else:
        logging.info("[init] Copilot token: not set")
    if git_push_token and git_push_token != cop_token:
        logging.info("[init] Push token: provided (separate)")
    elif git_push_token:
        logging.info("[init] Push token: using copilot token")

    logging.info(f"{'=' * 60}")
    logging.info("  Bug Bash Pipeline")
    logging.info(f"{'=' * 60}")
    logging.info(f"  JSONL:   {jsonl_file}")
    logging.info(f"  Models:  {', '.join(models)}")
    max_workers = workers or len(models)
    logging.info(f"  Workers: {max_workers}")

    # ── Load JSONL ──
    data = load_jsonl(jsonl_file)
    instance_id = data.get("instance_id", "unknown")
    repo = data.get("repo", "")
    logging.info(f"  Case:    {instance_id}")
    logging.info(f"  Repo:    {repo}")

    # ── Derive paths from --base-dir if provided ──
    if base_dir:
        if not repos_dir:
            repos_dir = os.path.join(base_dir, "tar.gz")
        if not output_dir:
            output_dir = os.path.join(base_dir, "evaluation")
        logging.info(f"  Base:    {base_dir}")

    if not repos_dir:
        logging.error("--repos-dir or --base-dir is required")
        sys.exit(1)

    # ── Resolve paths ──
    image_tar = os.path.join(images_dir, repo, "deps.tar")
    if not os.path.isfile(image_tar):
        logging.error(f"Image not found: {image_tar}")
        sys.exit(1)

    workspace_dir = data.get("workspace_dir", instance_id)
    repo_tar = os.path.join(repos_dir, f"{workspace_dir}.tar.gz")
    if not os.path.isfile(repo_tar):
        repo_tar = os.path.join(repos_dir, f"{instance_id}.tar.gz")
        if not os.path.isfile(repo_tar):
            logging.error(f"Repo archive not found: {repo_tar}")
            sys.exit(1)

    logging.info(f"  Image:   {image_tar}")
    logging.info(f"  Repo:    {repo_tar}")

    # Output directory
    out_dir = output_dir or os.path.dirname(os.path.abspath(jsonl_file))
    os.makedirs(out_dir, exist_ok=True)

    # ── Step 1: Load Docker image ──
    image_tag = load_docker_image(image_tar)

    # ── Step 2: Extract repo ──
    base_tmp = tempfile.mkdtemp(prefix=f"bugbash_{instance_id}_base_")
    repo_base = extract_repo(repo_tar, base_tmp)
    ensure_git_repo(repo_base)

    # Commit the current (buggy) state so that git diff only captures
    # model fixes, not the original mutation from the generate stage.
    subprocess.run(
        ["git", "add", "-A"],
        capture_output=True, timeout=30, cwd=repo_base,
    )
    commit_result = subprocess.run(
        ["git", "commit", "-m", "Buggy baseline (mutation applied)",
         "--allow-empty"],
        capture_output=True, text=True, timeout=30, cwd=repo_base,
    )
    logging.info(f"  Buggy baseline commit: {commit_result.stdout.strip()}")
    if commit_result.returncode != 0:
        logging.warning(f"  Commit stderr: {commit_result.stderr.strip()}")

    # Save the baseline commit hash for later diff comparison
    baseline_hash_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10, cwd=repo_base,
    )
    buggy_baseline_commit = baseline_hash_result.stdout.strip()
    logging.info(f"  Baseline hash: {buggy_baseline_commit}")

    # Verify: git diff should now be empty (everything committed)
    diff_check = subprocess.run(
        ["git", "diff", "--stat"],
        capture_output=True, text=True, timeout=10, cwd=repo_base,
    )
    if diff_check.stdout.strip():
        logging.warning(f"  WARNING: Uncommitted changes remain: {diff_check.stdout.strip()[:200]}")
    else:
        logging.info("  Repo clean — all buggy changes committed")

    logging.info(f"  Repo extracted to: {repo_base}")

    # ── Step 3: Run models ──
    logging.info(f"\nLaunching {len(models)} model(s) with "
                 f"{max_workers} worker(s)...")
    results: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                run_model_worker, model, data, image_tag,
                repo_base, cop_token, out_dir, prompt_version,
                buggy_baseline_commit,
            ): model
            for model in models
        }

        for future in as_completed(future_map):
            model = future_map[future]
            try:
                result = future.result()
                results[model] = result
                plen = len(result.get("patch", ""))
                log(model, f"Done (patch: {plen} chars)")
            except Exception as e:
                log(model, f"Unexpected error: {e}")
                results[model] = {
                    "model": model,
                    "patch": "",
                    "artifacts": {},
                    "test_passed": False,
                    "error": str(e),
                    "generated_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "work_dir": None,
                }

    # ── Step 3.5: Verify patches with test_runner ──
    from test_runner import run_tests  # noqa: E402 — deferred import

    logging.info(f"\n{'=' * 60}")
    logging.info("  Verifying patches...")
    logging.info(f"{'=' * 60}")

    for model in models:
        r = results.get(model, {})
        patch = r.get("patch", "")
        safe_model = model.replace("/", "_").replace(":", "_").replace(" ", "-")
        model_eval_dir = os.path.join(out_dir, instance_id, safe_model)
        os.makedirs(model_eval_dir, exist_ok=True)

        if not patch:
            log(model, "No patch — skipping verification")
            r["test_passed"] = False
            r["verification"] = {"error": "no patch produced"}
            # Write evaluation.jsonl even for failures
            eval_record = {
                "reponame": data.get("repo", ""),
                "instance_id": instance_id,
                "model": model,
                "batch_version": data.get("batch_version", ""),
                "issue_text": data.get("issue_text", ""),
                "fail_to_pass": data.get("fail_to_pass", []),
                "pass_to_pass": data.get("pass_to_pass", []),
                "labels": {"category": data.get("labels", {}).get("category", "")},
                "repo_description": data.get("repo_description", ""),
                "feature_description": data.get("feature_description", ""),
                "bug_description": data.get("bug_description", ""),
                "feature_direction": data.get("feature_direction", ""),
                "resolved": [],
                "unresolved": data.get("fail_to_pass", []),
                "still_passing": [],
                "regressed": [],
                "test_passed": False,
                "patch": "",
                "track": "",
                "cost_usd": 1,
            }
            eval_path = os.path.join(model_eval_dir, "evaluation.jsonl")
            with open(eval_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(eval_record, ensure_ascii=False) + "\n")
            log(model, f"evaluation.jsonl → {eval_path}")
            continue

        log(model, "Verifying patch...")
        try:
            verify_result = run_tests(
                jsonl_data=data,
                patch=patch,
                image_tag=image_tag,
                repo_dir=repo_base,
                collect=True,
            )

            # If run_tests returned an error (e.g. patch apply failed),
            # it returns {"passed": False, "error": "..."} without
            # resolved/unresolved keys. Treat as failure.
            if "error" in verify_result or verify_result.get("passed") is False:
                r["test_passed"] = False
                r["verification"] = verify_result
                log(model, f"Verification failed: {verify_result.get('error', 'patch did not apply')}")
            else:
                resolved = verify_result.get("resolved", [])
                unresolved = verify_result.get("unresolved", [])
                regressed = verify_result.get("regressed", [])
                ftp_count = len(data.get("fail_to_pass", []))
                r["test_passed"] = (
                    len(resolved) == ftp_count
                    and len(unresolved) == 0
                    and len(regressed) == 0
                )
                r["verification"] = verify_result

                log(model, f"Resolved: {len(resolved)}/{ftp_count}, "
                           f"Regressed: {len(regressed)}, "
                           f"Pass: {r['test_passed']}")
        except Exception as e:
            log(model, f"Verification error: {e}")
            r["test_passed"] = False
            r["verification"] = {"error": str(e)}

        # Read track.log content from artifacts
        track_content = ""
        track_path = r.get("artifacts", {}).get("track.log", "")
        if track_path and os.path.isfile(track_path):
            try:
                with open(track_path, "r", encoding="utf-8", errors="replace") as tf:
                    track_content = tf.read()
            except Exception:
                track_content = ""

        # Read share file (model session transcript) from artifacts
        share_content = ""
        share_key = f"{safe_model}.txt"
        share_path = r.get("artifacts", {}).get(share_key, "")
        if share_path and os.path.isfile(share_path):
            try:
                with open(share_path, "r", encoding="utf-8", errors="replace") as sf:
                    share_content = sf.read()
            except Exception:
                share_content = ""

        # Read final_prompt.txt from output directory
        prompt_content = ""
        prompt_path = os.path.join(model_eval_dir, "final_prompt.txt")
        if os.path.isfile(prompt_path):
            try:
                with open(prompt_path, "r", encoding="utf-8", errors="replace") as pf:
                    prompt_content = pf.read()
            except Exception:
                prompt_content = ""

        # Write evaluation.jsonl to per-model eval directory
        eval_record = {
            "reponame": data.get("repo", ""),
            "instance_id": instance_id,
            "model": model,
            "batch_version": data.get("batch_version", ""),
            "issue_text": data.get("issue_text", ""),
            "fail_to_pass": data.get("fail_to_pass", []),
            "pass_to_pass": data.get("pass_to_pass", []),
            "labels": {"category": data.get("labels", {}).get("category", "")},
            "repo_description": data.get("repo_description", ""),
            "feature_description": data.get("feature_description", ""),
            "bug_description": data.get("bug_description", ""),
            "feature_direction": data.get("feature_direction", ""),
            "resolved": r.get("verification", {}).get("resolved", []),
            "unresolved": r.get("verification", {}).get("unresolved", []),
            "still_passing": r.get("verification", {}).get("still_passing", []),
            "regressed": r.get("verification", {}).get("regressed", []),
            "test_passed": r.get("test_passed"),
            "prompt": prompt_content,
            "patch": patch,
            "track": track_content,
            "session": share_content,
            "cost_usd": 1,
        }
        eval_path = os.path.join(model_eval_dir, "evaluation.jsonl")
        with open(eval_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(eval_record, ensure_ascii=False) + "\n")
        log(model, f"evaluation.jsonl → {eval_path}")

    # ── Step 4: Push if requested ──
    if push and git_push_token:
        url = git_url
        if not url and github_org:
            url = f"https://github.com/{github_org}/{instance_id}.git"
        if not url:
            logging.warning("[push] No git URL or org — skipping push")
        else:
            logging.info(f"\n[push] Pushing to {url} ...")
            push_results(
                repo_base, list(results.values()), url, git_push_token,
                out_dir, instance_id)

    # ── Summary ──
    logging.info(f"\n{'=' * 60}")
    logging.info("  Pipeline Complete")
    logging.info(f"{'=' * 60}")
    for model in models:
        r = results.get(model, {})
        plen = len(r.get("patch", ""))
        passed = r.get("test_passed")
        status = "✓" if passed else ("✗" if passed is False else "?")
        err = f" (error: {r['error']})" if r.get("error") else ""
        logging.info(f"  {status} {model}: patch {plen} chars{err}")

    # ── Cleanup ──
    for r in results.values():
        wd = r.get("work_dir")
        if wd and os.path.isdir(wd):
            shutil.rmtree(wd, ignore_errors=True)
    shutil.rmtree(base_tmp, ignore_errors=True)

    logging.info(f"\nRemoving Docker image: {image_tag}")
    rm = subprocess.run(
        ["docker", "rmi", "-f", image_tag],
        capture_output=True, text=True, timeout=120,
    )
    if rm.returncode == 0:
        logging.info("  Image removed")
    else:
        logging.info(f"  Failed to remove: {rm.stderr.strip()}")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Bug Bash Pipeline Runner")
    parser.add_argument("jsonl_file", help="Path to JSONL task file")
    parser.add_argument(
        "--images-dir", required=True,
        help="Docker images directory (contains owner/repo/deps.tar)")
    parser.add_argument(
        "--base-dir", default=None,
        help="Base directory ($BASE). Derives --repos-dir as $BASE/tar.gz "
             "and evaluation output as $BASE/evaluation/")
    parser.add_argument(
        "--repos-dir", default=None,
        help="Repo archives directory (contains workspace_dir.tar.gz). "
             "Not needed if --base-dir is set.")
    parser.add_argument(
        "--model", action="append", default=None,
        help="Model(s) to use (repeatable: --model X --model Y)")
    parser.add_argument(
        "--models", default=None,
        help="Comma-separated list of models (alternative to --model). "
             "E.g. --models 'gpt-4.1,gpt-5.2,claude-opus-4.6'")
    parser.add_argument(
        "--output-dir", "-o", default=None,
        help="Evaluation output directory (default: $BASE/evaluation/)")
    parser.add_argument(
        "--workers", "-w", type=int, default=None,
        help="Max parallel workers (default: number of models)")
    parser.add_argument(
        "--copilot-token", default=None,
        help="Token for Copilot CLI auth (default: $COPILOT_GITHUB_TOKEN or $GITHUB_TOKEN)")
    parser.add_argument(
        "--push-token", default=None,
        help="Token for git push/PR operations (default: $GH_PUSH_TOKEN or copilot-token)")
    parser.add_argument(
        "--prompt-version", default="v2", choices=["v1", "v2"],
        help="Prompt version")
    parser.add_argument(
        "--no-push", action="store_true",
        help="Disable pushing results to remote (default: push is enabled)")
    parser.add_argument(
        "--github-org", default="ghcpd",
        help="GitHub org for auto-creating repos (default: ghcpd)")
    parser.add_argument(
        "--git-url", default=None,
        help="Explicit git URL to push to")

    args = parser.parse_args()

    # Resolve models: --models (comma-separated) takes precedence over --model
    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    elif args.model:
        models = args.model
    else:
        parser.error("Either --model or --models is required")

    run_pipeline(
        jsonl_file=args.jsonl_file,
        images_dir=args.images_dir,
        repos_dir=args.repos_dir,
        models=models,
        output_dir=args.output_dir,
        base_dir=args.base_dir,
        workers=args.workers,
        copilot_token=args.copilot_token,
        push_token=args.push_token,
        prompt_version=args.prompt_version,
        push=not args.no_push,
        github_org=args.github_org,
        git_url=args.git_url,
    )


if __name__ == "__main__":
    main()
