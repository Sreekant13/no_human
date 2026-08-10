"""The three test lanes are a tested fact, not a convention.

A mis-typed marker expression drops nodes out of BOTH lanes at once, and a test
that runs nowhere looks exactly like a test that passes. Nothing in pytest
notices that; these assertions do. They read `pyproject.toml` and
`.github/workflows/ci.yml` as text, because the selector strings CI actually
executes are the thing under test — a re-derivation of them here would agree
with itself and prove nothing.

The lanes:

    PR                          -m "not slow and not nightly"
    schedule / workflow_dispatch  -m "slow or nightly"
    push to main                (no filter — everything)

Scope note. This module lands with Task 1 of Phase R-C, which only registers
the `nightly` mark and wires the lanes; **no test carries `nightly` yet**. The
positive membership assertions — "this named heavy guard is in the nightly
lane" — arrive with Tasks 2-5, one per moved file. What is asserted here today
is the wiring plus the *negative* half that must hold at every point in that
series: the leak / tamper / export guards of the plan's Global Constraint 2
never acquire the mark. Those assertions are written to keep meaning as Tasks
2-5 add marks to other tests in the same files.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
SCRUB_YML = REPO / ".github" / "workflows" / "scrub.yml"

PR_SELECTOR = '-m "not slow and not nightly"'
NIGHTLY_SELECTOR = '-m "slow or nightly"'


def _marks(path: Path) -> dict[str, set[str]]:
    """{test function name: {marker names on it}} for one test module.

    Parsed, never imported: importing a test module runs its collection-time
    code and drags in the whole conftest, and this only needs the decorators.
    Module-level `pytestmark` applies to every function in the file, so it is
    folded into each entry.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def names(node: ast.AST) -> set[str]:
        # pytest.mark.X and pytest.mark.X(...) both reduce to "X".
        return {
            sub.attr
            for sub in ast.walk(node)
            if isinstance(sub, ast.Attribute)
            and isinstance(sub.value, ast.Attribute)
            and sub.value.attr == "mark"
        }

    module_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            module_level |= names(node.value)

    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test"
        ):
            out[node.name] = module_level | {
                m for dec in node.decorator_list for m in names(dec)
            }
    return out


def _bodies(path: Path) -> dict[str, str]:
    """{test function name: its source text} for one test module."""
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    return {
        node.name: "\n".join(lines[node.lineno - 1 : node.end_lineno])
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    }


# --------------------------------------------------------------------------
# (a) the mark is registered
# --------------------------------------------------------------------------


def test_the_nightly_marker_is_registered():
    """An unregistered mark is a warning, not an error — and `-m nightly`
    would then quietly select nothing while looking like it worked."""
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    markers = cfg["tool"]["pytest"]["ini_options"]["markers"]
    registered = {m.split(":", 1)[0].strip() for m in markers}
    assert "nightly" in registered, f"markers block is {markers}"
    # The two that were already there must not be lost to the edit.
    assert {"slow", "real_backend"} <= registered, registered


# --------------------------------------------------------------------------
# (b) CI executes exactly those selectors
# --------------------------------------------------------------------------


def test_the_pr_lane_selector_is_wired_in_ci():
    assert PR_SELECTOR in CI_YML.read_text(encoding="utf-8")


def test_the_nightly_lane_selector_is_wired_in_ci():
    assert NIGHTLY_SELECTOR in CI_YML.read_text(encoding="utf-8")


def test_the_nightly_lane_has_a_trigger_that_can_fire_it():
    """A selector nothing triggers is a lane that never runs. The `on:` block
    has to carry both the schedule and the manual handle."""
    on_block = re.search(
        r"^on:\n((?:[ \t].*\n|\n)+)", CI_YML.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert on_block, "ci.yml has no `on:` block"
    body = on_block.group(1)
    assert "schedule:" in body, body
    assert "cron:" in body, body
    assert "workflow_dispatch:" in body, body


def test_the_main_push_lane_still_runs_everything():
    """The fallback arm of the selector expression is the empty string, which
    is what makes a push to main run the whole suite. If it ever becomes a
    filter, a merged state stops carrying the full signal."""
    step = re.search(
        r"- name: Run tests\n\s+run: \|\n(.*?)\n\n",
        CI_YML.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert step, "ci.yml has no `Run tests` step in the expected shape"
    assert "|| ''" in step.group(1), step.group(1)


def test_the_local_runner_agrees_with_ci():
    """`scripts/run_tests.sh` is the same lanes for a human. If it drifts, a
    local green run stops predicting the CI result."""
    sh = (REPO / "scripts" / "run_tests.sh").read_text(encoding="utf-8")
    assert '-m "not slow and not nightly"' in sh, sh
    assert '-m "slow or nightly"' in sh, sh
    assert re.search(r"^\s+nightly\)", sh, re.MULTILINE), "no `nightly)` mode in run_tests.sh"


def test_the_nightly_lane_has_a_home_while_actions_minutes_are_out():
    """Actions minutes are exhausted, so the 03:00 launchd job is where the
    nightly lane actually runs today. Its exit code has to reach the caller —
    a lane whose failure is swallowed is a lane that is not a gate."""
    sh = (REPO / "scripts" / "nightly_eval.sh").read_text(encoding="utf-8")
    assert "run_tests.sh nightly" in sh, sh


# --------------------------------------------------------------------------
# (c) the must-stay-per-push guards never acquire the mark
#
# Global Constraint 2 of PLAN-test-suite-audit.md. Each entry below was checked
# to exist on main before it was listed here: a guard list that names a file
# which is not there scans nothing and reports clean.
# --------------------------------------------------------------------------

# Whole files: nothing in them may ever leave the push path.
PER_PUSH_FILES = [
    "tests/test_no_banned_terms.py",
    "tests/test_export_allowlist.py",
    "tests/test_check_release_manifest.py",
    "tests/test_tamper_guard.py",
    "tests/test_tamper_guard_evasions.py",
    "tests/test_base_tree_gate.py",
]

# Named subsets of files that Tasks 2-5 DO move tests out of. A whole-file rule
# would be wrong for these; the selector is what keeps the assertion honest as
# the rest of the file moves. (file, regex over test names or bodies, minimum
# expected matches — the floor asserts the subject still exists, so a rename
# cannot turn this into a scan over nothing.)
PER_PUSH_SELECTIONS = [
    # The builder's refusals: an export that refuses is the whole gate.
    ("tests/test_build_public_export.py", "name", r"refus", 8),
    # Leak-class, not prose-class.
    ("tests/test_narrated_demo.py", "name", r"proper_noun|company", 4),
    # The heading-injection matrix over the repo's OWN scanner. The pandoc
    # oracle-agreement group (`_pandoc_headings`) is Task 4's to move; anything
    # asserting over `_live_headings` is pinning product behaviour and stays.
    ("tests/test_pr_body_truthfulness.py", "body", r"_live_headings", 6),
]


@pytest.mark.parametrize("rel", PER_PUSH_FILES)
def test_a_per_push_guard_file_carries_no_nightly_mark(rel):
    path = REPO / rel
    assert path.exists(), f"{rel} is listed as a per-push guard but does not exist"
    marked = sorted(n for n, m in _marks(path).items() if "nightly" in m)
    assert not marked, f"{rel} must stay per-push, but these carry `nightly`: {marked}"


@pytest.mark.parametrize(
    "rel,where,pattern,floor", PER_PUSH_SELECTIONS, ids=[s[0] for s in PER_PUSH_SELECTIONS]
)
def test_a_per_push_guard_group_carries_no_nightly_mark(rel, where, pattern, floor):
    path = REPO / rel
    assert path.exists(), f"{rel} is listed as a per-push guard but does not exist"
    marks = _marks(path)
    haystack = marks if where == "name" else _bodies(path)
    selected = [n for n, text in ((k, k if where == "name" else v) for k, v in haystack.items())
                if re.search(pattern, text)]
    assert len(selected) >= floor, (
        f"{rel}: /{pattern}/ matched {len(selected)} tests, expected at least {floor} — "
        "the guards were renamed or removed, and this assertion is now scanning nothing"
    )
    marked = sorted(n for n in selected if "nightly" in marks[n])
    assert not marked, f"{rel}: these must stay per-push but carry `nightly`: {marked}"


def test_the_scrub_workflow_still_runs_on_every_push_and_every_pr():
    """The scrub set is not a pytest lane, so a marker cannot move it — but it
    is on the same Global Constraint 2 list and the same edit could reach it.
    A term pushed into private history is permanent."""
    on_block = re.search(
        r"^on:\n((?:[ \t].*\n|\n)+)", SCRUB_YML.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert on_block, "scrub.yml has no `on:` block"
    body = on_block.group(1)
    assert "push:" in body and "pull_request:" in body, body
