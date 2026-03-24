#!/usr/bin/env python3
"""
SWE-bench Instance Audit Script
================================
Independent third-party validator for spot-checking BenchInstance integrity.

Audit Dimensions (7 layers):
  L1  JSON completeness — required fields present, enum values valid
  L2  Snapshot integrity — workspace dir exists, no .git leak, installable
  L3  Patch applicability — gold_patch can be applied by git apply and reversed
  L4  FAIL->PASS reproduction — tests must FAIL on buggy code, PASS after patch
  L5  Patch-issue coherence — patch-modified files/functions match issue_text
  L6  Label reasonableness — difficulty/localization/category cross-validated with patch size
  L7  Anti-cheat — no answer leakage in snapshot (no .git, no gold_patch content)

Usage:
    python audit_step03_validate_instance.py <instance.json>                     # audit single instance
    python audit_step03_validate_instance.py <instance.json> --level L4          # audit up to L4
    python audit_step03_validate_instance.py *.json --sample 2 --seed 42         # random sample 2
    python audit_step03_validate_instance.py *.json --json-output audit.json     # output results as JSON

Human Review Protocol (for instances that pass automated audit):
  +---------------------------------------------------------------------+
  |  1. Random sampling:  --sample N --seed <random>                    |
  |  2. Automated audit:  run L1-L7, record results                    |
  |  3. Human review (for L4-passed instances, check these 4 points):  |
  |     [H1] Does issue_text read like a real bug report?              |
  |     [H2] Is gold_patch a minimal fix? Any extra changes?           |
  |     [H3] Do tests detect the actual bug, not unrelated behavior?   |
  |     [H4] Are labels (category/difficulty) subjectively reasonable? |
  |  4. Verdict:  2+ H-checks fail -> mark instance as "untrusted"    |
  +---------------------------------------------------------------------+

Drift Detection Strategy:
  - Time-window comparison: first 10 vs last 10 instances, L6 pass rate trend
  - Category-stratified sampling: at least 2 instances per category
  - Generator-auditor separation: generate with agent A, audit with agent B or human
  - Periodic re-audit: same instance re-audited after interval, results must be stable
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


# ── Constants ────────────────────────────────────────────────────────────────

REQUIRED_FIELDS = [
    "instance_id", "repo", "base_commit", "workspace_dir", "source",
    "issue_text", "patches", "fail_to_pass", "labels", "quality",
]

VALID_CATEGORIES = {
    "Logic & Algorithm", "Data Handling & Transformation",
    "API & Interface Contract", "Error Handling & Edge Cases",
    "Infrastructure & Tooling", "Performance & Efficiency",
    "Security & Access Control", "Configuration & Environment",
    "Type & Validation", "Documentation & Naming",
}

VALID_DIFFICULTIES = {"L1", "L2", "L3", "L4"}

VALID_LOCALIZATIONS = {"explicit", "implicit", "cross_file", "cross_module"}

VALID_CONTEXT_DEPS = {"self_contained", "local_context", "global_context"}

VALID_TEST_MODALITIES = {
    "unit_test", "integration_test", "regression_test", "performance_test",
}

VALID_SOURCES = {"real_extraction", "synthetic_mutation"}


# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    level: str
    name: str
    passed: bool
    detail: str = ""


@dataclass
class AuditReport:
    instance_id: str
    json_path: str
    results: list[CheckResult] = field(default_factory=list)

    def add(self, level: str, name: str, passed: bool, detail: str = ""):
        self.results.append(CheckResult(level, name, passed, detail))

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    def summary(self) -> str:
        lines = [f"\n{'='*70}", f"  Audit: {self.instance_id}", f"{'='*70}"]
        for r in self.results:
            mark = "PASS" if r.passed else "FAIL"
            sym = "[+]" if r.passed else "[-]"
            line = f"  {sym} {r.level} {r.name}"
            if r.detail:
                line += f"  --  {r.detail}"
            lines.append(line)
        total = len(self.results)
        passed = total - self.failed_count
        verdict = "ALL PASSED" if self.passed else f"{self.failed_count} FAILED"
        lines.append(f"\n  Result: {passed}/{total} checks passed  [{verdict}]")
        lines.append("=" * 70)
        return "\n".join(lines)


# ── L1: JSON Completeness ───────────────────────────────────────────────────

def audit_l1_json(data: dict, report: AuditReport) -> bool:
    """Check JSON field completeness and enum value validity."""
    ok = True

    # Required fields
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        report.add("L1", "required_fields", False, f"missing: {missing}")
        ok = False
    else:
        report.add("L1", "required_fields", True)

    # patches.gold_patch exists
    patches = data.get("patches", {})
    if not patches.get("gold_patch"):
        report.add("L1", "gold_patch_exists", False, "patches.gold_patch is empty")
        ok = False
    else:
        report.add("L1", "gold_patch_exists", True)

    # fail_to_pass non-empty
    ftp = data.get("fail_to_pass", [])
    if not ftp:
        report.add("L1", "fail_to_pass_nonempty", False, "fail_to_pass is an empty list")
        ok = False
    else:
        report.add("L1", "fail_to_pass_nonempty", True, f"{len(ftp)} test(s)")

    # source valid
    source = data.get("source", "")
    if source not in VALID_SOURCES:
        report.add("L1", "source_valid", False, f"'{source}' not in {VALID_SOURCES}")
        ok = False
    else:
        report.add("L1", "source_valid", True)

    # labels enum validation
    labels = data.get("labels", {})
    for field_name, valid_set, label in [
        ("category", VALID_CATEGORIES, "category"),
        ("difficulty", VALID_DIFFICULTIES, "difficulty"),
        ("localization", VALID_LOCALIZATIONS, "localization"),
        ("context_dependency", VALID_CONTEXT_DEPS, "context_dependency"),
        ("test_modality", VALID_TEST_MODALITIES, "test_modality"),
    ]:
        val = labels.get(field_name)
        if val and val not in valid_set:
            report.add("L1", f"label_{label}", False,
                        f"'{val}' not in {valid_set}")
            ok = False
        elif val:
            report.add("L1", f"label_{label}", True)

    return ok


# ── L2: Snapshot Integrity ───────────────────────────────────────────────────

def audit_l2_snapshot(data: dict, base_dir: Path, report: AuditReport) -> bool:
    """Check workspace directory exists, no .git, contains installable Python package."""
    ok = True
    ws_dir = base_dir / data.get("workspace_dir", "")

    if not ws_dir.is_dir():
        report.add("L2", "workspace_exists", False, f"directory not found: {ws_dir}")
        return False
    report.add("L2", "workspace_exists", True)

    # No .git
    git_dir = ws_dir / ".git"
    if git_dir.exists():
        report.add("L2", "no_git_leak", False, ".git directory exists -- anti-cheat failed")
        ok = False
    else:
        report.add("L2", "no_git_leak", True)

    # pyproject.toml or setup.py exists
    has_pyproject = (ws_dir / "pyproject.toml").exists()
    has_setup = (ws_dir / "setup.py").exists()
    if not has_pyproject and not has_setup:
        report.add("L2", "installable", False,
                    "neither pyproject.toml nor setup.py found")
        ok = False
    else:
        report.add("L2", "installable", True)

    # fail_to_pass test files exist
    ftp = data.get("fail_to_pass", [])
    for test_id in ftp:
        test_file = test_id.split("::")[0]
        if not (ws_dir / test_file).exists():
            report.add("L2", "test_file_exists", False,
                        f"test file not found: {test_file}")
            ok = False
            break
    else:
        if ftp:
            report.add("L2", "test_file_exists", True)

    return ok


# ── L3: Patch Applicability ─────────────────────────────────────────────────

def _git_init_and_apply(ws_dir: Path, patch_file: Path,
                       check_only: bool = False,
                       reverse: bool = False) -> subprocess.CompletedProcess:
    """Temporarily git init in directory, use git apply for patch (cross-platform)."""
    if not (ws_dir / ".git").exists():
        subprocess.run(["git", "init"], cwd=str(ws_dir),
                       capture_output=True, text=True)
        subprocess.run(["git", "add", "."], cwd=str(ws_dir),
                       capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.name=audit", "-c", "user.email=a@b.c",
             "commit", "-m", "init", "--allow-empty"],
            cwd=str(ws_dir), capture_output=True, text=True,
        )

    cmd = ["git", "apply"]
    if check_only:
        cmd.append("--check")
    if reverse:
        cmd.append("-R")
    cmd.append(str(patch_file))
    return subprocess.run(cmd, cwd=str(ws_dir),
                          capture_output=True, text=True)


def audit_l3_patch(data: dict, base_dir: Path, report: AuditReport) -> bool:
    """Verify gold_patch can be applied correctly and is reversible in a temp copy."""
    ws_dir = base_dir / data.get("workspace_dir", "")
    gold_patch = data.get("patches", {}).get("gold_patch", "")

    if not gold_patch:
        report.add("L3", "patch_applicable", False, "no gold_patch")
        return False

    with tempfile.TemporaryDirectory() as tmp:
        tmp_ws = Path(tmp) / "workspace"
        try:
            shutil.copytree(ws_dir, tmp_ws)
        except Exception as e:
            report.add("L3", "patch_applicable", False, f"copy failed: {e}")
            return False

        patch_file = Path(tmp) / "gold.patch"
        patch_file.write_text(gold_patch, encoding="utf-8")

        # dry-run
        result = _git_init_and_apply(tmp_ws, patch_file, check_only=True)
        if result.returncode != 0:
            report.add("L3", "patch_applicable", False,
                        result.stderr[:300])
            return False

        report.add("L3", "patch_applicable", True)

        # actual apply
        _git_init_and_apply(tmp_ws, patch_file)

        # verify reversibility
        rev = _git_init_and_apply(tmp_ws, patch_file,
                                  check_only=True, reverse=True)
        report.add("L3", "patch_reversible", rev.returncode == 0,
                    "" if rev.returncode == 0 else rev.stderr[:200])
        return rev.returncode == 0


# ── L4: FAIL->PASS Reproduction ─────────────────────────────────────────────

def audit_l4_quality_gate(data: dict, base_dir: Path,
                          report: AuditReport) -> bool:
    """Reproduce FAIL->PASS in an isolated environment:
       1. Install buggy version -> run fail_to_pass tests -> must FAIL
       2. Apply gold_patch -> run tests -> must PASS
    """
    ws_dir = base_dir / data.get("workspace_dir", "")
    gold_patch = data.get("patches", {}).get("gold_patch", "")
    ftp = data.get("fail_to_pass", [])

    if not ftp or not gold_patch:
        report.add("L4", "quality_gate", False, "missing fail_to_pass or gold_patch")
        return False

    def _extract_non_passed_tests(stdout: str, ftp_set: set) -> set:
        """Extract all FAILED or ERROR test IDs from pytest output.
        For file-level ERRORs (e.g. import failures), mark all ftp tests
        under that file as failed."""
        non_pass = set()
        for line in stdout.splitlines():
            for status in ("FAILED", "ERROR"):
                if status in line:
                    match = re.search(rf"{status}\s+(\S+)", line)
                    if match:
                        tid = match.group(1)
                        non_pass.add(tid)
                        # File-level ERROR (e.g. "ERROR tests/test_x.py", no ::)
                        # -> mark all ftp tests under that file
                        if "::" not in tid:
                            for ftp_id in ftp_set:
                                if ftp_id.startswith(tid.rstrip()):
                                    non_pass.add(ftp_id)
        return non_pass

    with tempfile.TemporaryDirectory() as tmp:
        tmp_ws = Path(tmp) / "workspace"
        try:
            shutil.copytree(ws_dir, tmp_ws)
        except Exception as e:
            report.add("L4", "quality_gate", False, f"copy failed: {e}")
            return False

        env = os.environ.copy()
        env["SETUPTOOLS_SCM_PRETEND_VERSION"] = "0.0.0"

        # If old-style setup.py depends on setuptools/pkg_resources, install setuptools first
        if (tmp_ws / "setup.py").exists():
            setup_content = (tmp_ws / "setup.py").read_text(
                encoding="utf-8", errors="ignore")
            if "setuptools" in setup_content:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install",
                     "setuptools", "--quiet"],
                    cwd=str(tmp_ws), capture_output=True, text=True, env=env,
                )

        # Install buggy version
        install = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
            cwd=str(tmp_ws), capture_output=True, text=True, env=env,
        )
        if install.returncode != 0:
            report.add("L4", "buggy_install", False,
                        install.stderr[:300])
            return False

        # Extract unique test files
        test_files = list({t.split("::")[0] for t in ftp})

        # Step 1: run tests -> expect FAIL
        fail_result = subprocess.run(
            [sys.executable, "-m", "pytest"] + test_files +
            ["-v", "--tb=line", "--rootdir", str(tmp_ws),
             "-o", "addopts="],
            cwd=str(tmp_ws), capture_output=True, text=True, env=env,
            timeout=120,
        )

        # Check that all fail_to_pass tests FAILED or ERROR
        ftp_set = set(ftp)
        buggy_non_pass = _extract_non_passed_tests(fail_result.stdout, ftp_set)

        not_failed = ftp_set - buggy_non_pass

        if not_failed:
            diag = f"these tests did not FAIL/ERROR on buggy code: {not_failed}"
            tail = "\n".join(fail_result.stdout.splitlines()[-15:])
            if fail_result.stderr:
                tail += "\nSTDERR: " + fail_result.stderr[:200]
            report.add("L4", "buggy_tests_fail", False,
                        f"{diag}\n  pytest output tail:\n{tail}")
            return False
        report.add("L4", "buggy_tests_fail", True,
                    f"all {len(ftp)} test(s) FAIL on buggy code")

        # Step 2: apply patch via git apply
        patch_file = Path(tmp) / "gold.patch"
        patch_file.write_text(gold_patch, encoding="utf-8")

        apply_result = _git_init_and_apply(tmp_ws, patch_file)

        if apply_result.returncode != 0:
            report.add("L4", "patch_apply", False,
                        apply_result.stderr[:300])
            return False

        # Reinstall
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
            cwd=str(tmp_ws), capture_output=True, text=True, env=env,
        )

        # Step 3: run tests -> expect PASS
        pass_result = subprocess.run(
            [sys.executable, "-m", "pytest"] + test_files +
            ["-v", "--tb=line", "--rootdir", str(tmp_ws),
             "-o", "addopts="],
            cwd=str(tmp_ws), capture_output=True, text=True, env=env,
            timeout=120,
        )

        # Check that all fail_to_pass tests now PASS
        post_non_pass = _extract_non_passed_tests(pass_result.stdout, ftp_set)

        still_failing = ftp_set & post_non_pass
        if still_failing:
            report.add("L4", "fixed_tests_pass", False,
                        f"still FAIL/ERROR after patch: {still_failing}")
            return False

        report.add("L4", "fixed_tests_pass", True,
                    f"all {len(ftp)} test(s) PASS after patch")
        return True


# ── L5: Patch-Issue Coherence ────────────────────────────────────────────────

def audit_l5_consistency(data: dict, report: AuditReport) -> bool:
    """Cross-validate that patch-modified files/function names appear in issue_text."""
    ok = True
    gold_patch = data.get("patches", {}).get("gold_patch", "")
    issue_text = data.get("issue_text", "")

    # Extract changed file names from patch
    changed_files = re.findall(r"^diff --git a/(\S+)", gold_patch, re.MULTILINE)
    if not changed_files:
        changed_files = re.findall(r"^---\s+a/(\S+)", gold_patch, re.MULTILINE)

    # Extract changed symbols (function/class names) from patch
    changed_symbols = set()
    for line in gold_patch.splitlines():
        # unified diff @@ lines often contain function names
        m = re.search(r"@@.*@@\s*(?:def|class)\s+(\w+)", line)
        if m:
            changed_symbols.add(m.group(1))
        # +/- lines containing function calls
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            for sym in re.findall(r"\b(\w{4,})\b", line):
                changed_symbols.add(sym)

    # Check if issue mentions relevant files or symbols
    issue_lower = issue_text.lower()
    file_mentioned = False
    for f in changed_files:
        basename = os.path.basename(f)
        if basename.lower() in issue_lower:
            file_mentioned = True
            break
        for part in f.split("/"):
            if len(part) > 3 and part.lower() in issue_lower:
                file_mentioned = True
                break

    # Find symbols mentioned in both issue and patch
    common_symbols = {s for s in changed_symbols
                      if len(s) > 3 and s.lower() in issue_lower}

    if not file_mentioned and not common_symbols:
        report.add("L5", "patch_issue_coherence", False,
                    f"patch modifies {changed_files}, but issue mentions none of the "
                    f"related files or symbols (sample symbols: {list(changed_symbols)[:10]})")
        ok = False
    else:
        mentions = []
        if file_mentioned:
            mentions.append("file name")
        if common_symbols:
            mentions.append(f"symbols {list(common_symbols)[:5]}")
        report.add("L5", "patch_issue_coherence", True,
                    f"issue mentions {', '.join(mentions)}")

    return ok


# ── L6: Label Reasonableness ────────────────────────────────────────────────

def audit_l6_labels(data: dict, report: AuditReport) -> bool:
    """Cross-validate labels against patch content for consistency."""
    ok = True
    labels = data.get("labels", {})
    gold_patch = data.get("patches", {}).get("gold_patch", "")
    num_lines = data.get("num_lines_changed", 0)
    num_files = data.get("num_files_changed", 0)

    difficulty = labels.get("difficulty", "")

    # Rough rules: L1 typically <= 5 lines, L4 typically > 50 lines
    if difficulty == "L1" and num_lines > 20:
        report.add("L6", "difficulty_vs_size", False,
                    f"labeled L1 but {num_lines} lines changed -- L1 is typically <= 20 lines")
        ok = False
    elif difficulty == "L4" and num_lines < 5:
        report.add("L6", "difficulty_vs_size", False,
                    f"labeled L4 but only {num_lines} lines changed -- L4 is typically > 5 lines")
        ok = False
    else:
        report.add("L6", "difficulty_vs_size", True,
                    f"difficulty={difficulty}, lines={num_lines}")

    # localization vs num_files
    loc = labels.get("localization", "")
    if loc in ("cross_file", "cross_module") and num_files <= 1:
        report.add("L6", "localization_vs_files", False,
                    f"labeled {loc} but only {num_files} file(s) changed")
        ok = False
    elif loc in ("explicit", "implicit") and num_files > 5:
        report.add("L6", "localization_vs_files", False,
                    f"labeled {loc} but {num_files} files changed -- consider cross_file?")
        ok = False
    else:
        report.add("L6", "localization_vs_files", True,
                    f"localization={loc}, files={num_files}")

    # Performance category <-> test modality
    category = labels.get("category", "")
    test_mod = labels.get("test_modality", "")
    if category == "Performance & Efficiency" and test_mod == "unit_test":
        report.add("L6", "category_vs_test_modality", False,
                    "Performance category typically uses performance_test, not unit_test")
        ok = False
    else:
        report.add("L6", "category_vs_test_modality", True)

    # sub_type only valid for synthetic
    source = data.get("source", "")
    mutation = data.get("mutation_type")
    if source == "synthetic_mutation" and not mutation:
        report.add("L6", "synthetic_has_mutation_type", False,
                    "synthetic instance should include mutation_type field")
        ok = False
    elif source == "synthetic_mutation":
        report.add("L6", "synthetic_has_mutation_type", True)

    return ok


# ── L7: Anti-Cheat ──────────────────────────────────────────────────────────

def audit_l7_anti_cheat(data: dict, base_dir: Path,
                        report: AuditReport) -> bool:
    """Check snapshot for answer leakage."""
    ok = True
    ws_dir = base_dir / data.get("workspace_dir", "")
    gold_patch = data.get("patches", {}).get("gold_patch", "")

    if not ws_dir.is_dir():
        report.add("L7", "anti_cheat", False, "workspace does not exist, skipping")
        return False

    # 1. No .git
    if (ws_dir / ".git").exists():
        report.add("L7", "no_git_history", False, ".git directory exists")
        ok = False
    else:
        report.add("L7", "no_git_history", True)

    # 2. Check files for gold_patch added lines (prevent answer leakage)
    #    Extract "+" lines from patch, only check sufficiently long and unique lines.
    #    Short lines (e.g. "@lru_cache", "import os") cause false positives.
    #    Special handling for swap-type mutations: if a line appears in both
    #    + and - sides, it's a position swap, not a new addition.
    #    Finding it in the buggy snapshot is expected.
    added_lines = set()
    removed_lines = set()
    for line in gold_patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            stripped = line[1:].strip()
            if len(stripped) > 30 and not stripped.startswith(("#", "//")):
                added_lines.add(stripped)
        elif line.startswith("-") and not line.startswith("---"):
            stripped = line[1:].strip()
            if len(stripped) > 5:
                removed_lines.add(stripped)

    # Exclude swap lines: lines appearing in both + and - sides
    truly_new_lines = [l for l in added_lines if l not in removed_lines]

    leak_found = False
    for root, dirs, files in os.walk(ws_dir):
        # Skip __pycache__, .pytest_cache, etc.
        dirs[:] = [d for d in dirs if not d.startswith((".", "__"))]
        for fname in files:
            if not fname.endswith((".py", ".cfg", ".toml", ".txt", ".md")):
                continue
            fpath = os.path.join(root, fname)
            try:
                content = open(fpath, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            for added in truly_new_lines:
                if added in content:
                    rel = os.path.relpath(fpath, ws_dir)
                    report.add("L7", "no_answer_in_snapshot", False,
                               f"gold_patch line '{added[:60]}...' found in {rel}")
                    leak_found = True
                    break
            if leak_found:
                break
        if leak_found:
            break

    if not leak_found:
        report.add("L7", "no_answer_in_snapshot", True,
                    f"checked {len(truly_new_lines)} truly-new line(s) "
                    f"(excluded {len(added_lines) - len(truly_new_lines)} swap line(s)), "
                    f"no leakage found in snapshot")

    # 3. No JSON metadata leakage
    for root, dirs, files in os.walk(ws_dir):
        dirs[:] = [d for d in dirs if not d.startswith((".", "__"))]
        for fname in files:
            if fname.endswith(".json"):
                fpath = os.path.join(root, fname)
                try:
                    content = open(fpath, encoding="utf-8").read()
                    if "gold_patch" in content or "fail_to_pass" in content:
                        rel = os.path.relpath(fpath, ws_dir)
                        report.add("L7", "no_metadata_leak", False,
                                   f"JSON file {rel} contains gold_patch/fail_to_pass")
                        ok = False
                        break
                except Exception:
                    pass

    if ok and not leak_found:
        report.add("L7", "no_metadata_leak", True)

    return ok and not leak_found


# ── Main Audit Flow ──────────────────────────────────────────────────────────

LEVEL_ORDER = ["L1", "L2", "L3", "L4", "L5", "L6", "L7"]

LEVEL_FUNCS = {
    "L1": lambda d, b, r: audit_l1_json(d, r),
    "L2": lambda d, b, r: audit_l2_snapshot(d, b, r),
    "L3": lambda d, b, r: audit_l3_patch(d, b, r),
    "L4": lambda d, b, r: audit_l4_quality_gate(d, b, r),
    "L5": lambda d, b, r: audit_l5_consistency(d, r),
    "L6": lambda d, b, r: audit_l6_labels(d, r),
    "L7": lambda d, b, r: audit_l7_anti_cheat(d, b, r),
}


def audit_instance(json_path: str, max_level: str = "L7") -> AuditReport:
    """Run audit on a single BenchInstance JSON."""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    base_dir = Path(json_path).parent
    report = AuditReport(
        instance_id=data.get("instance_id", "unknown"),
        json_path=json_path,
    )

    max_idx = LEVEL_ORDER.index(max_level)

    for level in LEVEL_ORDER[:max_idx + 1]:
        func = LEVEL_FUNCS[level]
        try:
            func(data, base_dir, report)
        except Exception as e:
            report.add(level, "exception", False, f"audit exception: {e}")

    return report


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SWE-bench Instance Audit Script")
    parser.add_argument("json_files", nargs="+",
                        help="One or more BenchInstance JSON file paths")
    parser.add_argument("--level", default="L7",
                        choices=LEVEL_ORDER,
                        help="Audit up to this level (default: L7, all)")
    parser.add_argument("--sample", type=int, default=0,
                        help="Random sample count (0=all)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed (for reproducible sampling)")
    parser.add_argument("--json-output", type=str, default=None,
                        help="Output audit results as JSON file")
    args = parser.parse_args()

    files = args.json_files

    # Random sampling
    if args.sample > 0 and args.sample < len(files):
        rng = random.Random(args.seed)
        files = rng.sample(files, args.sample)
        print(f"\nSampled {args.sample}/{len(args.json_files)} instances "
              f"(seed={args.seed})")

    reports: list[AuditReport] = []
    for json_path in files:
        if not os.path.isfile(json_path):
            print(f"[SKIP] File not found: {json_path}")
            continue
        report = audit_instance(json_path, max_level=args.level)
        reports.append(report)
        print(report.summary())

    # Summary
    total = len(reports)
    all_passed = sum(1 for r in reports if r.passed)
    print(f"\n{'='*70}")
    print(f"  Summary: {all_passed}/{total} instance(s) passed all audit checks")
    if all_passed < total:
        failed_ids = [r.instance_id for r in reports if not r.passed]
        print(f"  Failed: {failed_ids}")
    print(f"{'='*70}")

    # JSON output
    if args.json_output:
        out = []
        for r in reports:
            out.append({
                "instance_id": r.instance_id,
                "passed": r.passed,
                "checks": [
                    {"level": c.level, "name": c.name,
                     "passed": c.passed, "detail": c.detail}
                    for c in r.results
                ],
            })
        with open(args.json_output, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        print(f"\nAudit results saved to: {args.json_output}")

    return 0 if all_passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
