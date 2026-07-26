"""SCRUM-59: PR hygiene — the ticket prefix is never doubled in the PR/commit
title, and harness-to-coder dialogue paragraphs never leak into the PR body's
implementation summary."""

import pytest

from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


class _Commit:
    files_changed = 2
    insertions = 10
    deletions = 1


class _Result:
    final_text = "did the thing"
    num_turns = 5


def test_commit_message_no_double_prefix(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("SCRUM-59: Fix X", repo_path="/r", external_id="SCRUM-59")
    msg = orch._commit_message(t)
    assert msg == "SCRUM-59: Fix X"
    assert "SCRUM-59: SCRUM-59:" not in msg


def test_commit_message_prepends_when_absent(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("Fix X", repo_path="/r", external_id="SCRUM-59")
    assert orch._commit_message(t) == "SCRUM-59: Fix X"


def test_commit_message_distinct_key_not_suppressed(store, tmp_path):
    """A different (e.g. shorter, prefix-of) external_id must not falsely
    match a title that merely starts with a similar-looking key."""
    orch = _orch(store, tmp_path)
    t = Task.new("SCRUM-59: Fix X", repo_path="/r", external_id="SCRUM-5")
    assert orch._commit_message(t) == "SCRUM-5: SCRUM-59: Fix X"


def test_commit_message_no_external_id(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("Fix X", repo_path="/r")
    assert orch._commit_message(t) == "Fix X"


def test_pr_body_filters_harness_dialogue(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = (
        "That's expected — repro_tests.json is metadata for the harness "
        "and is never committed.\n\n"
        "Added the real change: dedupe the prefix."
    )
    body = orch._pr_body(t, _Commit(), result)
    assert "Added the real change" in body
    assert "repro_tests.json" not in body
    assert "harness" not in body
    assert "metadata" not in body


def test_pr_body_keeps_clean_summary(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = "Implemented the fix and added tests."
    body = orch._pr_body(t, _Commit(), result)
    assert "Implemented the fix and added tests." in body


def test_pr_body_keeps_metadata_column_paragraph(store, tmp_path):
    """Bare 'metadata' must not be a drop marker — a legitimate summary
    describing a schema change would otherwise be silently dropped."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = "Added a metadata column to the tasks table."
    body = orch._pr_body(t, _Commit(), result)
    assert "Added a metadata column to the tasks table." in body


def test_pr_body_keeps_harness_fixture_paragraph(store, tmp_path):
    """Bare 'harness' must not be a drop marker — a legitimate summary
    describing test infrastructure would otherwise be silently dropped."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = "Extended the test harness fixture to cover the new case."
    body = orch._pr_body(t, _Commit(), result)
    assert "Extended the test harness fixture to cover the new case." in body


def test_pr_body_all_filtered_emits_placeholder(store, tmp_path):
    """When every paragraph is dropped, the section gets a placeholder
    instead of rendering empty."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = (
        "That's expected — repro_tests.json is for the harness.\n\n"
        "See system instructions for details on the metadata file."
    )
    body = orch._pr_body(t, _Commit(), result)
    assert "_(implementation summary was filtered — see commits)_" in body
    assert "repro_tests.json" not in body
