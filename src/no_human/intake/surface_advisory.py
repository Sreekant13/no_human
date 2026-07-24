"""SCRUM-24: deterministic surface classification for intake advisories.

Pure, no LLM, no I/O. Dogfooding showed every ticket bundling more than one
surface (backend + frontend, CLI + board UI, etc.) burned 14-18M tokens across
truncated attempts and shipped nothing, while single-surface tickets merged in
1-2 review rounds. This module classifies the surfaces a plan's
``files_to_change`` touches so the orchestrator can attach a non-blocking
advisory before the attempt burns its budget.
"""

from __future__ import annotations

_TEST_DIR_SEGMENTS = {"test", "tests", "__tests__"}


def _extract_path_token(item: str) -> str | None:
    """First path-like token in a raw plan line (mirrors
    ``scope_guard._extract_path_token`` without importing it, to avoid
    coupling this pure module to the agent-side scope guard)."""
    tokens = item.split()
    for tok in tokens:
        tok = tok.strip("`'\"(),:|")
        if not tok:
            continue
        if "/" in tok or "." in tok:
            return tok
        if len(tokens) == 1:
            return tok
    return None


def _is_tests_only(path: str) -> bool:
    segments = path.split("/")
    if any(seg in _TEST_DIR_SEGMENTS for seg in segments[:-1]):
        return True
    basename = segments[-1]
    if basename == "conftest.py":
        return True
    if basename.startswith("test_") and basename.endswith(".py"):
        return True
    if basename.endswith("_test.py"):
        return True
    return False


def _surface_for(path: str) -> str | None:
    if path.startswith("src/"):
        return "backend"
    if path.startswith("web/"):
        return "frontend"
    if path.startswith("desktop/"):
        return "desktop"
    if path.startswith("docs/") or path.endswith(".md"):
        return "docs"
    return None


def classify_surfaces(files_to_change: list[str]) -> set[str]:
    """Distinct surfaces touched by *files_to_change*, tests-only excluded."""
    surfaces: set[str] = set()
    for item in files_to_change:
        path = _extract_path_token(item)
        if not path:
            continue
        path = path.removeprefix("./")
        if _is_tests_only(path):
            continue
        surface = _surface_for(path)
        if surface:
            surfaces.add(surface)
    return surfaces


def surface_advisory(files_to_change: list[str]) -> str | None:
    """A human-facing caution when the plan spans >=2 surfaces, else None."""
    surfaces = classify_surfaces(files_to_change)
    if len(surfaces) < 2:
        return None
    joined = " + ".join(sorted(surfaces))
    return (
        f"This task spans {joined}. Multi-surface tasks fail ~4x more often "
        "within budget - consider splitting (backend first, frontend "
        "against its contract)."
    )
