"""Auth/limit SDK errors are INFRA, not work.

INCIDENT (2026-08-13 17:14-18:15): the operator's subscription hit its weekly
limit and the server kept dispatching. 7 tasks failed BUDGET_EXHAUSTED at
attempts 9/9 — four of them (79183501, 2893fa27, e58a81d6, edeff716) with
ZERO tokens across ALL 9 attempts. Every dispatch died in the SDK with the
bare ``Exception: Claude Code returned an error result: success`` (the
subtype "success" has nothing to do with the real cause: the account's
weekly-limit banner never reached the SDK's own error text at all). The
existing ``paused_quota`` machinery did NOT engage for this shape — it did
engage for the Aug 12 session-quota cut, so classification is shape-sensitive
— and every dead dispatch burned a lifetime attempt on a wall none of them
could ever get past.

Follows ``tests/test_reviewer_infra_park.py``'s idiom: a fake backend
returning ``AgentResult``, a ``Store`` on ``tmp_path``, ``asyncio_mode =
"auto"`` (set repo-wide) so no ``@pytest.mark.asyncio`` is needed.
"""

from __future__ import annotations

import subprocess

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.blockers import BlockerCategory
from no_human.config import load_config
from no_human.core.bounds import Bounds, QuotaExhausted
from no_human.core.db import Store
from no_human.core.infra_breaker import InfraBreaker, infra_breaker
from no_human.core.orchestrator import Orchestrator, _infra_sdk_failure
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo

# Replayed verbatim from ~/.no_human/server.out during the incident.
_INCIDENT_TEXT = "Exception: Claude Code returned an error result: success"
_GENUINE_FAILURE = (
    "Traceback (most recent call last):\n  File \"x.py\", line 1\n"
    "TypeError: 'bool' object is not subscriptable"
)


@pytest.fixture(autouse=True)
def _clean_infra_breaker_singleton():
    """The breaker is a process-wide singleton (`infra_breaker.py`'s module
    docstring explains why); reset it around every test in this file so one
    test's infra failures can never leak into the next one's assertions."""
    infra_breaker().reset()
    yield
    infra_breaker().reset()


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


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
    return cfg


class _ScriptedBackend:
    """Stands in for ClaudeBackend: always returns the same scripted
    `AgentResult`, whatever attempt asks for it."""

    def __init__(self, result: AgentResult):
        self._result = result
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        return self._result


def _incident_result(**overrides) -> AgentResult:
    fields = dict(
        final_text=_INCIDENT_TEXT, num_turns=1, is_error=True, tokens_used=0,
        session_id=None, stop_reason="error", cache_read_tokens=0,
        cache_creation_tokens=0,
    )
    fields.update(overrides)
    return AgentResult(**fields)


def _genuine_failure_result(**overrides) -> AgentResult:
    fields = dict(
        final_text=_GENUINE_FAILURE, num_turns=22, is_error=True,
        tokens_used=140_000, session_id="s", stop_reason="error",
        cache_read_tokens=1_000, cache_creation_tokens=500,
    )
    fields.update(overrides)
    return AgentResult(**fields)


# --------------------------------------------------------------------------- #
# (1)/(2)/(3) — the pure classifier                                           #
# --------------------------------------------------------------------------- #


def test_incident_shape_is_classified_infra():
    """The incident's exact shape: zero tokens, turn 1, no cache. RED before
    the fix — `_infra_sdk_failure` does not exist on unfixed code."""
    result = _incident_result()
    assert _infra_sdk_failure(result) is not None


def test_genuine_failure_with_tokens_is_not_infra():
    """Known-positive control (criterion 2): a real traceback with real spend
    is never infra, whatever it looks like otherwise."""
    result = _genuine_failure_result()
    assert _infra_sdk_failure(result) is None


@pytest.mark.parametrize("api_error_status,text", [
    (401, "some SDK/transport failure"),
    (403, "some SDK/transport failure"),
    (429, "some SDK/transport failure"),
    (None, "Error: invalid token supplied to the API"),
    (None, "Error: payment required to continue this subscription"),
    (None, "You've hit your weekly limit — try again later"),
    (None, "the account suspended message came back from the API"),
])
def test_auth_and_limit_signatures_are_infra(api_error_status, text):
    """The text/status arm proven independently of the zero-token arm: real
    turns, real tokens, but the SDK named an auth/billing wall."""
    result = AgentResult(
        final_text=text, num_turns=10, is_error=True, tokens_used=5_000,
        session_id="s", stop_reason="error", cache_read_tokens=100,
        cache_creation_tokens=0, api_error_status=api_error_status,
    )
    assert _infra_sdk_failure(result) is not None, (api_error_status, text)


def test_a_healthy_result_is_never_infra():
    result = AgentResult(
        final_text="done", num_turns=5, is_error=False, tokens_used=1_000,
        session_id="s", stop_reason="end_turn", cache_read_tokens=0,
        cache_creation_tokens=0,
    )
    assert _infra_sdk_failure(result) is None


def test_a_real_max_turns_exhaustion_with_tokens_is_not_infra():
    """A turn-starved session that DID stream tokens is a real, chargeable
    failure — the structural zero-token check must not also catch it."""
    result = AgentResult(
        final_text="ran out of turns", num_turns=1, is_error=True,
        tokens_used=0, session_id="s", stop_reason="max_turns",
        cache_read_tokens=0, cache_creation_tokens=0,
    )
    assert _infra_sdk_failure(result) is None


# --------------------------------------------------------------------------- #
# (4)/(6) — attempt-level accounting, driven through the real `_run_attempt`   #
# --------------------------------------------------------------------------- #


async def _run_one_attempt(store, bare_repo, tmp_path, result: AgentResult):
    cfg = _config(tmp_path)
    backend = _ScriptedBackend(result)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    # `_run_attempt` transitions the task to IMPLEMENTING itself — only legal
    # from PLANNING (the main-flow spine, `core/task.py`), so walk it there
    # the way `_drive` would have before reaching the attempt loop.
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)
    return orch, backend, task, repo


async def test_incident_attempt_leaves_lifetime_attempts_unchanged(
        store, bare_repo, tmp_path):
    """Criterion 1, first half — RED before the fix: today this attempt is
    charged (`used_attempts == 1`)."""
    orch, backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())

    with pytest.raises(QuotaExhausted):
        await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls, "the backend never ran — the test proves nothing"
    used_attempts, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 0, (
        "a dead SDK dispatch charged a lifetime attempt")
    assert await store.count_attempts(task.id) == 1, (
        "the attempt row must still exist — it is the only durable record")


async def test_genuine_failure_still_consumes_an_attempt_and_fails(
        store, bare_repo, tmp_path):
    """Criterion 2 — must be GREEN both before and after: no weakening of
    honest failure accounting."""
    orch, backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _genuine_failure_result())

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls, "the backend never ran — the test proves nothing"
    assert outcome.status == TaskStatus.FAILED
    used_attempts, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 1
    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    assert not attempts[0].get("infra_failure"), (
        "a genuine failure must never be flagged infra")


# --------------------------------------------------------------------------- #
# (5) — full `run_task` replay: paused, not failed                            #
# --------------------------------------------------------------------------- #


async def test_incident_task_is_paused_not_failed(store, bare_repo, tmp_path):
    """Criterion 1, second half. Mirrors the assertion shape at
    `tests/test_e2e_orchestrator.py:6510`."""
    cfg = _config(tmp_path)
    backend = _ScriptedBackend(_incident_result())
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)

    outcome = await orch.run_task(task)
    parked = await store.get_task(task.id)

    assert backend.calls, "the backend never ran — the test proves nothing"
    assert outcome.status is TaskStatus.PAUSED_QUOTA, (
        f"an auth/limit wall burned a lifetime attempt instead of parking: "
        f"{outcome.status} {outcome.detail}")
    assert parked.blocker["wake_condition"] == "quota_refreshed"
    assert parked.wake_check_at, "a quota park with no re-check stamp never wakes"
    used_attempts, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 0


# --------------------------------------------------------------------------- #
# (7) — the exact incident arithmetic                                         #
# --------------------------------------------------------------------------- #


def _orch(store):
    o = Orchestrator.__new__(Orchestrator)
    o.store = store
    o.bounds = Bounds.from_config({})
    o.config = {}
    o._sink = lambda e: None
    return o


async def test_nine_incident_attempts_would_not_exhaust_the_budget(store):
    """Regression on the exact incident arithmetic: 9 dead dispatches must
    not trip BUDGET_EXHAUSTED, while 9 genuine ones still do (the default cap
    is `lifetime_attempts: 9`, bounds.py — unchanged by this fix)."""
    infra_task = Task.new("infra-only", repo_path="/tmp/x")
    await store.create_task(infra_task)
    for n in range(1, 10):
        aid = await store.create_attempt(infra_task.id, n)
        await store.update_attempt(
            aid, status="failed", infra_failure=1,
            tokens_used=0, cache_read_tokens=0, cache_creation_tokens=0)

    assert await _orch(store)._check_lifetime_budget(infra_task) is None, (
        "9 infra-classified attempts must never exhaust the lifetime budget")

    genuine_task = Task.new("genuine", repo_path="/tmp/x")
    await store.create_task(genuine_task)
    for n in range(1, 10):
        aid = await store.create_attempt(genuine_task.id, n)
        await store.update_attempt(
            aid, status="failed", tokens_used=100,
            cache_read_tokens=0, cache_creation_tokens=0)

    blocker = await _orch(store)._check_lifetime_budget(genuine_task)
    assert blocker is not None
    assert blocker.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "attempts 9/9" in blocker.root_cause_hypothesis


# --------------------------------------------------------------------------- #
# (8) — the fleet breaker, in isolation                                       #
# --------------------------------------------------------------------------- #


def test_breaker_trips_on_three_distinct_tasks_and_not_on_one():
    distinct = InfraBreaker()
    assert distinct.tripped() is None
    distinct.record_infra_failure("t1")
    assert distinct.tripped() is None
    distinct.record_infra_failure("t2")
    assert distinct.tripped() is None
    just_tripped = distinct.record_infra_failure("t3")
    assert just_tripped is True
    assert distinct.tripped() is not None

    one_task = InfraBreaker()
    one_task.record_infra_failure("same")
    one_task.record_infra_failure("same")
    one_task.record_infra_failure("same")
    assert one_task.tripped() is None, (
        "one task retrying itself is that task's problem, not the fleet's")

    healed = InfraBreaker()
    healed.record_infra_failure("t1")
    healed.record_infra_failure("t2")
    healed.record_healthy()
    healed.record_infra_failure("t3")
    assert healed.tripped() is None, (
        "record_healthy() must reset the consecutive streak")
