"""A `waived` repro verdict must buy ONE bounded corrective round on the SAME
attempt/branch before the attempt fails — not a full attempt restart.

BUG: a bugfix attempt that implements a fix and commits it (tamper-clean) but
whose repro gate comes back `waived` (no `.no_human/repro_tests.json`
manifest) used to be treated exactly like a `fail` verdict: the whole attempt
— potentially millions of tokens, dozens of turns — was discarded and the
next attempt started from a fresh branch off `main`, re-implementing
everything. A waived manifest is a MISSING ARTEFACT, not failed code: this
should cost one small corrective turn, not the attempt.

FIX: `Orchestrator._repro_gate_step` runs the gate; on `waived` it runs
`_repro_corrective_round` — one bounded coder turn (`_REPRO_CORRECTIVE_TURNS`)
on the SAME branch/worktree with `repro_send_back_message` as the
instruction — then re-runs the gate. Only a still-not-`pass` verdict fails
the attempt. A `fail` verdict (manifest exists, tests do not reproduce the
bug) is unchanged: it fails immediately, no corrective round.

These tests drive `_run_attempt` directly (the pattern `test_infra_not_
work.py` uses), against a real bare-repo checkout with a scripted backend, so
the whole pipeline — gate, corrective round, commit, tamper check, usage
accounting, abort routing — runs for real except for the LLM call itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.config import load_config
from no_human.core.db import Store
from no_human.blockers import SERVER_STOP_REASON
from no_human.core.orchestrator import (
    BudgetAbort, CancelRequested, Orchestrator, StuckAbort, repro_send_back_message,
)
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing.repro_gate import MANIFEST as REPRO_MANIFEST
from no_human.vcs import GitRepo


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    # A product file + an existing, already-passing test — the base the repro
    # gate's `fails-before` check runs against.
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _mul_files(cwd: Path) -> None:
    """A real fix (`mul`) plus its demonstrating test — no manifest."""
    cwd.joinpath("calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
    )
    cwd.joinpath("test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


async def _run_one_bugfix_attempt(store, bare_repo, tmp_path, backend):
    """Walk a fresh bugfix task to PLANNING and hand back everything a test
    needs to drive `_run_attempt` directly, mirroring
    `test_infra_not_work.py`'s `_run_one_attempt`."""
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    task = Task.new("fix mul()", repo_path=str(bare_repo), kind="bugfix")
    task.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)
    return orch, task, repo, events


# --------------------------------------------------------------------------- #
# AC1 (repro test) — waived, then pass: one bounded round, no restart         #
# --------------------------------------------------------------------------- #


class _WaivedThenPassBackend:
    """Coder turn: real fix, no manifest -> waived. Corrective round: writes
    the manifest naming a true repro -> pass."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "calc.py"}))
            _mul_files(cwd)
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")
        cwd.joinpath(".no_human").mkdir(exist_ok=True)
        (cwd / REPRO_MANIFEST).write_text('{"tests": ["test_calc.py::test_mul"]}')
        return AgentResult(final_text="wrote manifest", num_turns=1, is_error=False,
                           tokens_used=10, session_id="s2", stop_reason="end_turn")


async def test_waived_then_pass_runs_one_bounded_round_and_proceeds(
        bare_repo, tmp_path, store):
    """RED before the fix: today a `waived` verdict fails the attempt
    outright and this never gets a second gate run at all."""
    backend = _WaivedThenPassBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert backend.calls == 2

    gate_events = [e for e in events if e["kind"] == "repro_gate"]
    assert [e["verdict"] for e in gate_events] == ["waived", "pass"], gate_events

    round_events = [e for e in events if e["kind"] == "repro_corrective_round"]
    assert len(round_events) == 1, round_events

    # Same attempt throughout — not a restart on a fresh branch off main.
    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1, attempts


# --------------------------------------------------------------------------- #
# AC2a — waived twice: fails exactly as today, no review ran                  #
# --------------------------------------------------------------------------- #


class _WaivedTwiceBackend:
    """Coder turn: real fix, no manifest -> waived. Corrective round writes
    nothing at all -> still waived on the re-run."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "calc.py"}))
            _mul_files(cwd)
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")
        return AgentResult(final_text="nothing to add", num_turns=1, is_error=False,
                           tokens_used=5, session_id="s2", stop_reason="end_turn")


async def test_waived_twice_fails_the_attempt_exactly_as_today(
        bare_repo, tmp_path, store):
    backend = _WaivedTwiceBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.FAILED
    assert (outcome.detail or "").startswith("repro gate waived"), outcome.detail
    assert backend.calls == 2  # coder turn + exactly one corrective round, no retry

    gate_events = [e for e in events if e["kind"] == "repro_gate"]
    assert [e["verdict"] for e in gate_events] == ["waived", "waived"], gate_events

    round_events = [e for e in events if e["kind"] == "repro_corrective_round"]
    assert len(round_events) == 1, round_events  # never a second round

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert (attempts[0]["failure_reason"] or "").startswith("repro gate waived")

    # No review ran: no PR was opened.
    assert not [e for e in events if e["kind"] == "pr_open"]


# --------------------------------------------------------------------------- #
# AC2b — a `fail` verdict fails immediately, no corrective round              #
# --------------------------------------------------------------------------- #


class _FailVerdictBackend:
    """Writes a manifest naming a trivial test that passes regardless of the
    base state — a manifest that reproduces nothing. Deliberately a FRESH
    file untouched by the real fix (rather than naming a node inside
    `test_calc.py`): the fails-before check copies the whole FILE containing
    a named test into the base worktree, and `test_calc.py` at head imports
    `mul` — copying THAT file into a base tree that lacks `mul()` would fail
    on a collection/import error, which is a real failure but not the one
    this test means to exercise (an already-passing, bug-irrelevant test)."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        _mul_files(cwd)
        cwd.joinpath(".no_human").mkdir(exist_ok=True)
        cwd.joinpath("test_trivial.py").write_text(
            "from calc import add\n\n"
            "def test_trivial():\n    assert add(2, 2) == 4\n"
        )
        (cwd / REPRO_MANIFEST).write_text('{"tests": ["test_trivial.py::test_trivial"]}')
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_fail_verdict_fails_immediately_with_no_corrective_round(
        bare_repo, tmp_path, store):
    backend = _FailVerdictBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.FAILED
    assert (outcome.detail or "").startswith("repro gate fail:"), outcome.detail
    assert backend.calls == 1  # no corrective round call at all

    gate_events = [e for e in events if e["kind"] == "repro_gate"]
    assert [e["verdict"] for e in gate_events] == ["fail"], gate_events
    assert not [e for e in events if e["kind"] == "repro_corrective_round"]


# --------------------------------------------------------------------------- #
# AC3 — the corrective round's usage lands on the SAME attempt row (summed)   #
# --------------------------------------------------------------------------- #


class _UsageWaivedThenPassBackend:
    """Same waived->pass shape as `_WaivedThenPassBackend`, with numeric
    `AgentResult` fields chosen for exact-arithmetic verification."""

    CODER_TOKENS = 500
    CODER_CACHE_READ = 1000
    CODER_CACHE_CREATION = 200
    CODER_OUTPUT = 80
    CODER_TURNS = 5

    ROUND_TOKENS = 50
    ROUND_CACHE_READ = 70
    ROUND_CACHE_CREATION = 10
    ROUND_OUTPUT = 20
    ROUND_TURNS = 3

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "calc.py"}))
            _mul_files(cwd)
            return AgentResult(
                final_text="done", num_turns=self.CODER_TURNS, is_error=False,
                tokens_used=self.CODER_TOKENS, session_id="s", stop_reason="end_turn",
                cache_read_tokens=self.CODER_CACHE_READ,
                cache_creation_tokens=self.CODER_CACHE_CREATION,
                output_tokens=self.CODER_OUTPUT,
            )
        cwd.joinpath(".no_human").mkdir(exist_ok=True)
        (cwd / REPRO_MANIFEST).write_text('{"tests": ["test_calc.py::test_mul"]}')
        return AgentResult(
            final_text="wrote manifest", num_turns=self.ROUND_TURNS, is_error=False,
            tokens_used=self.ROUND_TOKENS, session_id="s2", stop_reason="end_turn",
            cache_read_tokens=self.ROUND_CACHE_READ,
            cache_creation_tokens=self.ROUND_CACHE_CREATION,
            output_tokens=self.ROUND_OUTPUT,
        )


async def test_corrective_round_usage_is_added_to_the_same_attempt_row(
        bare_repo, tmp_path, store):
    backend = _UsageWaivedThenPassBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    row = attempts[0]
    B = backend
    assert row["tokens_used"] == B.CODER_TOKENS + B.ROUND_TOKENS
    assert row["cache_read_tokens"] == B.CODER_CACHE_READ + B.ROUND_CACHE_READ
    assert row["cache_creation_tokens"] == (
        B.CODER_CACHE_CREATION + B.ROUND_CACHE_CREATION)
    assert row["output_tokens"] == B.CODER_OUTPUT + B.ROUND_OUTPUT
    assert row["turns_used"] == B.CODER_TURNS + B.ROUND_TURNS


# --------------------------------------------------------------------------- #
# AC4 — the send-back message describes the new one-round behaviour          #
# --------------------------------------------------------------------------- #


def test_send_back_message_docstring_describes_the_one_round():
    doc = repro_send_back_message.__doc__ or ""
    assert "ONE bounded corrective round" in doc, doc
    assert "SAME branch" in doc, doc
    assert "_repro_corrective_round" in doc, doc
    assert "fail" in doc and "waived" in doc, doc


# --------------------------------------------------------------------------- #
# Reviewer-mandated: abort controls must be RE-RAISED out of the corrective   #
# round, never swallowed into a FAILED repro attempt (mirrors                 #
# `_reformat_nudge`/`_abort_during_nudge`). `output_tokens` is part of the    #
# billed delta.                                                               #
# --------------------------------------------------------------------------- #


class _AbortingCorrectiveBackend:
    """Coder turn is genuine (real fix, no manifest -> waived). The
    corrective round feeds partial usage (including `output_tokens`) then
    raises one of the sink's three controls, mirroring
    `test_e2e_orchestrator._AbortingNudgeBackend`."""

    CODER_TOKENS = 100
    CODER_OUTPUT = 10
    FED_TOKENS = 9
    FED_OUTPUT = 4

    def __init__(self, exc):
        self.calls = 0
        self._exc = exc

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "calc.py"}))
            _mul_files(cwd)
            return AgentResult(
                final_text="done", num_turns=2, is_error=False,
                tokens_used=self.CODER_TOKENS, session_id="s", stop_reason="end_turn",
                output_tokens=self.CODER_OUTPUT,
            )
        if on_event is not None:
            on_event(AgentEvent(kind="usage", meta={
                "tokens_used": self.FED_TOKENS, "output_tokens": self.FED_OUTPUT}))
        raise self._exc


async def test_a_budget_abort_in_the_corrective_round_bills_output_tokens_and_reraises(
        bare_repo, tmp_path, store):
    # The very low cap means the sink's OWN budget check (fired by the
    # `usage` event below) raises the real `BudgetAbort` before this
    # backend's explicit `raise self._exc` line is ever reached — so the
    # message asserted below is the sink's, not the literal passed here.
    # Either way a `BudgetAbort` crosses `_repro_corrective_round`'s call,
    # which is exactly what this test exercises.
    backend = _AbortingCorrectiveBackend(BudgetAbort("attempt spend crossed the cap"))
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)
    task.config = {"lifetime_tokens": 1, "budget_unit": "weighted"}

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls == 2
    gate_events = [e for e in events if e["kind"] == "repro_gate"]
    assert [e["verdict"] for e in gate_events] == ["waived"], gate_events
    assert len([e for e in events if e["kind"] == "repro_corrective_round"]) == 1

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    row = attempts[0]
    assert row["status"] == "failed"
    assert (row["failure_reason"] or "").startswith("budget-abort:")
    assert "budget" in row["failure_reason"]
    # The corrective round's partial spend — INCLUDING output_tokens — reached
    # the ledger; this is the reviewer's explicit callout.
    assert row["tokens_used"] == backend.CODER_TOKENS + backend.FED_TOKENS
    assert row["output_tokens"] == backend.CODER_OUTPUT + backend.FED_OUTPUT

    fresh = await store.find_task(task.id)
    assert (fresh.blocker or {}).get("category") == "BUDGET_EXHAUSTED"
    assert outcome.status in (TaskStatus.BLOCKED, TaskStatus.ESCALATED, TaskStatus.FAILED)


async def test_a_stuck_abort_in_the_corrective_round_bills_output_tokens_and_reraises(
        bare_repo, tmp_path, store):
    backend = _AbortingCorrectiveBackend(StuckAbort("identical tool call x3"))
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls == 2
    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    row = attempts[0]
    assert row["status"] == "failed"
    assert row["failure_reason"] == "stuck-abort: identical tool call x3"
    assert row["tokens_used"] == backend.CODER_TOKENS + backend.FED_TOKENS
    assert row["output_tokens"] == backend.CODER_OUTPUT + backend.FED_OUTPUT
    assert outcome.status is TaskStatus.FAILED


async def test_a_cancel_in_the_corrective_round_parks_instead_of_failing(
        bare_repo, tmp_path, store):
    """The one control `_abort_during_repro_corrective` does NOT handle —
    `_repro_gate_step`'s own `except CancelRequested` routes straight to
    `_honor_cancel`, exactly like the `_reformat_nudge` sibling. A pause must
    still park; it must NOT surface as a failed repro attempt."""
    backend = _AbortingCorrectiveBackend(CancelRequested("operator paused"))
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls == 2
    assert outcome.status is TaskStatus.BLOCKED
    assert outcome.detail == "cancelled by operator: operator paused"

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    assert attempts[0]["status"] != "failed"

    fresh = await store.find_task(task.id)
    assert (fresh.blocker or {}).get("category") == "USER_PAUSED"


# --------------------------------------------------------------------------- #
# Corrective round commits a real change and runs its OWN tamper check        #
# --------------------------------------------------------------------------- #


class _CorrectiveCommitsRealChangeBackend:
    """Coder turn: real fix, no manifest -> waived. Corrective round writes
    the manifest AND a tracked, non-weakening change to `calc.py` — a real
    diff `repo.has_changes()` must see, so the round commits it and runs its
    own tamper check before the gate re-runs."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "calc.py"}))
            _mul_files(cwd)
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")
        cwd.joinpath(".no_human").mkdir(exist_ok=True)
        (cwd / REPRO_MANIFEST).write_text('{"tests": ["test_calc.py::test_mul"]}')
        calc = cwd / "calc.py"
        calc.write_text(calc.read_text() + "\n# note added by the corrective round\n")
        if on_event is not None:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        return AgentResult(final_text="wrote manifest + note", num_turns=1,
                           is_error=False, tokens_used=10, session_id="s2",
                           stop_reason="end_turn")


async def test_corrective_round_commits_a_real_change_and_runs_its_own_tamper_check(
        bare_repo, tmp_path, store):
    backend = _CorrectiveCommitsRealChangeBackend()
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    gate_events = [e for e in events if e["kind"] == "repro_gate"]
    assert [e["verdict"] for e in gate_events] == ["waived", "pass"], gate_events

    commit_events = [e for e in events if e["kind"] == "commit"]
    assert len(commit_events) == 2, commit_events
    assert "repro corrective round:" in commit_events[1]["text"], commit_events

    tamper_events = [e for e in events if e["kind"] == "tamper"]
    corrective_tamper = [e for e in tamper_events
                         if "[repro corrective round]" in e["text"]]
    assert len(corrective_tamper) == 1, tamper_events
    assert corrective_tamper[0]["tampered"] is False, corrective_tamper

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    assert attempts[0]["commit_sha"]


# --------------------------------------------------------------------------- #
# Review round 2 (D2): a billing wall INSIDE the corrective round is infra     #
# --------------------------------------------------------------------------- #


class _WallInCorrectiveBackend:
    """Coder turn is genuine (real fix, no manifest -> waived). The corrective
    round comes back as the CLI's own quota banner — an errored result with no
    tree change, exactly the shape `_quota_signal` parks on the coder turn."""

    def __init__(self, final_text: str, *, is_error: bool = True,
                 tokens_used: int = 0, num_turns: int = 0):
        self.calls = 0
        self._text = final_text
        self._is_error = is_error
        self._tokens = tokens_used
        self._turns = num_turns

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "calc.py"}))
            _mul_files(cwd)
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")
        return AgentResult(final_text=self._text, num_turns=self._turns,
                           is_error=self._is_error, tokens_used=self._tokens,
                           session_id="s2", stop_reason="error")


async def test_a_quota_wall_in_the_corrective_round_parks_and_spares_the_attempt(
        bare_repo, tmp_path, store):
    """RED before the fix: the wall left the tree unchanged, the second gate
    run read `waived` again, and the attempt FAILED "repro gate waived" with
    `infra_failure` NULL — a lifetime attempt consumed by a billing wall
    (the 2026-08-20 incident class, re-opened on this one path)."""
    from no_human.core.bounds import QuotaExhausted

    backend = _WallInCorrectiveBackend(
        "You've hit your session limit · resets 10:20pm (Asia/Jerusalem)")
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    with pytest.raises(QuotaExhausted):
        await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls == 2, "the corrective round must have run"
    gate_events = [e["verdict"] for e in events if e["kind"] == "repro_gate"]
    assert gate_events == ["waived"], (
        "the gate must not run a second time on a tree the wall left untouched")

    rows = await store.list_attempts(task.id)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["failure_reason"].startswith("quota: ")
    assert rows[0]["infra_failure"] == 1
    used_attempts, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 0, "a billing wall must not consume a lifetime attempt"


async def test_a_dead_sdk_session_in_the_corrective_round_is_infra_too(
        bare_repo, tmp_path, store):
    """The prose-less twin (`_infra_sdk_failure`): the SDK died before a
    single token — same park, reason prefixed `infra:`."""
    from no_human.core.bounds import QuotaExhausted

    backend = _WallInCorrectiveBackend(
        "Claude Code returned an error result: success")
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    with pytest.raises(QuotaExhausted):
        await orch._run_attempt(task, repo, 1, "main")

    rows = await store.list_attempts(task.id)
    assert rows[0]["status"] == "failed"
    assert rows[0]["failure_reason"].startswith("infra: ")
    assert rows[0]["infra_failure"] == 1


async def test_an_ordinary_errored_corrective_round_still_fails_as_waived(
        bare_repo, tmp_path, store):
    """Negative control: an errored round that is NEITHER a wall nor a dead
    session (a real tool failure with prose) keeps today's behaviour — the
    second gate run reads `waived`, the attempt fails `repro gate waived`,
    and the coder IS billed."""
    backend = _WallInCorrectiveBackend(
        "Error: the test command exited 2 — see output above",
        tokens_used=240, num_turns=3)
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.FAILED
    rows = await store.list_attempts(task.id)
    assert rows[0]["failure_reason"].startswith("repro gate waived")
    assert not rows[0]["infra_failure"]


# --------------------------------------------------------------------------- #
# Review round 2 (D1): a SERVER stop mid-corrective-round checkpoints         #
# --------------------------------------------------------------------------- #


class _ServerStopInCorrectiveBackend:
    """Coder turn is genuine. The corrective round writes a partial manifest
    edit and then observes the server stop, raising the sink's
    `CancelRequested` with the stop's own reason — the shape
    `_honor_cancel` must route to `_honor_server_stop` WITH the attempt id."""

    def __init__(self, orch_ref):
        self.calls = 0
        self._orch_ref = orch_ref

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        cwd = Path(cwd)
        if self.calls == 1:
            if on_event is not None:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "calc.py"}))
            _mul_files(cwd)
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")
        cwd.joinpath(".no_human").mkdir(exist_ok=True)
        (cwd / "notes.md").write_text("partial corrective work\n")
        self._orch_ref[0].request_server_stop()
        raise CancelRequested(SERVER_STOP_REASON)


async def test_a_server_stop_in_the_corrective_round_checkpoints_and_requeues(
        bare_repo, tmp_path, store):
    """RED before the fix: `_repro_gate_step`'s CancelRequested handler called
    `_honor_cancel` WITHOUT `attempt_id`, so `_honor_server_stop` took its
    cheap-boundary shape — no [WIP-PARTIAL] checkpoint, no `resume_from`,
    the row not spared as infra — and the next run re-implemented from base."""
    ref = []
    backend = _ServerStopInCorrectiveBackend(ref)
    orch, task, repo, events = await _run_one_bugfix_attempt(
        store, bare_repo, tmp_path, backend)
    ref.append(orch)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls == 2
    assert outcome.detail.startswith("interrupted: server stopping"), outcome.detail
    fresh = await store.find_task(task.id)
    # The gate step runs under REVIEWING. `_honor_server_stop` normalises only
    # PLANNING (worker-only AND not claimable); REVIEWING is left as-is on
    # purpose — it is in `Scheduler._ORPHANABLE`, so the next startup's orphan
    # sweep requeues it, resuming from the checkpoint stamped below. What must
    # NOT happen is a park: no USER_PAUSED blocker, no wake.
    assert fresh.status is TaskStatus.REVIEWING, fresh.status
    assert (fresh.blocker or {}).get("category") != "USER_PAUSED"
    assert fresh.wake_check_at is None
    rf = (fresh.context or {}).get("resume_from") or {}
    assert rf.get("by") == "server_stop", rf
    assert rf.get("sha"), "the mid-round checkpoint must be stamped"
    rows = await store.list_attempts(task.id)
    assert len(rows) == 1
    assert rows[0]["status"] == "interrupted"
    assert rows[0]["infra_failure"] == 1
    assert rows[0]["commit_sha"] == rf["sha"]
