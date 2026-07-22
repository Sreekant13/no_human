"""The repro gate hard-fails a Python bugfix that ships no
`.no_human/repro_tests.json` (orchestrator._run_attempt treats the "waived"
verdict as blocking as "fail"), but no prompt ever named the file or the
consequence — so the coder learned the requirement by burning a whole attempt
(db9bdeb7: 37 turns / ~19k tokens on attempt #1, once per Python bugfix on any
repo without a manifest). These tests pin the instruction to what the gate
actually enforces, in both directions."""

from __future__ import annotations

import pytest

from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.prompt_blocks import build_rules_block
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import FakeBackend


@pytest.fixture
async def store():
    s = await Store(":memory:").connect()
    yield s
    await s.close()


def _orch(store, tmp_path, mode=None):
    cfg = load_config(tmp_path / "c.yaml")
    if mode is not None:
        cfg.data.setdefault("repro_gate", {})["mode"] = mode
    return Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))


def _bugfix(tmp_path):
    t = Task.new("fix the off-by-one", repo_path=str(tmp_path))
    t.kind = "bugfix"
    return t


async def test_bugfix_directive_names_the_manifest_path_schema_and_consequence(
        store, tmp_path):
    d = _orch(store, tmp_path)._kind_directive(_bugfix(tmp_path))
    # The exact path the gate reads (testing/repro_gate.py: MANIFEST).
    assert ".no_human/repro_tests.json" in d
    # The schema, so the coder does not have to guess the key.
    assert '{"tests":' in d.replace('{"tests": ', '{"tests":')
    # Both directions the gate proves.
    low = d.lower()
    assert "fail on the unfixed code" in low
    assert "pass with your fix" in low
    # The consequence — the part whose absence cost a whole attempt.
    assert "failed" in low and "sent back" in low
    # And it must land in THIS attempt, not a later one.
    assert "this same attempt" in low


async def test_bugfix_directive_is_scoped_to_python_changes(store, tmp_path):
    """A JS/CSS-only bugfix cannot have a pytest repro. The gate enforces only
    when the change touched .py (orchestrator: ``changed_py``); demanding a
    manifest unconditionally is the 2026-07-11 regression that made every web
    bugfix uncompletable."""
    d = _orch(store, tmp_path)._kind_directive(_bugfix(tmp_path))
    assert "WHEN YOUR FIX CHANGES PYTHON" in d


async def test_no_manifest_demand_when_the_gate_is_off(store, tmp_path):
    """mode=off never runs the gate, so asking for the file would request
    something nothing reads."""
    d = _orch(store, tmp_path, mode="off")._kind_directive(_bugfix(tmp_path))
    assert ".no_human/repro_tests.json" not in d
    # The rest of the bugfix directive survives.
    assert "root cause" in d.lower()


async def test_other_kinds_do_not_get_the_bugfix_manifest_directive(
        store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("add a feature", repo_path=str(tmp_path))
    t.kind = "feature"
    assert "REPRO MANIFEST — REQUIRED" not in orch._kind_directive(t)


async def test_unknown_kind_stays_empty(store, tmp_path):
    """The directive is only ever appended to a real one — an unknown kind must
    not become a manifest-only instruction with no task context."""
    orch = _orch(store, tmp_path)
    t = Task.new("something", repo_path=str(tmp_path))
    t.kind = "not_a_kind"
    assert orch._kind_directive(t) == ""


def test_rules_block_states_the_consequence_only_when_required():
    """Under mode=required the hard-fail applies to EVERY kind, so the shared
    rules block is where that consequence belongs."""
    req = build_rules_block("pytest", "", None, repro_mode="required")
    assert ".no_human/repro_tests.json" in req
    assert "FAILS this" in req

    adv = build_rules_block("pytest", "", None, repro_mode="advisory")
    assert ".no_human/repro_tests.json" in adv
    # Advisory mode blocks bugfixes only — the bugfix directive carries that;
    # a blanket "fails every attempt" here would be false for other kinds.
    assert "FAILS this" not in adv


def test_rules_block_drops_the_manifest_bullet_when_the_gate_is_off():
    off = build_rules_block("pytest", "", None, repro_mode="off")
    assert "repro_tests.json" not in off
    # Neighbouring rules are untouched.
    assert "NEVER weaken, skip, or delete a test" in off


def test_rules_block_default_matches_the_orchestrator_default():
    """The default must equal config's ``repro_gate.mode`` default (advisory),
    or a caller that omits the argument silently changes the prompt."""
    assert build_rules_block("pytest", "", None) == build_rules_block(
        "pytest", "", None, repro_mode="advisory")
