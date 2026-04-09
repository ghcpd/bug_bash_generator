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
    "goldeneye-internal": "vscModelA-s360-x4",
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

Your task is to read the issue description, inspect the relevant code and tests, and implement the smallest correct patch.

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
1. **Explore**: Read and explore the local repo at {local_repo_dir}.
2. **Root Cause**: Analyze the problem statement to identify the root cause.
3. **Write a Verification Test**: Write a small focused test that reproduces the bug.
   - The test should FAIL on the current code and PASS after your fix.
   - Run the test to confirm it fails.
4. **Fix the Bug**: Edit the necessary files in {local_repo_dir}.
5. **Validate**: Run tests to confirm your fix works and nothing else breaks:
   docker exec {container_name} bash -c "cd {CONTAINER_WORKDIR} && {test_cmd}"
6. **Iterate** until all tests pass.
7. **Remove Your Verification Test**: Revert ALL test changes.
   Use `git checkout -- <test_file>` in {local_repo_dir} to revert each test file.
8. **Final Patch**: Run `cd {local_repo_dir} && git diff` to output the final patch
   (bug fix only, no test additions).

IMPORTANT:
- Only modify files necessary to fix the bug.
- The verification test is for development only — remove it before the final patch.
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
def extract_patch(local_repo_dir: str) -> str:
    subprocess.run(
        ["git", "add", "-N", "."],
        capture_output=True, timeout=10, cwd=local_repo_dir,
    )
    r = subprocess.run(
        ["git", "diff"],
        capture_output=True, text=True, timeout=60,
        cwd=local_repo_dir,
    )
    return r.stdout.strip()


# ─────────────────────────────────────────────────────────
# Artifact collection
# ─────────────────────────────────────────────────────────
def collect_artifacts(local_repo_dir: str, model: str,
                      share_path: str = None,
                      log_dir: str = None) -> dict[str, str]:
    """Collect copilot artifacts. Returns {filename: source_path}."""
    artifacts: dict[str, str] = {}

    # Search both repo dir and log_dir for copilot debug logs
    search_dirs = [local_repo_dir]
    if log_dir and os.path.isdir(log_dir):
        search_dirs.append(log_dir)

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

    # Check if track.log already exists
    for name in ("track.log", "track.jsonl"):
        for d in search_dirs:
            p = os.path.join(d, name)
            if os.path.isfile(p) and name not in artifacts:
                artifacts[name] = p

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
        patch = extract_patch(local_repo)
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
                shutil.copy2(src, os.path.join(model_out, name))
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

    # Create remote repo if it doesn't exist
    if remote_missing:
        repo_name = os.path.basename(
            git_url.rstrip("/").removesuffix(".git"))
        org = git_url.rstrip("/").removesuffix(".git").split("/")[-2]
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
    elif not remote_empty and not remote_missing:
        logging.info("[push] Remote has branches — skipping main push")

    # Set up local git
    ensure_git_repo(repo_base_dir)
    if _git("remote", "get-url", "origin").returncode == 0:
        _git("remote", "set-url", "origin", auth_url)
    else:
        _git("remote", "add", "origin", auth_url)

    # Push main if remote was empty/missing
    if remote_empty or remote_missing:
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

        # Copy artifacts from output directory
        if output_dir and instance_id:
            model_out = os.path.join(output_dir, instance_id, safe_model)
            if os.path.isdir(model_out):
                for fname in os.listdir(model_out):
                    if fname == "patch.diff":
                        continue
                    shutil.copy2(
                        os.path.join(model_out, fname),
                        os.path.join(repo_base_dir, fname),
                    )

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
    github_token: str = None,
    prompt_version: str = "v2",
    push: bool = False,
    github_org: str = None,
    git_url: str = None,
):
    _setup_logging()

    token = github_token or os.environ.get("GITHUB_TOKEN")
    if token:
        logging.info("[init] GITHUB_TOKEN: provided")
    else:
        logging.info("[init] GITHUB_TOKEN: not set")

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
    logging.info(f"  Repo extracted to: {repo_base}")

    # ── Step 3: Run models ──
    logging.info(f"\nLaunching {len(models)} model(s) with "
                 f"{max_workers} worker(s)...")
    results: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                run_model_worker, model, data, image_tag,
                repo_base, token, out_dir, prompt_version,
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
                "instance_id": instance_id,
                "model": model,
                "repo": data.get("repo", ""),
                "batch_version": data.get("batch_version", ""),
                "resolved": [],
                "unresolved": data.get("fail_to_pass", []),
                "still_passing": [],
                "regressed": [],
                "test_passed": False,
                "patch": "",
                "patch_size": 0,
                "labels": data.get("labels", {}),
                "generated_at": r.get("generated_at", ""),
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "error": "no patch produced",
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
            resolved = verify_result.get("resolved", [])
            unresolved = verify_result.get("unresolved", [])
            regressed = verify_result.get("regressed", [])
            r["test_passed"] = len(unresolved) == 0 and len(regressed) == 0
            r["verification"] = verify_result

            log(model, f"Resolved: {len(resolved)}/{len(data.get('fail_to_pass', []))}, "
                       f"Regressed: {len(regressed)}, "
                       f"Pass: {r['test_passed']}")
        except Exception as e:
            log(model, f"Verification error: {e}")
            r["test_passed"] = False
            r["verification"] = {"error": str(e)}

        # Write evaluation.jsonl to per-model eval directory
        eval_record = {
            "instance_id": instance_id,
            "model": model,
            "repo": data.get("repo", ""),
            "batch_version": data.get("batch_version", ""),
            "resolved": r.get("verification", {}).get("resolved", []),
            "unresolved": r.get("verification", {}).get("unresolved", []),
            "still_passing": r.get("verification", {}).get("still_passing", []),
            "regressed": r.get("verification", {}).get("regressed", []),
            "test_passed": r.get("test_passed"),
            "patch": patch,
            "patch_size": len(patch),
            "labels": data.get("labels", {}),
            "generated_at": r.get("generated_at", ""),
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "error": r.get("error"),
        }
        eval_path = os.path.join(model_eval_dir, "evaluation.jsonl")
        with open(eval_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(eval_record, ensure_ascii=False) + "\n")
        log(model, f"evaluation.jsonl → {eval_path}")

    # ── Step 4: Push if requested ──
    if push and token:
        url = git_url
        if not url and github_org:
            url = f"https://github.com/{github_org}/{instance_id}.git"
        if not url:
            logging.warning("[push] No git URL or org — skipping push")
        else:
            logging.info(f"\n[push] Pushing to {url} ...")
            push_results(
                repo_base, list(results.values()), url, token,
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
        "--model", action="append", required=True,
        help="Model(s) to use (repeatable: --model X --model Y)")
    parser.add_argument(
        "--output-dir", "-o", default=None,
        help="Evaluation output directory (default: $BASE/evaluation/)")
    parser.add_argument(
        "--workers", "-w", type=int, default=None,
        help="Max parallel workers (default: number of models)")
    parser.add_argument(
        "--github-token", default=None,
        help="GitHub token (default: $GITHUB_TOKEN)")
    parser.add_argument(
        "--prompt-version", default="v2", choices=["v1", "v2"],
        help="Prompt version")
    parser.add_argument(
        "--push", action="store_true",
        help="Push results to remote after all models complete")
    parser.add_argument(
        "--github-org", default=None,
        help="GitHub org for auto-creating repos (used with --push)")
    parser.add_argument(
        "--git-url", default=None,
        help="Explicit git URL to push to")

    args = parser.parse_args()

    run_pipeline(
        jsonl_file=args.jsonl_file,
        images_dir=args.images_dir,
        repos_dir=args.repos_dir,
        models=args.model,
        output_dir=args.output_dir,
        base_dir=args.base_dir,
        workers=args.workers,
        github_token=args.github_token,
        prompt_version=args.prompt_version,
        push=args.push,
        github_org=args.github_org,
        git_url=args.git_url,
    )


if __name__ == "__main__":
    main()
