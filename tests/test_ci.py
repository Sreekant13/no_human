"""CI module: parsers, GitLab trigger/poll logic, orchestrator wiring."""

from __future__ import annotations

import json

import pytest

from no_human.ci.base import CIResult, JobResult, PipelineStatus
from no_human.ci.gitlab import GitLabCI, _is_infra_failure, _parse_trigger_output
from no_human.ci.parser import parse_pytest, parse_results, parse_surefire
from no_human.ci import ci_from_config


# --------------------------------------------------------------------------- #
# Result parsers                                                               #
# --------------------------------------------------------------------------- #

def test_parse_pytest_summary():
    text = "42 passed, 3 failed, 1 error in 12.3s"
    assert parse_pytest(text) == (42, 3, 1)


def test_parse_pytest_no_results():
    assert parse_pytest("no output") == (0, 0, 0)


def test_parse_surefire_single_class():
    text = "Tests run: 5, Failures: 1, Errors: 0, Skipped: 0"
    passed, failed, errors = parse_surefire(text)
    assert passed == 4
    assert failed == 1
    assert errors == 0


def test_parse_surefire_multiple_classes():
    text = (
        "Tests run: 10, Failures: 0, Errors: 0\n"
        "Tests run: 5, Failures: 2, Errors: 1\n"
    )
    passed, failed, errors = parse_surefire(text)
    assert passed == 12   # 15 total - 2 failures - 1 error
    assert failed == 2
    assert errors == 1


def test_parse_results_dispatch():
    pytest_text = "5 passed"
    surefire_text = "Tests run: 5, Failures: 0, Errors: 0"
    assert parse_results(pytest_text, "pytest")[0] == 5
    assert parse_results(surefire_text, "surefire")[0] == 5


# --------------------------------------------------------------------------- #
# Trigger output parsing                                                       #
# --------------------------------------------------------------------------- #

def test_parse_trigger_url():
    text = "Created pipeline https://gitlab.acme.net/group/repo/-/pipelines/12345\n"
    pid, url = _parse_trigger_output(text)
    assert pid == "12345"
    assert "12345" in url


def test_parse_trigger_id_only():
    pid, url = _parse_trigger_output("Pipeline #42 created")
    assert pid == "42"


def test_parse_trigger_no_match():
    pid, url = _parse_trigger_output("Error: not found")
    assert pid == ""
    assert url == ""


# --------------------------------------------------------------------------- #
# Infra failure detection                                                      #
# --------------------------------------------------------------------------- #

def test_infra_failure_all_infra_reasons():
    jobs = [
        JobResult("test", "failed", "runner_system_failure"),
        JobResult("build", "failed", "stuck_or_timeout_failure"),
    ]
    assert _is_infra_failure(jobs) is True


def test_infra_failure_mixed_reasons():
    jobs = [
        JobResult("test", "failed", "runner_system_failure"),
        JobResult("coverage", "failed", None),  # real failure
    ]
    assert _is_infra_failure(jobs) is False


def test_infra_failure_no_failed_jobs():
    jobs = [JobResult("test", "success", None)]
    assert _is_infra_failure(jobs) is False


def test_infra_failure_no_reason_is_real():
    jobs = [JobResult("test", "failed", None)]
    assert _is_infra_failure(jobs) is False


# --------------------------------------------------------------------------- #
# PipelineStatus                                                               #
# --------------------------------------------------------------------------- #

def test_pipeline_status_terminal():
    assert PipelineStatus.SUCCESS.is_terminal
    assert PipelineStatus.FAILED.is_terminal
    assert not PipelineStatus.RUNNING.is_terminal
    assert not PipelineStatus.PENDING.is_terminal


def test_ci_result_summary_pass():
    r = CIResult("123", "https://x/123", PipelineStatus.SUCCESS)
    assert "PASS" in r.summary
    assert "123" in r.summary


def test_ci_result_summary_infra():
    r = CIResult("", "", PipelineStatus.FAILED, infra_failure=True)
    assert "INFRA" in r.summary


# --------------------------------------------------------------------------- #
# GitLabCI with a fake subprocess runner                                       #
# --------------------------------------------------------------------------- #

class FakeRunner:
    """Scripted sequence of responses to CI API calls."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> str:
        self.calls.append(cmd)
        if self._responses:
            return self._responses.pop(0)
        return ""


def _make_pipeline_response(status: str) -> str:
    return json.dumps({"id": 99, "status": status, "web_url": "https://x/99"})


def _make_jobs_response(jobs: list[dict]) -> str:
    return json.dumps(jobs)


def test_gitlab_ci_success():
    fake = FakeRunner([
        # trigger: glab ci run → outputs pipeline URL
        "Created pipeline https://gitlab.sn.net/g/p/-/pipelines/99\n",
        # poll 1: running
        _make_pipeline_response("running"),
        # poll 2: success
        _make_pipeline_response("success"),
        # get jobs
        _make_jobs_response([{"name": "test", "status": "success", "failure_reason": None,
                               "web_url": ""}]),
    ])
    ci = GitLabCI("g/p", hostname="gitlab.sn.net", poll_interval=0, _run_cmd=fake)
    result = ci._trigger_and_wait("no-human/branch", {})
    assert result.passed
    assert result.pipeline_id == "99"
    assert not result.infra_failure


def test_gitlab_ci_real_failure():
    fake = FakeRunner([
        "https://gitlab.sn.net/g/p/-/pipelines/5\n",
        _make_pipeline_response("success"),  # status poll returns success
        # ... but then we call jobs and find failures with real reasons
    ])
    # Simulate a pipeline that reports failed with real test failures.
    fake2 = FakeRunner([
        "https://gitlab.sn.net/g/p/-/pipelines/5\n",
        _make_pipeline_response("failed"),
        _make_jobs_response([{"name": "test", "status": "failed",
                               "failure_reason": None, "web_url": ""}]),
    ])
    ci = GitLabCI("g/p", hostname="gitlab.sn.net", poll_interval=0, _run_cmd=fake2)
    result = ci._trigger_and_wait("branch", {})
    assert result.failed
    assert not result.infra_failure


def test_gitlab_ci_infra_failure():
    fake = FakeRunner([
        "https://gitlab.sn.net/g/p/-/pipelines/7\n",
        _make_pipeline_response("failed"),
        _make_jobs_response([{"name": "runner", "status": "failed",
                               "failure_reason": "runner_system_failure",
                               "web_url": ""}]),
    ])
    ci = GitLabCI("g/p", hostname="gitlab.sn.net", poll_interval=0, _run_cmd=fake)
    result = ci._trigger_and_wait("branch", {})
    assert result.failed
    assert result.infra_failure


def test_gitlab_ci_trigger_no_output_is_infra():
    # No pipeline ID in output → infra failure.
    fake = FakeRunner(["Error connecting to GitLab"])
    ci = GitLabCI("g/p", hostname="gitlab.sn.net", poll_interval=0, _run_cmd=fake)
    result = ci._trigger_and_wait("branch", {})
    assert result.infra_failure


async def test_gitlab_ci_infra_retry_succeeds_on_second_try():
    """Infra failure on first attempt, real success on second → passes."""
    call_count = [0]

    def fake_run(cmd):
        call_count[0] += 1
        if "ci" in cmd and "run" in cmd:
            return "https://x/pipelines/1\n"
        # First pipeline poll returns infra failure.
        if call_count[0] <= 3:
            if "pipelines/1" in (cmd[-1] if cmd else ""):
                return json.dumps({"status": "failed"})
            return json.dumps([{"name": "t", "status": "failed",
                                 "failure_reason": "runner_system_failure",
                                 "web_url": ""}])
        # After backoff trigger again.
        if "pipelines/2" in (cmd[-1] if cmd else ""):
            return json.dumps({"status": "success"})
        return json.dumps([{"name": "t", "status": "success",
                             "failure_reason": None, "web_url": ""}])

    # Use a scripted sequence instead — simpler to reason about.
    sequence = [
        # Trigger attempt 1: pipeline 10
        "https://x/pipelines/10\n",
        # Poll: failed
        json.dumps({"id": 10, "status": "failed"}),
        # Jobs: infra
        json.dumps([{"name": "t", "status": "failed",
                      "failure_reason": "runner_system_failure", "web_url": ""}]),
        # Trigger attempt 2 (after backoff): pipeline 11
        "https://x/pipelines/11\n",
        # Poll: success
        json.dumps({"id": 11, "status": "success"}),
        # Jobs: success
        json.dumps([{"name": "t", "status": "success",
                      "failure_reason": None, "web_url": ""}]),
    ]
    fake = FakeRunner(sequence)
    ci = GitLabCI("g/p", hostname="gitlab.sn.net", poll_interval=0,
                  max_infra_retries=1, _run_cmd=fake)

    # Patch asyncio.sleep to be instant in the test
    import unittest.mock
    with unittest.mock.patch("asyncio.sleep", return_value=None):
        result = await ci.trigger("branch", {})

    assert result.passed
    assert not result.infra_failure


async def test_gitlab_ci_infra_exhausted_returns_infra_result():
    """Infra failure on all attempts → CIResult with infra_failure=True."""
    def infra_sequence():
        while True:
            yield "https://x/pipelines/1\n"
            yield json.dumps({"status": "failed"})
            yield json.dumps([{"name": "t", "status": "failed",
                                "failure_reason": "stuck_or_timeout_failure",
                                "web_url": ""}])

    gen = infra_sequence()
    fake = FakeRunner([next(gen) for _ in range(9)])  # enough for 3 attempts
    ci = GitLabCI("g/p", hostname="gitlab.sn.net", poll_interval=0,
                  max_infra_retries=2, _run_cmd=fake)

    import unittest.mock
    with unittest.mock.patch("asyncio.sleep", return_value=None):
        result = await ci.trigger("branch", {})

    assert result.infra_failure


# --------------------------------------------------------------------------- #
# ci_from_config                                                               #
# --------------------------------------------------------------------------- #

def test_ci_from_config_disabled():
    assert ci_from_config({"ci": {"enabled": False}}) is None
    assert ci_from_config({}) is None


def test_ci_from_config_gitlab():
    cfg = {
        "ci": {
            "enabled": True,
            "backend": "gitlab",
            "project": "ci_gate/customer/metrics-core",
            "hostname": "gitlab.acme.net",
            "timeout_minutes": 30,
            "max_infra_retries": 1,
            "poll_interval": 10,
            "variables": {"ENV": "test"},
            "result_parser": "surefire",
        }
    }
    ci = ci_from_config(cfg)
    assert ci is not None
    assert ci.project == "ci_gate/customer/metrics-core"
    assert ci.hostname == "gitlab.acme.net"
    assert ci.max_infra_retries == 1
    assert ci.variables == {"ENV": "test"}
    assert ci.result_parser == "surefire"


def test_ci_from_config_no_project_returns_none():
    cfg = {"ci": {"enabled": True, "backend": "gitlab", "project": ""}}
    assert ci_from_config(cfg) is None


# --------------------------------------------------------------------------- #
# Orchestrator + CI integration (fake CI runner)                              #
# --------------------------------------------------------------------------- #

import subprocess as _subprocess

from no_human.agent.claude_backend import AgentResult
from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import ReviewDecision
from no_human.review.selfcheck import ChecklistItem


def _git(cwd, *args):
    _subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    _subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
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


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


class FakeBackend:
    def __init__(self, mutate=None):
        self._mutate = mutate or (lambda cwd: None)

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None, on_event=None):
        self._mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


class FakeReviewer:
    def __init__(self, decision):
        self._decision = decision

    async def review(self, task, *, repo_path, **kw):
        return self._decision


class FakeCI:
    def __init__(self, result: CIResult):
        self._result = result
        self.calls: list[str] = []
        self.max_infra_retries = 2

    async def trigger(self, branch, extra_variables=None):
        self.calls.append(branch)
        return self._result


def _good_mutate(cwd):
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
    )
    (cwd / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


def _passing_review():
    return ReviewDecision(passed=True, checklist=[
        ChecklistItem("ok", True, "calc.py:3"),
    ])


async def test_ci_pass_leads_to_awaiting_approval(bare_repo, tmp_path, store):
    """CI passes → AWAITING_APPROVAL (PR opened)."""
    ci_result = CIResult("42", "https://x/42", PipelineStatus.SUCCESS)
    cfg = load_config(tmp_path / "config.yaml")
    fake_ci = FakeCI(ci_result)
    orch = Orchestrator(
        store, cfg.data,
        FakeBackend(_good_mutate),
        SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()),
        ci_runner=fake_ci,
    )
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is not None
    assert fake_ci.calls  # CI was triggered
    # Attempt records CI fields.
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["ci_pipeline_id"] == "42"
    assert attempts[-1]["ci_status"] == "success"


async def test_ci_real_failure_loops_to_escalate(bare_repo, tmp_path, store):
    """CI real test failure → attempt FAILED → loops → ESCALATED after max_attempts."""
    ci_result = CIResult(
        "77", "https://x/77", PipelineStatus.FAILED, infra_failure=False,
        parsed_output="3 failed, 0 passed",
    )
    cfg = load_config(tmp_path / "config.yaml")
    fake_ci = FakeCI(ci_result)
    orch = Orchestrator(
        store, cfg.data,
        FakeBackend(_good_mutate),
        SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()),
        ci_runner=fake_ci,
    )
    t = Task.new("fix tests", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert "CI failed" in outcome.detail
    # CI was called once per attempt (3 attempts default).
    assert len(fake_ci.calls) == 3


async def test_ci_infra_failure_escalates_immediately(bare_repo, tmp_path, store):
    """CI infra failure (after internal retries) → ESCALATED immediately, not looped."""
    ci_result = CIResult(
        "", "", PipelineStatus.FAILED, infra_failure=True,
        parsed_output="runner_system_failure",
    )
    cfg = load_config(tmp_path / "config.yaml")
    fake_ci = FakeCI(ci_result)
    call_count = []
    original_trigger = fake_ci.trigger

    async def counting_trigger(branch, **kw):
        call_count.append(branch)
        return await original_trigger(branch, **kw)

    fake_ci.trigger = counting_trigger
    orch = Orchestrator(
        store, cfg.data,
        FakeBackend(_good_mutate),
        SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()),
        ci_runner=fake_ci,
    )
    t = Task.new("fix stuff", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert "infra" in outcome.detail.lower()
    # Infra failure escalates from the first attempt — no point burning attempts
    # on a scheduler that's down.
    assert len(call_count) == 1


async def test_no_ci_runner_skips_ci(bare_repo, tmp_path, store):
    """No ci_runner configured → pipeline runs without CI, reaches AWAITING_APPROVAL."""
    cfg = load_config(tmp_path / "config.yaml")
    orch = Orchestrator(
        store, cfg.data,
        FakeBackend(_good_mutate),
        SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()),
        ci_runner=None,
    )
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
