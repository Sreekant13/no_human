"""The review gate fails closed when no reviewer is wired (M0.3).

The reviewer is the only gate between an unreviewed diff and a PR. Returning a
passing decision when it is absent turns the hard gate into a silent rubber
stamp — CLAUDE.md #3. `nh watch` did exactly that in production.
"""

import ast
import pathlib

import pytest

from no_human.config import DEFAULT_CONFIG, load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import ReviewerUnavailable

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo, store  # noqa: F401


def _good_mutate(cwd):
    """A real, passing diff — so the task reaches the review gate rather than
    tripping the zero-diff breaker first."""
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )
    (cwd / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


def test_default_config_fails_closed():
    assert DEFAULT_CONFIG["reviewer"]["allow_advisory"] is False


async def test_run_review_raises_when_no_reviewer_is_wired(store, tmp_path):
    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = False
    orch = Orchestrator(store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None))
    with pytest.raises(ReviewerUnavailable, match="rubber stamp"):
        await orch._run_review(Task.new("t", repo_path="/r"), None, "attempt-1")


async def test_advisory_pass_through_requires_the_explicit_flag(store, tmp_path):
    """Opting in is allowed for eval/replay — but it is announced, never silent."""
    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = True
    events = []
    orch = Orchestrator(
        store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None),
        event_sink=events.append,
    )
    decision = await orch._run_review(Task.new("t", repo_path="/r"), None, "attempt-1")

    assert decision.passed is True
    (advisory,) = [e for e in events if e["kind"] == "review_advisory"]
    assert "NOT reviewed" in advisory["text"]


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_a_missing_reviewer_escalates_instead_of_opening_a_pr(
    store, bare_repo, tmp_path
):
    """End to end: reverting the fail-closed guard lets this task reach
    AWAITING_APPROVAL with an unreviewed diff."""
    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = False
    orch = Orchestrator(store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None))

    task = Task.new("add add()", repo_path=str(bare_repo))
    task.acceptance_criteria = ["add(a,b) returns a+b"]
    await store.create_task(task)
    outcome = await orch.run_task(task)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None
    assert "no reviewer is configured" in outcome.detail


def test_no_production_orchestrator_is_built_without_a_reviewer():
    """`nh watch` built its own Orchestrator and forgot the reviewer, so it drove
    tasks to a PR with the gate pass-through. Nothing caught it: the constructor
    defaults `reviewer=None`. This is the guard that would have."""
    missing = []
    for path in pathlib.Path("src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "Orchestrator"
                and "reviewer" not in {kw.arg for kw in node.keywords}
            ):
                missing.append(f"{path}:{node.lineno}")
    assert not missing, (
        "production code must always wire the review gate; missing at: "
        + ", ".join(missing)
    )


# ------- the reviewer that reached no verdict (task 84251cb2, attempt 13) ---- #


class _FakeAgentResult:
    def __init__(self, final_text, *, is_error=False, stop_reason="end_turn"):
        self.final_text = final_text
        self.is_error = is_error
        self.stop_reason = stop_reason
        self.num_turns = 11
        self.tokens_used = 0
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0
        self.session_id = "s"


class _ReviewerBackend:
    """Replays a scripted sequence of reviewer sessions."""

    def __init__(self, *results):
        self._results = list(results)
        self.budgets: list[int] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, on_event=None, **kw):
        self.budgets.append(max_turns)
        return self._results.pop(0)


# NB: the parser reads "items", not "checklist". This fixture originally used
# "checklist", so items parsed as [] — and the test only passed because of the
# vacuous-pass bug (empty checklist + passed:true ⇒ pass) the gate fix removed.
_VERDICT = (
    'REVIEW_JSON_START {"passed": true, "items": '
    '[{"label": "ok", "passed": true, "evidence": "calc.py:1"}]} REVIEW_JSON_END'
)
_MAX_TURNS_ERROR = "Claude Code returned an error result: Reached maximum number of turns (10)"


async def test_reviewer_out_of_turns_escalates_instead_of_blaming_the_coder(tmp_path):
    """Attempt 13 of task 84251cb2: the reviewer exhausted its own turn budget
    while reading files and never emitted REVIEW_JSON. The fail-closed decision
    was fed back as a coder finding ("reviewer produced no parseable
    REVIEW_JSON") and spent the task's last bounded attempt.

    Reverting `_agent_review`'s retry+raise makes this return passed=False,
    i.e. a finding the coder is then told to fix."""
    from no_human.review.reviewer import AdversarialReviewer

    backend = _ReviewerBackend(
        _FakeAgentResult(_MAX_TURNS_ERROR, is_error=True, stop_reason="max_turns"),
        _FakeAgentResult(_MAX_TURNS_ERROR, is_error=True, stop_reason="max_turns"),
    )
    reviewer = AdversarialReviewer(backend=backend)

    with pytest.raises(ReviewerUnavailable, match="no verdict"):
        await reviewer._agent_review("prompt", tmp_path)

    assert len(backend.budgets) == 2, "one bounded infra retry (constraint #4)"
    assert backend.budgets[1] > backend.budgets[0], "the retry gets more turns"


async def test_a_reviewer_that_recovers_on_the_retry_returns_its_verdict(tmp_path):
    from no_human.review.reviewer import AdversarialReviewer

    backend = _ReviewerBackend(
        _FakeAgentResult(_MAX_TURNS_ERROR, is_error=True, stop_reason="max_turns"),
        _FakeAgentResult(_VERDICT),
    )
    decision = await AdversarialReviewer(backend=backend)._agent_review("p", tmp_path)
    assert decision.passed is True


async def test_review_timeout_halves_the_retry_window_on_a_hang(tmp_path, monkeypatch):
    """A hung/saturated reviewer (timeout, not turn exhaustion) must not be
    granted a second FULL window — a 50-line diff sat 20min in review (2×600s)
    in prod. The retry's window is halved; the task escalates ~5min sooner."""
    import asyncio
    import time as _time

    import no_human.review.reviewer as rv

    # Small floor so the shrink is observable in a fast test.
    monkeypatch.setattr(rv, "_REVIEW_MIN_RETRY_TIMEOUT", 0.05)

    windows: list[float] = []

    class _HangingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, on_event=None, **kw):
            start = _time.monotonic()
            try:
                await asyncio.sleep(5)  # never finishes inside the wait_for window
            except asyncio.CancelledError:
                windows.append(_time.monotonic() - start)
                raise
            return _FakeAgentResult(_VERDICT)

    with pytest.raises(ReviewerUnavailable, match="no verdict"):
        await rv.AdversarialReviewer(backend=_HangingBackend())._agent_review(
            "p", tmp_path, timeout=0.4
        )

    assert len(windows) == 2, "one bounded infra retry (constraint #4)"
    # Round 2's window was ~half of round 1's — not another full 0.4s.
    assert windows[1] < windows[0] * 0.75


async def test_a_real_failing_verdict_is_never_retried_or_swallowed(tmp_path):
    """A genuine FAIL is a finding, not an infra error: return it on round one."""
    from no_human.review.reviewer import AdversarialReviewer

    # "items", not "checklist" — with the wrong key this asserted False for the
    # wrong reason (empty checklist fails closed) instead of the failing finding.
    fail = (
        'REVIEW_JSON_START {"passed": false, "items": '
        '[{"label": "bug", "passed": false, "severity": "high", '
        '"evidence": "calc.py:3"}]} REVIEW_JSON_END'
    )
    backend = _ReviewerBackend(_FakeAgentResult(fail))
    decision = await AdversarialReviewer(backend=backend)._agent_review("p", tmp_path)
    assert decision.passed is False
    assert len(backend.budgets) == 1, "a real finding must not trigger an infra retry"


async def test_reviewer_turn_budget_outgrew_the_pre_D16_default():
    """The 10-turn cap predates the reviewer being able to read files."""
    from no_human.review import reviewer as r
    assert r._REVIEW_TURNS >= 30


async def test_run_review_does_not_convert_no_verdict_into_a_coder_finding(
    store, tmp_path, bare_repo
):
    """`_run_review`'s `except Exception` used to swallow ReviewerUnavailable and
    return a failing ReviewDecision — re-poisoning the coder's feedback."""
    from no_human.core.orchestrator import Orchestrator

    class _DeadReviewer:
        model = "claude-opus-5"
        _on_event = None
        async def review(self, *a, **kw):
            raise ReviewerUnavailable("reviewer reached no verdict")

    cfg = _config(tmp_path)
    orch = Orchestrator(
        store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None),
        reviewer=_DeadReviewer(),
    )
    task = Task.new("t", repo_path=str(bare_repo))
    await store.create_task(task)
    with pytest.raises(ReviewerUnavailable):
        await orch._run_review(task, orch._open_repo(task), "attempt-1")


def test_the_gate_prompt_tells_the_reviewer_the_pr_exists_0a():
    """0a / PR-021 — the gate ran BEFORE the PR existed.

    `_run_review` is called from `_run_attempt`; `open_pr` lived only in `_finalize`,
    which runs later. So a criterion of the form "the PR body contains X" was judged
    when no PR existed. Evidence (task abc7e570): three attempts, 4.89M tokens, ZERO
    PRs, each failing on "Required PR-body evidence still missing". The plan names it
    the root cause of PR-011 (10.33M, no PR) and says it REFRAMES PR-015 — a reliable
    judge applying an impossible rule, not an inconsistent judge.

    🔴 THE ORDERING WAS WRONG, NOT THE CRITERION. A bugfix with no demonstrated RED is
    exactly what this product exists to stop shipping, so the criterion stays and the
    artifact it names is created first.

    This asserts the PROMPT, because the plumbing being present proves nothing — a
    kwarg accepted and never interpolated is the defect that took five review rounds on
    a different branch. Both directions matter: present, and honestly absent.
    """
    from no_human.core.task import Task
    from no_human.review.reviewer import _build_review_prompt

    task = Task.new("add mul()", repo_path="/tmp/r")
    task.acceptance_criteria = ["the PR body contains a demonstrated RED"]

    with_pr = _build_review_prompt(
        task, "diff", "", "", draft_pr="https://github.com/o/r/pull/7")
    assert "pull/7" in with_pr, (
        "the draft PR url never reaches the prompt — the reviewer cannot judge a "
        "PR-body criterion against a PR it was never told about"
    )
    assert "judge it against" in with_pr, (
        "the prompt mentions the PR but does not instruct the reviewer to judge the "
        "criterion against it"
    )
    assert "cannot author" in with_pr, (
        "the prompt must say the body is template-generated; otherwise the reviewer "
        "faults the implementer for headings it could not have written"
    )

    # 🔴 THE ABSENCE TEXT IS CONDITIONAL NOW, and that is the point. My first version
    # emitted "the forge was unreachable when one was attempted" whenever there was no
    # url — false for every GitLab remote, every local bare repo (the entire bench corpus
    # and these fixtures), and _gate_already_satisfied, none of which attempt an open. A
    # false causal claim in the one component whose value is evidence-based judgement,
    # on the majority of runs — and it made the diff non-bench-neutral.
    attempted_and_failed = _build_review_prompt(
        task, "diff", "", "", draft_pr_absent="open failed")
    assert "artifact is absent" in attempted_and_failed, (
        "when an open was attempted and FAILED, the prompt must say the artifact is "
        "genuinely missing"
    )
    assert "do NOT ask the implementer to open a PR" in attempted_and_failed, (
        "the original failure mode was the reviewer instructing the coder to open a PR — "
        "something only the loop can do, which burned three attempts on abc7e570"
    )

    # No attempt made (GitLab, local bare repo, bench): SILENT. Byte-identical to main for
    # those runs, which is the only defensible default for a diff that must be
    # bench-neutral.
    not_attempted = _build_review_prompt(task, "diff", "", "")
    assert "unreachable" not in not_attempted, (
        "the prompt claims the forge was unreachable when no open was ever attempted"
    )
    assert "artifact is absent" not in not_attempted, (
        "the prompt excuses a PR-body criterion on a run where the PR is opened at "
        "delivery and the criterion IS satisfiable — the opposite of honest judgement"
    )


async def _async_noop(*_a, **_kw):
    """Minimal awaitable store double — see the note at each call site."""
    return None


async def test_the_draft_helper_actually_RETURNS_the_url_it_opened_0a(tmp_path,
                                                                      monkeypatch):
    """🔴 MY SECOND ATTEMPT AT THIS TEST WAS A TAUTOLOGY.

    The first was `src.index()` arithmetic, which a review defeated two ways. I replaced
    it with a "runtime" test that called a spy reviewer with a HARDCODED url and asserted
    the kwarg arrived — i.e. it asserted that Python passes arguments. It tested nothing
    about the orchestrator, and the mutation it was written to kill (the helper returning
    "" unconditionally) sailed straight through it.

    This one drives `_open_draft_pr_for_review` itself with `open_pr` stubbed, so the
    assertion is about OUR code: the helper must open a PR on a GitHub remote and return
    that url. If it returns "" the whole fix is inert and the reviewer is told no PR
    exists.
    """
    from types import SimpleNamespace

    import no_human.core.orchestrator as orch_mod
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.task import Task

    opened: list[tuple[str, str]] = []

    def fake_open_pr(repo, branch, title, body, **kw):
        opened.append((branch, body))
        return SimpleNamespace(url="https://github.com/o/r/pull/42")

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)

    orch = Orchestrator.__new__(Orchestrator)
    orch._sink = lambda e: None
    orch._active_attempt_id = None
    orch.config = {"git": {"github_hosts": [], "pr_labels": []}}
    # The helper now stakes a DURABLE claim ("this run created the draft, so this run
    # may rewrite its body") in task.context, because an in-process attribute could not
    # survive a park/resume — a review drove that defect. A store is therefore a real
    # collaborator here, not stub scaffolding.
    orch.store = SimpleNamespace(update_task=_async_noop)

    repo = SimpleNamespace(
        remote_url=lambda: "https://github.com/o/r.git", path=tmp_path)
    task = Task.new("add mul()", repo_path=str(tmp_path))
    task.acceptance_criteria = ["the PR body contains a demonstrated RED"]
    result = SimpleNamespace(final_text="did the thing", num_turns=3)
    commit = SimpleNamespace(files_changed=1, insertions=2, deletions=0, sha="abc1234")

    url = await orch._open_draft_pr_for_review(
        task, repo, "nh/task-1", "main", "att-1", commit=commit, result=result)

    assert url == "https://github.com/o/r/pull/42", (
        f"the helper opened a PR but returned {url!r} — the reviewer is then told no PR "
        f"exists and a PR-body criterion fails for a reason the coder cannot act on, "
        f"which is the exact defect 0a fixes"
    )
    assert opened and opened[0][0] == "nh/task-1", "no PR was opened at all"
    # And the body it opened with must be the real one, not a placeholder: the reviewer
    # judges this text.
    assert "add mul()" in opened[0][1] or "demonstrated RED" in opened[0][1], (
        f"the PR body the gate will judge carries neither the task nor its criteria: "
        f"{opened[0][1][:200]!r}"
    )


async def test_a_non_github_remote_gets_no_pre_gate_pr_0a(tmp_path, monkeypatch):
    """CRITICAL-1: on GitLab this must not open anything.

    `gitlab.open_mr` has no already-exists branch and passes no `--draft`, so a pre-gate
    open there made `_finalize`'s open raise twice and ESCALATED a task that had PASSED
    review — driven and confirmed by review (AWAITING_APPROVAL on main -> ESCALATED).
    """
    from types import SimpleNamespace

    import no_human.core.orchestrator as orch_mod
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.task import Task

    calls: list = []
    monkeypatch.setattr(orch_mod, "open_pr",
                        lambda *a, **k: calls.append(1) or SimpleNamespace(url="x"))

    orch = Orchestrator.__new__(Orchestrator)
    orch._sink = lambda e: None
    orch._active_attempt_id = None
    orch.config = {"git": {"github_hosts": [], "pr_labels": []}}
    # The helper now stakes a DURABLE claim ("this run created the draft, so this run
    # may rewrite its body") in task.context, because an in-process attribute could not
    # survive a park/resume — a review drove that defect. A store is therefore a real
    # collaborator here, not stub scaffolding.
    orch.store = SimpleNamespace(update_task=_async_noop)

    repo = SimpleNamespace(
        remote_url=lambda: "https://gitlab.com/o/r.git", path=tmp_path)
    task = Task.new("x", repo_path=str(tmp_path))

    url = await orch._open_draft_pr_for_review(
        task, repo, "nh/task-1", "main", "att-1",
        commit=SimpleNamespace(files_changed=0, insertions=0, deletions=0, sha="a"),
        result=SimpleNamespace(final_text="t", num_turns=1))

    assert url == "" and not calls, (
        "a pre-gate PR was opened against a non-GitHub remote. GitLab is neither "
        "draft-by-default nor idempotent, so _finalize's open then fails twice and a "
        "PASSING task escalates."
    )


def test_the_draft_pr_is_opened_before_the_gate_not_after_0a():
    """Ordering lint. Explicitly a BACKSTOP, not the proof.

    Kept only because it catches a whole-hog reordering cheaply. A review demonstrated it
    cannot see a url dropped at the second seam, nor a helper that returns "" always —
    tests/test_e2e_orchestrator.py::test_the_draft_pr_url_REACHES_the_reviewer_end_to_end_0a
    is what covers those. (This line named a test that never existed for two commits; a
    review caught it twice. Both names in this docstring are real as of this commit.)
    """
    import inspect

    import no_human.core.orchestrator as _mod

    src = inspect.getsource(_mod)
    draft = src.index("_open_draft_pr_for_review(")
    review = src.index("decision = await self._run_review(")
    assert draft < review, (
        "the draft PR is opened AFTER the review gate runs — which is the original "
        "defect: the criterion refers to an artifact that does not exist yet"
    )
    # 🔴 GITHUB-ONLY GUARD. Without it, GitLab escalates a task that PASSED review:
    # gitlab.open_mr has no already-exists branch, so _finalize's open raises twice.
    assert "is_github_remote" in src, (
        "the pre-gate draft open is not gated to GitHub. gitlab.open_mr is neither "
        "draft-by-default nor idempotent, and a duplicate MR turns a passing task into "
        "an escalation — driven and confirmed by review."
    )


# The `_run_review` -> `reviewer.review` seam is covered, but NOT here — it needs the
# driven harness, so it lives in
#   tests/test_e2e_orchestrator.py::test_the_draft_pr_url_REACHES_the_reviewer_end_to_end_0a
# It drives the real run_task with a stubbed FORGE (orch_mod.open_pr) and a spy reviewer,
# and asserts both 0a properties: open_pr happens BEFORE review, and the url the forge
# returned is the url the gate received. Verified to kill two mutants that this file's
# tests do not see: dropping `draft_pr=` at the seam, and removing the helper call
# entirely. Two earlier attempts at the same seam were worthless and are recorded so
# they are not repeated — one passed a hardcoded url to a spy and asserted that Python
# passes arguments; the other stubbed Orchestrator and grew an attribute per run
# (_usable_profile, _test_cache, ...) until it was a mock testing the mock.


def test_the_already_exists_path_refreshes_the_body_only_when_asked_0a(tmp_path,
                                                                      monkeypatch):
    """The C-2 fix had ZERO coverage — deleting it passed the whole suite.

    And unconditional editing would have OVERWRITTEN a PR description a human edited, on
    the revision flow (a task resuming onto an existing PR branch). So the update is
    opt-in and only `_finalize` asks for it, after this run opened the draft itself.
    """
    import subprocess as _sp

    from no_human.vcs import github as gh

    argv: list[list[str]] = []

    class Done:
        returncode = 1
        stdout = ""
        stderr = "a pull request for branch already exists"

    class Ok:
        returncode = 0
        stdout = "https://github.com/o/r/pull/9"
        stderr = ""

    def fake_run(cmd, **kw):
        argv.append(cmd)
        if cmd[:3] == ["gh", "pr", "create"]:
            return Done()
        if cmd[:3] == ["gh", "pr", "list"]:
            return Ok()
        if cmd[:3] == ["gh", "pr", "edit"]:
            return Ok()
        return Ok()

    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr(gh.subprocess, "run", fake_run)

    # Default: NO body rewrite — a human's edits survive.
    argv.clear()
    gh.open_pr(tmp_path, "br", "t", "new body")
    assert not any(c[:3] == ["gh", "pr", "edit"] for c in argv), (
        "the body was rewritten without being asked — this path is also the revision "
        "flow, where it would discard a description a human edited"
    )

    # Opt-in: the run that opened the draft refreshes it with the evidence-bearing body.
    argv.clear()
    gh.open_pr(tmp_path, "br", "t", "new body", update_existing_body=True)
    edits = [c for c in argv if c[:3] == ["gh", "pr", "edit"]]
    assert edits, "update_existing_body=True did not refresh the body"
    assert edits[0][-1] == "new body" and "--body" in edits[0], (
        f"the edit did not carry the new body: {edits[0]!r}"
    )
