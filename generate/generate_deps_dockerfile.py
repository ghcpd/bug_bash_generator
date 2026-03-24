#!/usr/bin/env python3
"""Generate a Dockerfile that installs all runtime + test dependencies for a repo.

Usage:
    python generate_deps_dockerfile.py --repo-dir /path/to/repo [--output Dockerfile] [--python python3]

The generated Dockerfile encapsulates every dependency-install strategy from
the original inline bash (uv.lock, pip editable, pyproject extras, PEP 735
dependency-groups, requirements-*.txt, tox.ini deps, setup.py extras) so that
``docker build`` produces an image with every dependency pre-installed.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Repo introspection helpers
# ---------------------------------------------------------------------------

def _find_requirements_files(repo: Path) -> list[str]:
    """Return repo-relative paths for all requirements*.txt files (depth ≤ 2)."""
    hits: list[str] = []
    for root, _dirs, files in os.walk(repo):
        depth = str(root).replace(str(repo), "").count(os.sep)
        if depth > 2:
            continue
        for f in sorted(files):
            if re.match(r"requirements.*\.txt$", f):
                rel = os.path.relpath(os.path.join(root, f), repo)
                if not rel.startswith(("build/", ".tox/")):
                    hits.append(rel)
    return sorted(set(hits))


def _parse_pyproject_optional_groups(repo: Path) -> list[str]:
    if tomllib is None:
        return []
    pp = repo / "pyproject.toml"
    if not pp.exists():
        return []
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except Exception:
        return []
    groups = list(data.get("project", {}).get("optional-dependencies", {}).keys())
    extras = data.get("tool", {}).get("setuptools", {}).get("extras_require", {})
    groups.extend(extras.keys())
    return sorted(set(groups))


def _parse_pyproject_dep_groups(repo: Path) -> list[str]:
    """PEP 735 dependency-groups → flat list of package specs."""
    if tomllib is None:
        return []
    pp = repo / "pyproject.toml"
    if not pp.exists():
        return []
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except Exception:
        return []
    dep_groups = data.get("dependency-groups", {})
    if not dep_groups:
        return []
    pkgs: list[str] = []
    for _name, items in dep_groups.items():
        for item in items:
            if isinstance(item, str):
                s = item.strip()
                if s and not s.startswith("{"):
                    pkgs.append(s)
            elif isinstance(item, dict) and "include-group" in item:
                ref = item["include-group"]
                for p in dep_groups.get(ref, []):
                    if isinstance(p, str):
                        s = p.strip()
                        if s and not s.startswith("{"):
                            pkgs.append(s)
    return sorted(set(pkgs))


def _parse_tox_deps(repo: Path) -> list[str]:
    tox_ini = repo / "tox.ini"
    if not tox_ini.exists():
        return []
    cfg = configparser.ConfigParser()
    try:
        cfg.read(str(tox_ini))
    except Exception:
        return []
    for sec in cfg.sections():
        if "testenv" in sec:
            raw = cfg.get(sec, "deps", fallback="")
            deps: list[str] = []
            for line in raw.strip().splitlines():
                line = line.strip()
                if line and not line.startswith(("-", "#")):
                    deps.append(line)
            return deps
    return []


def _parse_setup_py_extras(repo: Path) -> list[str]:
    setup_py = repo / "setup.py"
    if not setup_py.exists():
        return []
    import ast

    try:
        tree = ast.parse(setup_py.read_text(encoding="utf-8"))
    except Exception:
        return []
    extras: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "extras_require":
            if isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        extras.append(key.value)
    return sorted(set(extras))


# ---------------------------------------------------------------------------
# Python version detection
# ---------------------------------------------------------------------------

# Supported slim image tags, newest first
_SUPPORTED_VERSIONS = ["3.13", "3.12", "3.11", "3.10", "3.9", "3.8"]
_DEFAULT_VERSION = "3.11"


def _detect_python_version(repo: Path) -> str:
    """Best-effort detection of the Python version a project needs.

    Sources checked (first match wins on specificity; otherwise highest
    compatible version is picked):
      1. .python-version file  (exact, e.g. "3.12.1" → "3.12")
      2. pyproject.toml  requires-python  (e.g. ">=3.10" → pick highest compatible)
      3. setup.cfg  python_requires
      4. setup.py   python_requires string literal
      5. tox.ini    basepython / envlist  (e.g. py312 → "3.12")
      6. Fallback → _DEFAULT_VERSION
    """

    # 1. .python-version
    pv = repo / ".python-version"
    if pv.exists():
        raw = pv.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        m = re.match(r"(3\.\d+)", raw)
        if m and m.group(1) in _SUPPORTED_VERSIONS:
            return m.group(1)

    # 2. pyproject.toml requires-python
    if tomllib is not None:
        pp = repo / "pyproject.toml"
        if pp.exists():
            try:
                data = tomllib.loads(pp.read_text(encoding="utf-8"))
                rp = data.get("project", {}).get("requires-python", "")
                ver = _parse_requires_python(rp)
                if ver:
                    return ver
            except Exception:
                pass

    # 3. setup.cfg python_requires
    setup_cfg = repo / "setup.cfg"
    if setup_cfg.exists():
        import configparser as _cp
        cfg = _cp.ConfigParser()
        try:
            cfg.read(str(setup_cfg), encoding="utf-8")
            rp = cfg.get("options", "python_requires", fallback="")
            ver = _parse_requires_python(rp)
            if ver:
                return ver
        except Exception:
            pass

    # 4. setup.py python_requires literal
    sp = repo / "setup.py"
    if sp.exists():
        try:
            text = sp.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'python_requires\s*=\s*["\']([^"\']+)["\']', text)
            if m:
                ver = _parse_requires_python(m.group(1))
                if ver:
                    return ver
        except Exception:
            pass

    # 5. tox.ini basepython / envlist
    tox_ini = repo / "tox.ini"
    if tox_ini.exists():
        try:
            text = tox_ini.read_text(encoding="utf-8", errors="ignore")
            # basepython = python3.12
            m = re.search(r'basepython\s*=\s*python(3\.\d+)', text)
            if m and m.group(1) in _SUPPORTED_VERSIONS:
                return m.group(1)
            # envlist = py312, py311 → pick highest
            m = re.search(r'envlist\s*=\s*(.+)', text)
            if m:
                found = re.findall(r'py(3)(\d+)', m.group(1))
                candidates = [f"3.{minor}" for _, minor in found]
                for v in _SUPPORTED_VERSIONS:
                    if v in candidates:
                        return v
        except Exception:
            pass

    return _DEFAULT_VERSION


def _parse_requires_python(spec: str) -> str | None:
    """Extract the best Docker image version from a PEP 440 requires-python spec.

    Examples:
        '>=3.10'       → '3.13'  (highest supported that satisfies)
        '>=3.10,<3.13' → '3.12'
        '==3.11.*'     → '3.11'
    """
    spec = spec.strip()
    if not spec:
        return None

    # ==3.X or ==3.X.* → exact
    m = re.match(r'==\s*(3\.\d+)', spec)
    if m and m.group(1) in _SUPPORTED_VERSIONS:
        return m.group(1)

    # Parse >= lower bound and optional < upper bound
    lower = None
    upper = None  # exclusive upper bound
    for part in re.split(r'\s*,\s*', spec):
        m = re.match(r'>=\s*(3\.\d+)', part)
        if m:
            lower = m.group(1)
        m = re.match(r'<\s*(3\.\d+)', part)
        if m:
            upper = m.group(1)

    if lower is None:
        return None

    # Pick highest supported version that satisfies the bounds
    for v in _SUPPORTED_VERSIONS:
        if _ver_tuple(v) >= _ver_tuple(lower):
            if upper and _ver_tuple(v) >= _ver_tuple(upper):
                continue
            return v
    return None


def _ver_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


# ---------------------------------------------------------------------------
# Dockerfile generation
# ---------------------------------------------------------------------------

_COMMON_EXTRAS = ("test", "testing", "tests", "dev", "all", "async")


def generate_dockerfile(
    repo: Path,
    python_bin: str = "python3",
) -> str:
    """Return the full text of a Dockerfile for dependency installation."""

    has_uv_lock = (repo / "uv.lock").exists()
    has_pyproject = (repo / "pyproject.toml").exists()
    has_setup_py = (repo / "setup.py").exists()
    has_tox = (repo / "tox.ini").exists()

    python_version = _detect_python_version(repo)

    optional_groups = _parse_pyproject_optional_groups(repo)
    dep_group_pkgs = _parse_pyproject_dep_groups(repo)
    req_files = _find_requirements_files(repo)
    tox_deps = _parse_tox_deps(repo)
    setup_py_extras = _parse_setup_py_extras(repo) if not optional_groups else []

    PY = python_bin
    # Shell helper: try --user --break-system-packages, then --user, then bare
    def _pip_install(spec: str, *, extra_flags: str = "") -> str:
        flags = f"{extra_flags} " if extra_flags else ""
        return (
            f"{PY} -m pip install {flags}{spec} 2>/dev/null "
            f"|| {PY} -m pip install {spec} 2>/dev/null "
            f"|| true"
        )

    lines: list[str] = []

    # -- header
    lines.append("# Auto-generated — installs runtime + test dependencies")
    lines.append(f"FROM python:{python_version}-slim")
    lines.append("")
    lines.append("# OS-level tooling needed by many Python packages")
    lines.append("RUN apt-get update && apt-get install -y --no-install-recommends \\")
    lines.append("        git curl build-essential && \\")
    lines.append("    rm -rf /var/lib/apt/lists/*")
    lines.append("")
    lines.append("WORKDIR /repo")
    lines.append("COPY . /repo")
    lines.append("")

    # -- ensure pip & pytest
    lines.append("# Ensure pip + pytest are available")
    lines.append(f"RUN {PY} -m pip install --upgrade pip setuptools wheel && \\")
    lines.append(f"    {PY} -m pip install pytest>=7.0")
    lines.append("")

    # -- Strategy 0: uv.lock
    if has_uv_lock:
        lines.append("# Strategy 0: uv.lock — precise locked install")
        lines.append(f"RUN {PY} -m pip install uv 2>/dev/null || true")
        lines.append("RUN if command -v uv >/dev/null 2>&1 && \\")
        lines.append("       uv export --frozen --no-hashes --all-groups --all-extras -o /tmp/_uv_reqs.txt 2>/dev/null; then \\")
        lines.append(f"        {PY} -m pip install -r /tmp/_uv_reqs.txt 2>/dev/null || true; \\")
        lines.append("        rm -f /tmp/_uv_reqs.txt; \\")
        lines.append("    fi")
        lines.append(f"RUN {PY} -m pip install -e . 2>/dev/null \\")
        lines.append(f"    || SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 {PY} -m pip install -e . 2>/dev/null \\")
        lines.append("    || true")
        lines.append("")

    # -- Base package install (editable)
    lines.append("# Base package install (editable)")
    lines.append(f"RUN {PY} -m pip install -e . 2>/dev/null \\")
    lines.append(f"    || SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 {PY} -m pip install -e . 2>/dev/null \\")
    lines.append(f"    || {PY} -m pip install . 2>/dev/null \\")
    lines.append(f"    || SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 {PY} -m pip install . 2>/dev/null \\")
    lines.append("    || true")
    lines.append("")

    # -- Strategy 1a: pyproject.toml optional-dependencies
    if optional_groups:
        lines.append("# Strategy 1a: pyproject.toml optional-dependencies")
        for grp in optional_groups:
            lines.append(f"RUN {PY} -m pip install -e \".[{grp}]\" 2>/dev/null \\")
            lines.append(f"    || SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 {PY} -m pip install -e \".[{grp}]\" 2>/dev/null \\")
            lines.append("    || true")
        lines.append("")
    elif has_pyproject or has_setup_py:
        lines.append("# Strategy 1a: common extras (no optional-dependency groups detected)")
        for extra in _COMMON_EXTRAS:
            lines.append(f"RUN {PY} -m pip install -e \".[{extra}]\" 2>/dev/null \\")
            lines.append(f"    || SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 {PY} -m pip install -e \".[{extra}]\" 2>/dev/null \\")
            lines.append("    || true")
        lines.append("")

    # -- Strategy 1b: PEP 735 dependency-groups
    if dep_group_pkgs:
        lines.append("# Strategy 1b: PEP 735 dependency-group packages")
        # Install in a single RUN to keep layers small
        joined = " \\\n    ".join(f'"{pkg}"' for pkg in dep_group_pkgs)
        lines.append(f"RUN for pkg in {joined}; do \\")
        lines.append(f"        {PY} -m pip install \"$pkg\" 2>/dev/null || true; \\")
        lines.append("    done")
        lines.append("")

    # -- Strategy 2: requirements files
    if req_files:
        lines.append("# Strategy 2: requirements files")
        for rf in req_files:
            lines.append(f"RUN {PY} -m pip install -r \"{rf}\" 2>/dev/null || true")
        lines.append("")

    # -- Strategy 3: tox.ini deps
    if tox_deps:
        lines.append("# Strategy 3: tox.ini deps")
        for dep in tox_deps:
            lines.append(f"RUN {PY} -m pip install \"{dep}\" 2>/dev/null || true")
        lines.append("")

    # -- Strategy 4: setup.py extras_require
    if setup_py_extras:
        lines.append("# Strategy 4: setup.py extras_require")
        for extra in setup_py_extras:
            lines.append(f"RUN {PY} -m pip install -e \".[{extra}]\" 2>/dev/null || true")
        lines.append("")

    # -- Re-install project editable (ensure local dev version wins)
    lines.append("# Re-install project (editable, --no-deps) to ensure local version wins")
    lines.append(f"RUN {PY} -m pip install -e . --no-deps 2>/dev/null \\")
    lines.append(f"    || SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 {PY} -m pip install -e . --no-deps 2>/dev/null \\")
    lines.append("    || true")
    lines.append("")

    lines.append('CMD ["python3"]')
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSONL map: repo_slug → {dockerfile_hash, dockerfile_content, acr_image}
# ---------------------------------------------------------------------------

def _hash_dockerfile(content: str) -> str:
    """SHA-256 of the Dockerfile content (whitespace-normalised)."""
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def _map_lookup(map_path: Path, repo_slug: str, dockerfile_content: str) -> str | None:
    """Return the acr_image if repo_slug exists in the map with same hash, else None."""
    if not map_path.exists():
        return None
    target_hash = _hash_dockerfile(dockerfile_content)
    with open(map_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("repo_slug") == repo_slug and entry.get("dockerfile_hash") == target_hash:
                return entry.get("acr_image")
    return None


def _map_register(
    map_path: Path,
    repo_slug: str,
    dockerfile_content: str,
    acr_image: str,
) -> None:
    """Add or update an entry in the JSONL map (dedup by repo_slug)."""
    new_hash = _hash_dockerfile(dockerfile_content)
    entries: dict[str, dict] = {}

    # Load existing entries
    if map_path.exists():
        with open(map_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    slug = entry.get("repo_slug", "")
                    if slug:
                        entries[slug] = entry
                except json.JSONDecodeError:
                    continue

    # Upsert
    entries[repo_slug] = {
        "repo_slug": repo_slug,
        "dockerfile_hash": new_hash,
        "dockerfile_content": dockerfile_content,
        "acr_image": acr_image,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Atomic write
    map_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(map_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries.values():
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, str(map_path))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a Dockerfile that installs runtime + test deps for a repo."
    )
    parser.add_argument("--repo-dir", required=True, help="Path to the cloned repo")
    parser.add_argument("--output", default=None, help="Write Dockerfile to this path (default: stdout)")
    parser.add_argument("--python", default="python3", help="Python binary name inside the image")
    parser.add_argument("--map", default=None, help="Path to JSONL map file for image caching")
    parser.add_argument("--repo-slug", default=None, help="owner__repo slug (required with --map)")
    args = parser.parse_args()

    repo = Path(args.repo_dir).resolve()
    if not repo.is_dir():
        print(f"ERROR: repo-dir does not exist: {repo}", file=sys.stderr)
        return 1

    # Generate the Dockerfile content (always — needed for hash comparison)
    dockerfile = generate_dockerfile(repo, python_bin=args.python)

    # Check the map: if an image already exists for this repo + same Dockerfile, print it and exit
    if args.map and args.repo_slug:
        map_path = Path(args.map)
        cached = _map_lookup(map_path, args.repo_slug, dockerfile)
        if cached:
            # Print the cached image to stdout so the caller can use it
            print(cached)
            print(f"MAP_HIT: {args.repo_slug} → {cached}", file=sys.stderr)
            return 0
        print(f"MAP_MISS: {args.repo_slug} — will generate Dockerfile", file=sys.stderr)

    # Write the Dockerfile
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(dockerfile, encoding="utf-8")
        print(f"Wrote {out}", file=sys.stderr)
    else:
        sys.stdout.write(dockerfile)

    # Register a placeholder in the map (acr_image will be updated after build+push)
    if args.map and args.repo_slug:
        map_path = Path(args.map)
        local_tag = f"bugbash-deps-{args.repo_slug.lower()}"
        _map_register(map_path, args.repo_slug, dockerfile, local_tag)
        print(f"MAP_REGISTERED: {args.repo_slug} → {local_tag} (pending build)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
