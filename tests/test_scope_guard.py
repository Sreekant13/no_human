"""Tests for the deterministic scope & dependency guards (Phase 5e)."""

import textwrap
from pathlib import Path

import pytest

from no_human.agent.scope_guard import (
    SCRATCH_DIR,
    ScopeGuardHook,
    check_dependency_diff,
    check_forbidden_imports,
    check_scope,
    commit_time_checks,
    is_agent_owned,
    parse_plan_files,
    scratch_redirect,
)


# ── parse_plan_files ──────────────────────────────────────────────────── #


def test_parse_plan_files_basic():
    plan = textwrap.dedent("""\
        ## FILES TO CHANGE/CREATE
        - `src/no_human/core/orchestrator.py` — add scope guard wiring
        - `src/no_human/agent/scope_guard.py` — new module
        - tests/test_scope_guard.py — tests

        ## TEST PLAN
        ...
    """)
    result = parse_plan_files(plan)
    assert "src/no_human/core/orchestrator.py" in result
    assert "src/no_human/agent/scope_guard.py" in result
    assert "tests/test_scope_guard.py" in result


def test_parse_plan_files_no_section():
    assert parse_plan_files("no plan here") == set()


def test_parse_plan_files_empty_section():
    plan = "## FILES TO CHANGE/CREATE\n\n## TEST PLAN\n"
    assert parse_plan_files(plan) == set()


def test_parse_plan_files_table_and_extensionless():
    """Regression: the planner sometimes emits this section as a markdown
    table, and extension-less filenames (Jenkinsfile, Makefile) must not be
    dropped — this was silently missing `Jenkinsfile` entirely, causing
    every legitimate edit to it to fire a false [SCOPE] warning."""
    plan = textwrap.dedent("""\
        ## FILES TO CHANGE/CREATE

        | File | Change |
        |------|--------|
        | `Jenkinsfile` | Add integration test stage |
        | `.no_human/project.yml` | Add required credential |

        ---

        ## TEST PLAN
        ...
    """)
    result = parse_plan_files(plan)
    assert result == {"Jenkinsfile", ".no_human/project.yml"}


# ── check_scope ───────────────────────────────────────────────────────── #


def test_check_scope_in_plan():
    plan_files = {"src/foo.py", "src/bar.py"}
    assert check_scope("src/foo.py", plan_files) is None


def test_check_scope_out_of_plan():
    plan_files = {"src/foo.py"}
    result = check_scope("src/other.py", plan_files)
    assert result is not None
    assert "[SCOPE]" in result
    assert "other.py" in result


def test_check_scope_empty_plan():
    assert check_scope("src/foo.py", set()) is None


def test_check_scope_relative_path():
    plan_files = {"src/foo.py"}
    assert check_scope("./src/foo.py", plan_files) is None


def test_check_scope_basename_match():
    """Fuzzy: if basename matches a plan file, allow it."""
    plan_files = {"src/core/foo.py"}
    assert check_scope("/repo/src/core/foo.py", plan_files, "/repo") is None


# ── check_forbidden_imports ───────────────────────────────────────────── #


def test_check_forbidden_imports(tmp_path):
    py_file = tmp_path / "bad.py"
    py_file.write_text("import torch\nimport os\n")
    result = check_forbidden_imports([py_file])
    assert len(result) == 1
    assert result[0][2] == "import torch"


def test_check_forbidden_imports_clean(tmp_path):
    py_file = tmp_path / "good.py"
    py_file.write_text("import os\nimport json\n")
    assert check_forbidden_imports([py_file]) == []


def test_check_forbidden_imports_skips_non_python(tmp_path):
    js_file = tmp_path / "app.js"
    js_file.write_text("import torch from 'torch';\n")
    assert check_forbidden_imports([js_file]) == []


# ── agent-owned paths (D18) ───────────────────────────────────────────── #


def test_is_agent_owned_relative_and_absolute():
    assert is_agent_owned(".no_human/draft.groovy")
    assert is_agent_owned("/repo/.no_human/draft.groovy")
    assert is_agent_owned("/repo/.claude/settings.json")
    assert not is_agent_owned("/repo/src/foo.py")


def test_is_agent_owned_relative_to_repo_root():
    """A repo living under an agent-owned dir must not read as wholly owned."""
    assert not is_agent_owned("/home/u/.claude/repo/src/foo.py", "/home/u/.claude/repo")


def test_check_scope_exempts_agent_owned_dirs():
    """D18: the coder drafted in `.no_human/` and got '[SCOPE] … revert' on every
    write. `.no_human/` never reaches a diff, so it cannot be out of scope."""
    plan_files = {"src/foo.py"}
    assert check_scope(".no_human/ci_gate_stage_draft.groovy", plan_files) is None
    assert check_scope("/repo/.no_human/draft.groovy", plan_files, "/repo") is None
    # …while a genuinely unplanned source file is still flagged.
    assert check_scope("src/other.py", plan_files) is not None


def test_scratch_redirect_points_at_the_scratch_dir_and_never_says_revert():
    msg = scratch_redirect("/repo/.no_human/draft.groovy", "/repo")
    assert msg is not None
    assert SCRATCH_DIR in msg
    assert "revert" not in msg.lower()


def test_scratch_redirect_silent_inside_the_scratch_dir():
    assert scratch_redirect(f"{SCRATCH_DIR}/notes.md") is None
    assert scratch_redirect(f"/repo/{SCRATCH_DIR}/notes.md", "/repo") is None


def test_scratch_redirect_ignores_ordinary_paths():
    assert scratch_redirect("src/foo.py") is None


# ── ScopeGuardHook ────────────────────────────────────────────────────── #


@pytest.mark.asyncio
async def test_scope_guard_hook_warns_on_out_of_plan():
    plan = "## FILES TO CHANGE/CREATE\n- src/foo.py\n"
    events = []
    hook = ScopeGuardHook(plan, "/repo", on_event=lambda k, t: events.append((k, t)))
    result = await hook.hook(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/bar.py"}},
        "id1", None,
    )
    assert "additionalContext" in result.get("hookSpecificOutput", {})
    assert len(events) == 1
    assert events[0][0] == "scope_warning"


@pytest.mark.asyncio
async def test_scope_guard_hook_nudges_once_per_agent_owned_path():
    """The nudge reaches the agent every time (it is the tool's feedback), but
    the event log records it once — five identical warnings was the D18 noise."""
    plan = "## FILES TO CHANGE/CREATE\n- src/foo.py\n"
    events = []
    hook = ScopeGuardHook(plan, "/repo", on_event=lambda k, t: events.append((k, t)))
    call = {"tool_name": "Write",
            "tool_input": {"file_path": "/repo/.no_human/draft.groovy"}}

    first = await hook.hook(call, "id1", None)
    second = await hook.hook(call, "id2", None)

    assert SCRATCH_DIR in first["hookSpecificOutput"]["additionalContext"]
    assert SCRATCH_DIR in second["hookSpecificOutput"]["additionalContext"]
    assert [k for k, _ in events] == ["scratch_redirect"]


@pytest.mark.asyncio
async def test_scope_guard_hook_silent_inside_scratch_dir():
    plan = "## FILES TO CHANGE/CREATE\n- src/foo.py\n"
    events = []
    hook = ScopeGuardHook(plan, "/repo", on_event=lambda k, t: events.append((k, t)))
    result = await hook.hook(
        {"tool_name": "Write",
         "tool_input": {"file_path": f"/repo/{SCRATCH_DIR}/notes.md"}},
        "id1", None,
    )
    assert result == {}
    assert events == []


@pytest.mark.asyncio
async def test_scope_guard_hook_silent_on_in_plan():
    plan = "## FILES TO CHANGE/CREATE\n- src/foo.py\n"
    hook = ScopeGuardHook(plan, "/repo")
    result = await hook.hook(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/foo.py"}},
        "id1", None,
    )
    assert result == {}


@pytest.mark.asyncio
async def test_scope_guard_hook_ignores_reads():
    plan = "## FILES TO CHANGE/CREATE\n- src/foo.py\n"
    hook = ScopeGuardHook(plan, "/repo")
    result = await hook.hook(
        {"tool_name": "Read", "tool_input": {"file_path": "src/bar.py"}},
        "id1", None,
    )
    assert result == {}


@pytest.mark.asyncio
async def test_scope_guard_hook_no_plan():
    hook = ScopeGuardHook("no plan here", "/repo")
    result = await hook.hook(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/bar.py"}},
        "id1", None,
    )
    assert result == {}
