"""CI module: parsers, GitLab trigger/poll logic, orchestrator wiring."""

from __future__ import annotations

import asyncio
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


def test_gitlab_trigger_argv_is_api_post_never_ci_run():
    """Regression guard: the trigger must use the proven `glab api --method
    POST projects/<p>/pipeline --input <json>` form. `glab ci run` defaults to
    gitlab.com (401) and drops variables on gitlab.acme.net."""
    captured = {}

    def fake(cmd):
        captured["cmd"] = cmd
        body_path = cmd[cmd.index("--input") + 1]
        with open(body_path) as f:
            captured["body"] = json.load(f)
        return json.dumps({"id": 99, "web_url": "https://x/-/pipelines/99"})

    ci = GitLabCI("g/p", hostname="gitlab.acme.net", _run_cmd=fake)
    pid, url = ci._trigger("my-branch", {"K": "V"})
    cmd = captured["cmd"]
    assert pid == "99" and url == "https://x/-/pipelines/99"
    assert cmd[1] == "api"
    assert "--method" in cmd and cmd[cmd.index("--method") + 1] == "POST"
    assert cmd[cmd.index("--hostname") + 1] == "gitlab.acme.net"
    assert "projects/g%2Fp/pipeline" in cmd
    assert "ci" not in cmd and "run" not in cmd
    assert not any(a == "--variables" for a in cmd)
    assert captured["body"] == {"ref": "my-branch",
                                "variables": [{"key": "K", "value": "V"}]}


def test_parse_trigger_json_response():
    pid, url = _parse_trigger_output(
        json.dumps({"id": 9876543, "web_url": "https://h/p/-/pipelines/9876543"})
    )
    assert pid == "9876543"
    assert url == "https://h/p/-/pipelines/9876543"


def test_gitlab_ci_success():
    fake = FakeRunner([
        # trigger response (URL fallback parse path)
        "Created pipeline https://gitlab.acme.net/g/p/-/pipelines/99\n",
        # poll 1: running
        _make_pipeline_response("running"),
        # poll 2: success
        _make_pipeline_response("success"),
        # get jobs
        _make_jobs_response([{"name": "test", "status": "success", "failure_reason": None,
                               "web_url": ""}]),
    ])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._trigger_and_wait("no-human/branch", {})
    assert result.passed
    assert result.pipeline_id == "99"
    assert not result.infra_failure


def test_gitlab_ci_real_failure():
    fake = FakeRunner([
        "https://gitlab.acme.net/g/p/-/pipelines/5\n",
        _make_pipeline_response("success"),  # status poll returns success
        # ... but then we call jobs and find failures with real reasons
    ])
    # Simulate a pipeline that reports failed with real test failures.
    fake2 = FakeRunner([
        "https://gitlab.acme.net/g/p/-/pipelines/5\n",
        _make_pipeline_response("failed"),
        _make_jobs_response([{"name": "test", "status": "failed",
                               "failure_reason": None, "web_url": ""}]),
    ])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake2)
    result = ci._trigger_and_wait("branch", {})
    assert result.failed
    assert not result.infra_failure


def test_gitlab_ci_infra_failure():
    fake = FakeRunner([
        "https://gitlab.acme.net/g/p/-/pipelines/7\n",
        _make_pipeline_response("failed"),
        _make_jobs_response([{"name": "runner", "status": "failed",
                               "failure_reason": "runner_system_failure",
                               "web_url": ""}]),
    ])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._trigger_and_wait("branch", {})
    assert result.failed
    assert result.infra_failure


def test_gitlab_ci_trigger_no_output_is_infra():
    # No pipeline ID in output → infra failure.
    fake = FakeRunner(["Error connecting to GitLab"])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
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
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0,
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
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0,
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


def test_gitlab_check_status_terminal():
    """check_status returns terminal status with jobs when pipeline is done."""
    fake = FakeRunner([
        _make_pipeline_response("success"),
        _make_jobs_response([{"name": "test", "status": "success",
                               "failure_reason": None, "web_url": ""}]),
    ])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._check_pipeline("99")
    assert result.status == PipelineStatus.SUCCESS
    assert result.passed
    assert len(result.jobs) == 1


def test_gitlab_check_status_running():
    """check_status returns non-terminal status when pipeline is still running."""
    fake = FakeRunner([
        _make_pipeline_response("running"),
    ])
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._check_pipeline("99")
    assert result.status == PipelineStatus.RUNNING
    assert not result.status.is_terminal


def test_gitlab_check_status_api_failure():
    """check_status returns UNKNOWN when the API call fails."""
    fake = FakeRunner([""])  # empty response → _glab_api returns None
    ci = GitLabCI("g/p", hostname="gitlab.acme.net", poll_interval=0, _run_cmd=fake)
    result = ci._check_pipeline("99")
    assert result.status == PipelineStatus.UNKNOWN
    assert result.infra_failure


def test_gitlab_is_a_ci_backend():
    from no_human.ci.base import CIBackend
    assert issubclass(GitLabCI, CIBackend)
    assert GitLabCI(project="p").name == "gitlab"


def test_ci_from_config_github_actions_readonly():
    from no_human.ci import GitHubActionsCI
    cfg = {"ci": {"enabled": True, "backend": "github_actions",
                  "repo": "dev/query-service", "workflow": "ci.yml"}}
    ci = ci_from_config(cfg)
    assert isinstance(ci, GitHubActionsCI)
    assert ci.repo == "dev/query-service"
    # CI.1: no longer a seam — it's a READ-ONLY reader (max_infra_retries=0,
    # never `gh workflow run`). Read behavior is covered by the test_gha_* suite;
    # here we just confirm routing + that it does not retry a read.
    assert ci.max_infra_retries == 0


def test_ci_from_config_jenkins_defaults_to_watch():
    # Phase 6: Jenkins is now a real backend. The default mode is read-only
    # `watch` (poll the PR-triggered build); it is NOT human-gated by default.
    from no_human.ci import JenkinsCI
    cfg = {"ci": {"enabled": True, "backend": "jenkins", "job": "metrics-core-image"}}
    ci = ci_from_config(cfg)
    assert isinstance(ci, JenkinsCI)
    assert ci.mode == "watch"


def test_ci_jenkins_human_gated_mode_still_parks():
    # The image-build prerequisite is preserved as an OPT-IN mode that parks the
    # task with a wake hint rather than faking the step (constraint §3.4).
    from no_human.ci import HumanGatedCI, JenkinsCI
    cfg = {"ci": {"enabled": True, "backend": "jenkins", "job": "metrics-core-image",
                  "mode": "human_gated"}}
    ci = ci_from_config(cfg)
    assert isinstance(ci, JenkinsCI)
    with pytest.raises(HumanGatedCI) as exc:
        asyncio.run(ci.trigger("no-human/x"))
    assert exc.value.wake_hint  # carries a wake hint for the orchestrator to park on


def test_ci_from_config_unknown_backend_raises():
    with pytest.raises(ValueError, match="unknown ci.backend"):
        ci_from_config({"ci": {"enabled": True, "backend": "bamboo"}})


def test_all_backends_expose_max_infra_retries():
    # The orchestrator reads ci_runner.max_infra_retries unconditionally; every
    # backend must have it (constraint #5: never crash). Jenkins is now a real
    # backend that retries infra failures like the others.
    from no_human.ci import GitHubActionsCI, JenkinsCI
    assert GitLabCI(project="p").max_infra_retries >= 0
    assert GitHubActionsCI(repo="o/r").max_infra_retries >= 0
    assert JenkinsCI(job="j").max_infra_retries >= 0


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

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None, on_event=None,
                  supervisor_hook=None, **kwargs):
        self._mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


class FakeReviewer:
    def __init__(self, decision):
        self._decision = decision

    async def review(self, task, *, repo_path, **kw):
        return self._decision


class FakeCI:
    name = "fake-ci"

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


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
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


@pytest.mark.slow
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


@pytest.mark.slow
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


async def test_ci_unrelated_failure_escalates_immediately(bare_repo, tmp_path, store):
    """Phase 6.3: a red build whose failing tests are not in any changed file is a
    pre-existing/monorepo failure → escalate from the first attempt with cited
    evidence, NOT loop trying to fix code we didn't write."""
    ci_result = CIResult(
        "88", "https://x/88", PipelineStatus.FAILED, infra_failure=False,
        jobs=[JobResult(name="com.acme.billing.InvoiceIT.testTotals",
                        status="failed", failure_reason="pre-existing")],
        parsed_output="1 failing test: com.acme.billing.InvoiceIT.testTotals",
    )
    cfg = load_config(tmp_path / "config.yaml")
    fake_ci = FakeCI(ci_result)
    orch = Orchestrator(
        store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()), ci_runner=fake_ci,
    )
    t = Task.new("add mul()", repo_path=str(bare_repo))  # touches calc.py/test_calc.py
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert len(fake_ci.calls) == 1  # did NOT burn 3 attempts
    t2 = await store.get_task(t.id)
    assert "InvoiceIT" in (t2.blocker or {}).get("evidence", "")


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_concurrency_worktree_mode_opens_pr_and_cleans_up(bare_repo, tmp_path, store):
    """Phase 7.2: with concurrency.enabled the task runs in its own worktree,
    still reaches AWAITING_APPROVAL with a PR, and the worktree is removed."""
    from no_human.vcs import GitRepo
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["concurrency"] = {"enabled": True,
                               "worktree_root": str(tmp_path / "wt")}
    orch = Orchestrator(
        store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()), ci_runner=None,
    )
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul works"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is not None
    # Worktree cleaned up: only the primary checkout remains.
    main = GitRepo(bare_repo)
    assert all("/wt/" not in w for w in main.list_worktrees())
    assert not (tmp_path / "wt" / t.id).exists()
    # The agent worked in the worktree, never the primary checkout.
    assert "mul" not in (bare_repo / "calc.py").read_text()


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_ci_access_failure_routes_to_missing_access(bare_repo, tmp_path, store):
    """Phase 6 / (a): a credential/permission wall (not infra, not code) parks as
    MISSING_ACCESS with an ask, and is NOT retried."""
    ci_result = CIResult(
        "", "https://x/jenkins/job", PipelineStatus.FAILED,
        access_failure=True,
        parsed_output="Jenkins denied access (401/403). Set JENKINS_API_TOKEN …",
    )
    cfg = load_config(tmp_path / "config.yaml")
    fake_ci = FakeCI(ci_result)
    orch = Orchestrator(
        store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()), ci_runner=fake_ci,
    )
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert len(fake_ci.calls) == 1  # access is not retried
    t2 = await store.get_task(t.id)
    assert (t2.blocker or {}).get("category") == "MISSING_ACCESS"
    assert "access" in (t2.blocker or {}).get("question", "").lower()


@pytest.mark.slow
async def test_ci_related_failure_threads_into_next_prompt(bare_repo, tmp_path, store):
    """Phase 6.2: a real failure in a file we changed feeds the next attempt's
    prompt so the agent fixes the ACTUAL remote failure."""
    ci_result = CIResult(
        "91", "https://x/91", PipelineStatus.FAILED, infra_failure=False,
        jobs=[JobResult(name="calc.test_mul", status="failed",
                        failure_reason="AssertionError: mul(2,3)==6")],
        parsed_output="1 failing test: calc.test_mul",
    )
    cfg = load_config(tmp_path / "config.yaml")
    fake_ci = FakeCI(ci_result)
    prompts: list[str] = []

    class CapturingBackend(FakeBackend):
        async def run(self, prompt, **kw):
            prompts.append(prompt)
            return await super().run(prompt, **kw)

    orch = Orchestrator(
        store, cfg.data, CapturingBackend(_good_mutate), SlackNotifier(None),
        reviewer=FakeReviewer(_passing_review()), ci_runner=fake_ci,
    )
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    # First attempt got no CI context; the second (after the red build) does.
    assert len(prompts) >= 2
    assert "remote ci build" in prompts[1].lower()
    assert "calc.test_mul" in prompts[1]


# --------------------------------------------------------------------------- #
# CI.1 — GitHub Actions READ-ONLY reader (Phase 2C)                            #
# --------------------------------------------------------------------------- #
from no_human.ci.github_actions import GitHubActionsCI, _is_auth_error  # noqa: E402


def _cr(name, conclusion, status="completed", url=""):
    """Build one raw GitHub check-run dict."""
    return {"name": name, "conclusion": conclusion, "status": status, "html_url": url}


def _statuses(state="pending", entries=None):
    """A raw ``/commits/{ref}/status`` payload. Default = zero statuses (no CI on
    that surface), which reads as UNKNOWN/no-jobs and is dropped by the merge."""
    entries = entries or []
    return {"state": state, "statuses": entries, "total_count": len(entries)}


def _st(context, state, url=""):
    """Build one raw commit-status entry."""
    return {"context": context, "state": state, "target_url": url}


def _gha(runs=None, *, raises=None, poll_interval=0, statuses=None,
         statuses_raises=None):
    """A GitHubActionsCI whose fetches are stubbed — never touches the network.

    ``statuses`` defaults to an empty commit-status payload so existing check-run
    tests keep their exact verdict (the empty status surface is dropped by the
    merge)."""
    async def fake_fetch(repo, ref, *, hostname=""):
        if raises is not None:
            raise raises
        return runs or []

    async def fake_statuses(repo, ref, *, hostname=""):
        if statuses_raises is not None:
            raise statuses_raises
        return statuses if statuses is not None else _statuses()

    return GitHubActionsCI("owner/repo", poll_interval=poll_interval,
                           fetch_runs=fake_fetch, fetch_statuses=fake_statuses)


def test_gha_all_success_is_pass():
    ci = _gha([_cr("build", "success"), _cr("test", "success")])
    result = asyncio.run(ci.trigger("branch"))
    assert result.passed
    assert result.status == PipelineStatus.SUCCESS
    assert not result.infra_failure and not result.access_failure


def test_gha_any_failure_is_fail_not_infra():
    ci = _gha([_cr("build", "success"), _cr("test", "failure")])
    result = asyncio.run(ci.trigger("branch"))
    assert result.failed
    assert not result.infra_failure  # a real test failure is NOT infra
    assert not result.passed


def test_gha_no_checks_is_unknown_never_green():
    # THE no-CI disambiguation: a repo with no checks must never read as green.
    ci = _gha([])
    result = asyncio.run(ci.trigger("branch"))
    assert result.status == PipelineStatus.UNKNOWN
    assert not result.passed
    assert not result.infra_failure and not result.access_failure


def test_gha_missing_token_is_access_failure_not_infra():
    ci = _gha(raises=RuntimeError("gh api failed (1): HTTP 401: Bad credentials"))
    result = asyncio.run(ci.trigger("branch"))
    assert result.access_failure
    assert result.access_env_key == "GH_TOKEN"
    assert not result.infra_failure  # a missing token is NOT transient — don't retry
    assert not result.passed


def test_gha_forbidden_is_access_failure():
    ci = _gha(raises=RuntimeError("gh api failed (1): HTTP 403: Resource not accessible by personal access token"))
    result = asyncio.run(ci.trigger("branch"))
    assert result.access_failure and not result.infra_failure


def test_gha_transient_error_is_infra_failure():
    ci = _gha(raises=RuntimeError("gh api failed (1): HTTP 502 Bad Gateway"))
    result = asyncio.run(ci.trigger("branch"))
    assert result.infra_failure
    assert not result.access_failure  # a 5xx IS transient — retryable
    assert not result.passed


def test_gha_check_status_in_progress_is_running():
    ci = _gha([_cr("test", "", status="in_progress")])
    result = asyncio.run(ci.check_status("branch"))
    assert result.status == PipelineStatus.RUNNING
    assert not result.passed


# --- CI.1b: Commit-Status API merged into the reader -------------------------

def test_gha_commit_status_only_green_is_pass():
    # No check-runs at all, but the Commit Status API reports success -> green.
    ci = _gha([], statuses=_statuses("success", [_st("jenkins", "success")]))
    result = asyncio.run(ci.trigger("branch"))
    assert result.passed
    assert [j.name for j in result.jobs] == ["jenkins"]


def test_gha_checkruns_green_and_no_statuses_is_green():
    # Green check-runs + zero commit-statuses (the common case) stays green:
    # the empty status surface is "no opinion", not a failure.
    ci = _gha([_cr("build", "success")], statuses=_statuses("pending", []))
    result = asyncio.run(ci.trigger("branch"))
    assert result.passed


def test_gha_commit_status_failure_overrides_checkrun_pass():
    # A failing external status must sink an otherwise-green check-run set — a
    # green on one surface may NEVER hide a failure on the other.
    ci = _gha([_cr("build", "success")],
              statuses=_statuses("failure", [_st("jenkins", "failure")]))
    result = asyncio.run(ci.trigger("branch"))
    assert result.failed
    assert not result.passed
    assert not result.infra_failure


def test_gha_both_surfaces_empty_is_unknown_never_green():
    # No check-runs AND no commit-statuses -> UNKNOWN, never a false pass.
    ci = _gha([], statuses=_statuses("pending", []))
    result = asyncio.run(ci.trigger("branch"))
    assert result.status == PipelineStatus.UNKNOWN
    assert not result.passed
    assert not result.infra_failure and not result.access_failure


def test_gha_pending_commit_status_is_running():
    # A real pending status (>=1 entry) is RUNNING — distinct from the empty
    # "pending with zero statuses" that means no-CI.
    ci = _gha([_cr("build", "success")],
              statuses=_statuses("pending", [_st("jenkins", "pending")]))
    result = asyncio.run(ci.check_status("branch"))
    assert result.status == PipelineStatus.RUNNING
    assert not result.passed


def test_gha_status_auth_wall_is_not_dropped_to_green():
    # If the commit-status read hits an auth wall it must NOT be silently dropped
    # to let green check-runs vouch for the commit — the wall keeps it UNKNOWN.
    ci = _gha([_cr("build", "success")],
              statuses_raises=RuntimeError("gh api failed (1): HTTP 401: Bad credentials"))
    result = asyncio.run(ci.check_status("branch"))
    assert result.access_failure
    assert result.status == PipelineStatus.UNKNOWN
    assert not result.passed


def test_gha_trigger_is_read_only_never_raises_notimplemented():
    # The old stub raised NotImplementedError; the read-only path must not.
    ci = _gha([_cr("test", "success")])
    result = asyncio.run(ci.trigger("branch"))  # would raise if still a stub
    assert result.passed


def test_gha_is_a_ci_backend_and_routes_from_config():
    from no_human.ci.base import CIBackend
    ci = ci_from_config({"ci": {"enabled": True, "backend": "github_actions",
                                 "repo": "owner/repo"}})
    assert isinstance(ci, GitHubActionsCI)
    assert isinstance(ci, CIBackend)


def test_is_auth_error_classifier():
    # Genuine auth walls -> True.
    assert _is_auth_error("gh api failed (1): HTTP 401: Bad credentials")
    assert _is_auth_error("gh api failed (1): HTTP 403: forbidden")
    assert _is_auth_error("Resource not accessible by integration")
    # Transient conditions that carry a 403/404 -> NOT auth (must stay retryable).
    assert not _is_auth_error("HTTP 403: API rate limit exceeded for user 12345")
    assert not _is_auth_error("HTTP 403: You have exceeded a secondary rate limit")
    assert not _is_auth_error("HTTP 404: No commit found for SHA deadbeef")
    # Bare digits inside byte counts / ms timers must NOT match (substring bug).
    assert not _is_auth_error("gh api failed: i/o timeout after 40130ms")
    assert not _is_auth_error("gh api failed: read 1403 bytes then reset")
    assert not _is_auth_error("HTTP 502 Bad Gateway")
    assert not _is_auth_error("connection reset by peer")


def test_gha_startup_failure_is_not_green():
    # A workflow that failed to START (invalid YAML, etc.) never ran the tests —
    # it must NOT read as green through the reader.
    ci = _gha([_cr("ci", "startup_failure")])
    result = asyncio.run(ci.trigger("branch"))
    assert result.status == PipelineStatus.UNKNOWN
    assert not result.passed


def test_gha_rate_limit_403_is_infra_not_access():
    ci = _gha(raises=RuntimeError("gh api failed (1): HTTP 403: API rate limit exceeded"))
    result = asyncio.run(ci.trigger("branch"))
    assert result.infra_failure       # transient -> retryable
    assert not result.access_failure  # NOT a permanent human park
    assert not result.passed


def test_gha_no_commit_404_is_infra_not_access():
    ci = _gha(raises=RuntimeError("gh api failed (1): HTTP 404: No commit found for SHA x"))
    result = asyncio.run(ci.trigger("branch"))
    assert result.infra_failure
    assert not result.access_failure  # a token won't fix a missing SHA
