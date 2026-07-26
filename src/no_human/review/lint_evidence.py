"""Deterministic lint evidence for the reviewer (SCRUM-64, phase 1: Python/ruff).

The reviewer currently judges a diff cold. This module detects whether the
repo under review has ruff configured (its OWN config — we never impose our
style), runs ruff on the diff's changed Python files ONLY, and returns
structured findings the reviewer context builder can attach as machine-parsed
evidence.

Advisory only: any failure — no config, missing binary, timeout, bad output —
returns an empty result. This must never block or slow the review gate.

Whose ruff runs: the `ruff` binary resolved from no_human's OWN PATH/
environment — deliberately never the target repo's venv (we never execute
code sourced from the repo under review). If that ruff's version disagrees
with the target repo's pinned ruff (e.g. an unsupported `--output-format` or
a rejected config option), ruff exits 2 and `collect_lint_evidence` treats it
as untrusted output -> empty result, by design.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Hard cap. The review gate has its own multi-minute budget; lint evidence is
# a small advisory add-on and must never be the reason a review stalls.
LINT_TIMEOUT = 30

_RUFF_TABLE_RE = re.compile(r"^\[tool\.ruff(\.[\w.-]+)?\]", re.MULTILINE)


@dataclass
class LintFinding:
    path: str
    line_number: int
    error_code: str
    message: str


def has_ruff_config(repo_path: Path) -> bool:
    """True if the repo configures ruff via ruff.toml/.ruff.toml, or a
    [tool.ruff] table in pyproject.toml. Absence of config means we attach
    nothing — never our own style preferences."""
    try:
        if (repo_path / "ruff.toml").is_file():
            return True
        if (repo_path / ".ruff.toml").is_file():
            return True
        pyproject = repo_path / "pyproject.toml"
        if pyproject.is_file():
            text = pyproject.read_text(encoding="utf-8", errors="ignore")
            if _RUFF_TABLE_RE.search(text):
                return True
    except OSError:
        return False
    return False


def _changed_python_files(repo_path: Path, changed_files: list[str]) -> list[str]:
    """Changed `.py` paths that actually exist inside `repo_path`. Never scans
    the repo — only ever considers paths the caller says are in the diff, and
    drops anything that would resolve outside the repo."""
    resolved_root = repo_path.resolve()
    out: list[str] = []
    for rel in changed_files:
        if not rel.endswith(".py"):
            continue
        try:
            resolved = (repo_path / rel).resolve()
            if not resolved.is_relative_to(resolved_root):
                continue
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            out.append(rel)
    return out


def collect_lint_evidence(
    repo_path: Path,
    changed_files: list[str],
    *,
    timeout: int = LINT_TIMEOUT,
) -> list[LintFinding]:
    """Run ruff on the diff's changed Python files, if the repo configures
    ruff. Returns [] on: no config, no changed .py files, a missing/failing
    ruff binary, a timeout, or unparseable output — this is advisory evidence
    and must never raise into the review gate."""
    try:
        if not has_ruff_config(repo_path):
            return []
        files = _changed_python_files(repo_path, changed_files)
        if not files:
            return []
        proc = subprocess.run(
            ["ruff", "check", "--output-format=json", "--no-fix", *files],
            cwd=repo_path, capture_output=True, text=True, timeout=timeout,
        )
        # ruff exits 1 when it finds violations — that is a normal result, not
        # a failure. Anything else (2 = usage/config error, missing binary
        # raising below) means we cannot trust the output.
        if proc.returncode not in (0, 1):
            log.warning("ruff exited %d: %s", proc.returncode, (proc.stderr or "")[:500])
            return []
        data = json.loads(proc.stdout or "[]")
        if not isinstance(data, list):
            return []
        findings: list[LintFinding] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            loc = item.get("location") or {}
            findings.append(LintFinding(
                path=str(item.get("filename") or ""),
                line_number=int(loc.get("row") or 0),
                error_code=str(item.get("code") or ""),
                message=str(item.get("message") or ""),
            ))
        # Determinism is enforced here, not inherited from ruff's (unordered
        # across files) output order.
        findings.sort(key=lambda f: (f.path, f.line_number, f.error_code))
        return findings
    except subprocess.TimeoutExpired:
        log.warning("ruff lint evidence timed out after %ds", timeout)
        return []
    except Exception:  # noqa: BLE001 — advisory evidence, never blocks the review
        log.warning("ruff lint evidence failed", exc_info=True)
        return []


# Hard caps on the rendered block. A changed file with thousands of
# violations (generated/vendored code, an aggressive `select = ["ALL"]`)
# must never blow the adversarial reviewer's context — this is advisory
# evidence, not the diff itself.
MAX_LINT_FINDINGS = 50
MAX_LINT_BYTES = 8192


def format_lint_evidence(findings: list[LintFinding]) -> str:
    """Render findings as a labeled block for the reviewer prompt.

    Empty string when there is nothing to show — callers must omit the
    section entirely rather than attach an empty, header-only block.

    Capped at MAX_LINT_FINDINGS findings and MAX_LINT_BYTES bytes, whichever
    is hit first, with a trailing '... truncated' line noting how many
    findings were dropped.
    """
    if not findings:
        return ""
    header = "Evidence: ruff (deterministic static analysis of the changed files)"
    lines = [header]
    shown = 0
    size = len(header)
    for f in findings:
        if shown >= MAX_LINT_FINDINGS:
            break
        loc = f"{f.path}:{f.line_number}" if f.line_number else f.path
        code = f" {f.error_code}" if f.error_code else ""
        line = f"  {loc}{code} {f.message}".rstrip()
        if size + 1 + len(line) > MAX_LINT_BYTES:
            break
        lines.append(line)
        size += 1 + len(line)
        shown += 1
    remaining = len(findings) - shown
    if remaining > 0:
        lines.append(f"  ... truncated ({remaining} more findings)")
    return "\n".join(lines)
