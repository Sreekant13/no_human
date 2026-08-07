"""Deterministic wiring evidence for the reviewer's goal-reachability check.

Lists top-level Python symbols a diff ADDS that have no reference anywhere in
the after-state tree outside their own defining file and test paths. That is
the raw material of the "implemented but never called by the production path"
defect class — surfaced as labeled evidence next to the lint section, never as
a verdict: an unreferenced symbol may be wired through dynamic dispatch this
module cannot see, or be exactly the uncalled artifact the request asks for.

Named ceilings, stated because the prompt block repeats them:

* PYTHON-ONLY: symbols come from ``ast.parse`` over changed ``.py`` files, so
  a diff in any other language contributes nothing and the section is absent.
* STATIC: references are found by ``git grep -w`` over the after-state tree.
  Dynamic dispatch, registries, ``getattr``, string-built names and console
  entry points are invisible to it.
* TOP-LEVEL ONLY: functions and classes at module top level. Methods,
  nested defs and assignments are not inspected.

Advisory only, like lint_evidence: any failure — a bad ref, unparseable
Python, a timeout — returns an empty result and must never block or slow the
review gate.
"""

from __future__ import annotations

import ast
import logging
import subprocess

log = logging.getLogger(__name__)

# Hard cap on the whole collection pass, same rationale as
# lint_evidence.LINT_TIMEOUT: advisory evidence must never stall the gate.
WIRING_TIMEOUT = 30
# One `git grep` per symbol, so bound the symbol count.
MAX_WIRING_SYMBOLS = 30
MAX_WIRING_BYTES = 4096


def _is_test_path(path: str) -> bool:
    """True for paths whose references do not count as production wiring."""
    parts = path.split("/")
    name = parts[-1]
    return (
        "tests" in parts
        or "test" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def _show(repo_path, ref: str, rel: str, timeout: float) -> str | None:
    """Text of ``rel`` at ``ref``, or None (absent, binary, unreadable)."""
    proc = subprocess.run(
        ["git", "show", f"{ref}:{rel}"],
        cwd=repo_path, capture_output=True, timeout=timeout,
    )
    if proc.returncode != 0 or b"\x00" in proc.stdout:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _top_level_symbols(text: str) -> set[str]:
    """Names of module-top-level function and class definitions."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    return {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def collect_wiring_evidence(
    repo_path, before_ref: str, after_ref: str, *, timeout: int = WIRING_TIMEOUT,
) -> list[tuple[str, str]]:
    """``(defining_path, symbol)`` pairs the diff adds with no reference found
    outside the defining file and test paths, sorted. ``[]`` on ANY failure.

    A symbol whose reference search fails (git error, timeout) is treated as
    referenced — this evidence fails toward silence, never toward accusation.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-status", "-M", f"{before_ref}..{after_ref}"],
            cwd=repo_path, capture_output=True, text=True, errors="replace",
            timeout=timeout,
        )
        if proc.returncode != 0:
            return []
        added: list[tuple[str, str]] = []
        for line in (proc.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or parts[0].startswith("D"):
                continue
            rel = parts[-1]
            if not rel.endswith(".py") or _is_test_path(rel):
                continue
            after_text = _show(repo_path, after_ref, rel, timeout)
            if after_text is None:
                continue
            before_text = _show(repo_path, before_ref, rel, timeout) or ""
            new = _top_level_symbols(after_text) - _top_level_symbols(before_text)
            added.extend((rel, name) for name in sorted(new))
        added = added[:MAX_WIRING_SYMBOLS]

        unreferenced: list[tuple[str, str]] = []
        for rel, name in added:
            grep = subprocess.run(
                ["git", "grep", "-l", "-w", "--fixed-strings", "-e", name,
                 after_ref],
                cwd=repo_path, capture_output=True, text=True, errors="replace",
                timeout=timeout,
            )
            if grep.returncode not in (0, 1):
                continue  # untrusted search — treat as referenced
            outside = False
            for hit in (grep.stdout or "").splitlines():
                # `git grep -l <ref>` lines are "<ref>:<path>".
                path = hit.split(":", 1)[1] if ":" in hit else hit
                if path != rel and not _is_test_path(path):
                    outside = True
                    break
            if not outside:
                unreferenced.append((rel, name))
        return sorted(unreferenced)
    except subprocess.TimeoutExpired:
        log.warning("wiring evidence timed out after %ds", timeout)
        return []
    except Exception:  # noqa: BLE001 — advisory evidence, never blocks review
        log.warning("wiring evidence failed", exc_info=True)
        return []


def format_wiring_evidence(unreferenced: list[tuple[str, str]]) -> str:
    """Render the labeled block for the reviewer prompt; "" when empty.

    Capped at MAX_WIRING_BYTES with a truncation note, like lint evidence.
    """
    if not unreferenced:
        return ""
    header = (
        "WIRING EVIDENCE (deterministic, static, Python-only — dynamic "
        "dispatch/registries not visible): top-level symbols this diff adds "
        "with no reference found outside their defining file and test paths:"
    )
    lines = [header]
    size = len(header)
    shown = 0
    for rel, name in unreferenced:
        line = f"  {rel}: {name}"
        if size + 1 + len(line) > MAX_WIRING_BYTES:
            break
        lines.append(line)
        size += 1 + len(line)
        shown += 1
    if shown < len(unreferenced):
        lines.append(f"  ... truncated ({len(unreferenced) - shown} more)")
    lines.append(
        "  Evidence, not a verdict: a listed symbol may be wired dynamically, "
        "or be exactly the uncalled artifact the request asks for."
    )
    return "\n".join(lines)
