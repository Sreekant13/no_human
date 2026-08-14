"""Retry-cost class (refile of 4413c7c5): attempt N>1 starts from a distilled
state doc instead of re-accumulating the repo map + gathered-context digest
every attempt (80% of spend delivered 37% of the work; retries re-read the
whole repo+history each attempt).

Covers all three acceptance criteria:
  - attempt-2 context tokens measurably below the re-accumulation baseline
  - the distilled doc carries tried/failed/diff/findings/criteria sections
  - distillation failure is loud (ERROR log + event) and falls back, never
    silently degrades the attempt
"""

import logging
import subprocess

from unittest.mock import patch as _patch

from no_human.agent.claude_backend import AgentResult
from no_human.core.orchestrator import Orchestrator
from no_human.core.prompt_blocks import (
    DISTILLED_STATE_CAP,
    build_distilled_state,
)
from no_human.core.task import Task, TaskStatus
from no_human.vcs.git import GitRepo


# --------------------------------------------------------------- fixtures --

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo_with_diff(tmp_path, *, big=False):
    """A real git repo: an initial commit on ``main``, then a second commit
    on a task branch that is the "attempt's" diff. ``repo.diff('main')``
    from this state is the diff the orchestrator would see mid-attempt."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "base.py").write_text("def base():\n    return 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "checkout", "-b", "task-branch")
    body = "x" * (7000 if big else 20)
    (work / "feature.py").write_text(f"def feature():\n    return '{body}'\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "feature")
    return GitRepo(work)


class _FakeStore:
    def __init__(self):
        self.updates = []

    async def update_task(self, task):
        self.updates.append(task)


def _orch_min(store=None):
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch._sink = lambda e: None
    orch.store = store
    return orch


def _orch_for_prompt():
    """Minimal orchestrator for `_build_implement_prompt` — same idiom as
    tests/test_context_diet.py's `_orch()`. No `_sink` set on purpose: the
    seam must not require an event sink to construct a prompt."""
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch.ci_runner = None
    orch._active_profile = None
    orch._active_memories = None
    return orch


class _DiffBackend:
    """Stands in for ClaudeBackend's readonly diff-compression call."""

    def __init__(self, text=None, raise_exc=None):
        self.text = text
        self.raise_exc = raise_exc
        self.calls = []

    async def run(self, prompt, **kw):
        self.calls.append((prompt, kw))
        if self.raise_exc:
            raise self.raise_exc
        return AgentResult(final_text=self.text, num_turns=1, is_error=False,
                            tokens_used=42, session_id="s", stop_reason="end_turn")


# --------------------------------------------- AC1: attempt-2 token diet --

def test_attempt2_context_tokens_under_30pct_of_reaccumulation_baseline(monkeypatch):
    import no_human.context.repo_map as rm
    monkeypatch.setattr(rm, "repo_map", lambda p: "MAP-" + ("x" * 12_000))

    t = Task.new("fix the widget", repo_path="/tmp/repo")
    t.status = TaskStatus.IMPLEMENTING
    t.acceptance_criteria = ["widget renders", "no crash on empty input"]
    # A plan long enough that only its head inlines (> _PLAN_INLINE_MAX) —
    # the repo map still fires (test_repo_map_kept_when_plan_is_truncated),
    # and the plan block itself is unaffected by distillation either way, so
    # it is present on BOTH attempts and proves the reduction is not "drop
    # the contract".
    plan = "## OBJECTIVE\nfix the widget\n" + ("filler step\n" * 800)
    t.context = {
        "plan": plan,
        "gathered": {"chunks": [
            {"source": "grep", "title": f"widget.py hit {i}"} for i in range(8)
        ]},
    }

    orch = _orch_for_prompt()
    attempt1_prompt = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=1)
    baseline = orch._last_context_breakdown["reaccumulated_tokens"]
    assert baseline > 0
    assert "MAP-" in attempt1_prompt
    assert "Gathered context" in attempt1_prompt

    distilled_doc = build_distilled_state(
        t, diff_text="diff --git a/widget.py b/widget.py\n+def render(): pass\n",
        changed_files=["widget.py"], last_detail="attempt 1: render() missing",
    )
    t.context = {
        **t.context, "distilled_state": distilled_doc, "distilled_state_attempt": 2,
    }
    attempt2_prompt = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=2)
    measured = orch._last_context_breakdown["reaccumulated_tokens"]

    assert measured / baseline < 0.30, (measured, baseline)
    # the re-accumulated bytes are gone from attempt 2 ...
    assert "MAP-" not in attempt2_prompt
    assert "Gathered context" not in attempt2_prompt
    # ... but the contract is not: rules, plan and criteria all still there.
    assert "IMPLEMENTATION PLAN" in attempt2_prompt
    assert "widget renders" in attempt2_prompt
    assert "no crash on empty input" in attempt2_prompt
    assert "DISTILLED STATE FROM YOUR PREVIOUS ATTEMPT" in attempt2_prompt


def test_distilled_doc_respects_size_cap():
    t = Task.new("fix x", repo_path="/tmp/r")
    t.acceptance_criteria = [f"criterion {i} must hold end to end" for i in range(5)]
    t.context = {
        "attempt_log": ["attempt 1: tests failed: 1 failed, 0 passed"],
        "review_feedback": [
            {"label": f"finding-{i}", "file": "a.py", "line": i,
             "comment": "x" * 450}
            for i in range(20)
        ],
    }
    diff = "diff --git a/foo.py b/foo.py\n+" + ("y" * 40_000)
    doc = build_distilled_state(
        t, diff_text=diff, changed_files=["foo.py"], last_detail="attempt 1: fail"
    )
    assert len(doc) <= DISTILLED_STATE_CAP
    assert "more chars truncated" in doc or "truncated to fit" in doc


# --------------------------------------- AC2: five sections, content round-trip

def test_distilled_state_carries_four_sections():
    t = Task.new("fix x", repo_path="/tmp/r")
    t.acceptance_criteria = ["the widget parses input"]
    t.context = {
        "attempt_log": ["attempt 1: TRIED-MARKER a naive parse"],
        "review_feedback": [
            {"label": "bug", "file": "widget.py", "line": 12,
             "comment": "VERBATIM-FINDING-MARKER the widget parses input is still broken"},
        ],
    }
    diff = "diff --git a/widget.py b/widget.py\n+def parse(): return CHANGED_LINE_MARKER\n"
    doc = build_distilled_state(
        t, diff_text=diff, changed_files=["widget.py"],
        last_detail="FAILURE-DETAIL-MARKER: parse() raised",
    )
    for heading in (
        "## What was tried", "## What failed and why", "## Diff so far",
        "## Review findings (verbatim)", "## Remaining acceptance criteria",
    ):
        assert heading in doc, heading

    assert "TRIED-MARKER" in doc
    assert "FAILURE-DETAIL-MARKER" in doc
    assert "widget.py" in doc
    assert "+def parse(): return CHANGED_LINE_MARKER" in doc
    # the finding's comment survives BYTE-IDENTICAL, not paraphrased.
    assert "VERBATIM-FINDING-MARKER the widget parses input is still broken" in doc
    for c in t.acceptance_criteria:
        assert c in doc


def test_remaining_criteria_never_claims_met():
    t = Task.new("fix x", repo_path="/tmp/r")
    t.acceptance_criteria = ["untouched criterion has no evidence either way",
                              "the parser must not crash on empty input"]
    t.context = {
        "review_feedback": [
            {"label": "bug", "file": "p.py", "line": 1,
             "comment": "the parser must not crash on empty input — it does"},
        ],
    }
    doc = build_distilled_state(
        t, diff_text="diff --git a/p.py b/p.py\n+pass\n",
        changed_files=["p.py"], last_detail="",
    )
    assert "untouched criterion has no evidence either way — [status unknown]" in doc
    assert "the parser must not crash on empty input — [NOT MET]" in doc
    assert "[MET]" not in doc
    assert " — MET" not in doc


# ------------------------------------------------- AC3: loud failure, never silent

async def test_distillation_failure_is_loud_and_falls_back(tmp_path, caplog, monkeypatch):
    import no_human.context.repo_map as rm
    monkeypatch.setattr(rm, "repo_map", lambda p: "MAP-SENTINEL")

    orch = _orch_min(_FakeStore())
    events = []
    orch._sink = events.append
    repo = _repo_with_diff(tmp_path, big=True)
    t = Task.new("fix x", repo_path=str(repo.path))
    t.status = TaskStatus.IMPLEMENTING
    t.acceptance_criteria = ["thing works"]
    t.context = {
        "attempt_log": ["attempt 1: failed"],
        "gathered": {"chunks": [{"source": "grep", "title": "hit"}]},
    }

    boom = _DiffBackend(raise_exc=RuntimeError("utility backend unavailable"))
    with caplog.at_level(logging.ERROR, logger="no_human.orchestrator"):
        with _patch("no_human.core.orchestrator.advisory_backend", return_value=boom):
            # must not raise — the caller (_run_attempt) is unaffected.
            await orch._distill_attempt_state(t, repo, 2, "main")

    failed = [e for e in events if e.get("kind") == "attempt_distill_failed"]
    assert failed, events
    assert failed[0].get("error") == "RuntimeError"
    assert "falling back to full context re-read" in caplog.text

    assert "distilled_state" not in (t.context or {})

    # the seam falls back to exactly the pre-change shape: map + digest back.
    orch2 = _orch_for_prompt()
    prompt = orch2._build_implement_prompt(t, str(repo.path))
    assert "MAP-SENTINEL" in prompt
    assert "Gathered context" in prompt


async def test_empty_distillation_result_is_treated_as_failure(tmp_path, caplog):
    orch = _orch_min(_FakeStore())
    events = []
    orch._sink = events.append
    repo = _repo_with_diff(tmp_path, big=True)
    t = Task.new("fix x", repo_path=str(repo.path))
    t.context = {"attempt_log": ["attempt 1: failed"]}

    empty = _DiffBackend(text="")
    with caplog.at_level(logging.ERROR, logger="no_human.orchestrator"):
        with _patch("no_human.core.orchestrator.advisory_backend", return_value=empty):
            await orch._distill_attempt_state(t, repo, 2, "main")

    failed = [e for e in events if e.get("kind") == "attempt_distill_failed"]
    assert failed, events
    assert "falling back to full context re-read" in caplog.text
    assert "distilled_state" not in (t.context or {})


async def test_attempt_one_is_byte_identical(tmp_path):
    orch = _orch_min(_FakeStore())
    events = []
    orch._sink = events.append
    repo = _repo_with_diff(tmp_path)
    t = Task.new("fix x", repo_path=str(repo.path))
    t.context = {}

    await orch._distill_attempt_state(t, repo, 1, "main")

    assert events == []
    assert "distilled_state" not in (t.context or {})
    assert orch.store.updates == []


async def test_disabled_by_config_emits_skipped(tmp_path):
    orch = _orch_min(_FakeStore())
    orch.config = {"context": {"attempt_state_distill_enabled": False}}
    events = []
    orch._sink = events.append
    repo = _repo_with_diff(tmp_path)
    t = Task.new("fix x", repo_path=str(repo.path))
    t.context = {}

    await orch._distill_attempt_state(t, repo, 2, "main")

    skipped = [e for e in events if e.get("kind") == "attempt_distill_skipped"]
    assert skipped and skipped[0].get("reason") == "disabled"
    assert "distilled_state" not in (t.context or {})
    assert orch.store.updates == []


async def test_utility_tier_is_used_for_diff_compression(tmp_path):
    orch = _orch_min(_FakeStore())
    events = []
    orch._sink = events.append
    repo = _repo_with_diff(tmp_path, big=True)
    t = Task.new("fix x", repo_path=str(repo.path))
    t.context = {"attempt_log": ["attempt 1: failed"]}

    fake = _DiffBackend(text="feature.py: added feature() returning a constant")
    with _patch("no_human.core.orchestrator.advisory_backend",
                return_value=fake) as mocked_seam:
        await orch._distill_attempt_state(t, repo, 2, "main")

    assert mocked_seam.call_args.args[0] == orch._utility_model()
    assert mocked_seam.call_args.kwargs.get("role") == "distill"
    # spend landed in distill_*, not utility_*/plan_/supervisor_.
    assert getattr(orch, "_distill_usage", None) is not None
    assert orch._distill_usage["tokens_used"] == 42
    assert getattr(orch, "_utility_usage", None) is None

    fired = [e for e in events if e.get("kind") == "attempt_distill"]
    assert fired and fired[0].get("diff_compressed") is True
    assert "distilled_state" in (t.context or {})


# --- regression: stale distilled_state must not reach a resumed attempt 1 --

def _stale_context():
    """Exactly what a prior run leaves behind on ``task.context`` when it
    wrote a distilled-state doc for its own attempt 2."""
    return {
        "distilled_state": "STALE-DOC-MARKER from a prior run's attempt 2",
        "distilled_state_attempt": 2,
        "gathered": {"chunks": [{"source": "grep", "title": "widget.py hit"}]},
    }


async def test_resumed_attempt_1_clears_a_prior_runs_distilled_state(tmp_path):
    """AC1, red-first: a resumed attempt 1 (nh reply/requeue re-enters the
    loop at attempt_n==1) must not let a prior run's doc survive in
    task.context — it must be actively cleared and the clear persisted."""
    orch = _orch_min(_FakeStore())
    events = []
    orch._sink = events.append
    repo = _repo_with_diff(tmp_path)
    t = Task.new("fix x", repo_path=str(repo.path))
    t.context = _stale_context()

    await orch._distill_attempt_state(t, repo, 1, "main")

    assert "distilled_state" not in (t.context or {})
    assert "distilled_state_attempt" not in (t.context or {})
    assert orch.store.updates, "the clear must be persisted, not just in-memory"
    skipped = [e for e in events if e.get("kind") == "attempt_distill_skipped"]
    assert len(skipped) == 1, events
    assert skipped[0].get("reason") == "stale_cleared"


def test_resumed_attempt_1_prompt_reaccumulates_map_and_digest(monkeypatch):
    """AC1 at the consumption layer — the layer the round-2 review found
    ``test_attempt_one_is_byte_identical`` never checked. Even if a stale
    doc is still sitting in task.context (e.g. the seam is reached before
    ``_distill_attempt_state`` runs, or a future caller skips it), the
    prompt-building seam itself must refuse to consume a doc that isn't
    tagged for the attempt it was asked to build."""
    import no_human.context.repo_map as rm
    monkeypatch.setattr(rm, "repo_map", lambda p: "MAP-SENTINEL")

    t = Task.new("fix x", repo_path="/tmp/repo")
    t.acceptance_criteria = ["widget renders"]
    t.context = _stale_context()

    orch = _orch_for_prompt()
    prompt = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=1)

    assert "MAP-SENTINEL" in prompt
    assert "Gathered context" in prompt
    assert "STALE-DOC-MARKER" not in prompt
    assert "DISTILLED STATE FROM YOUR PREVIOUS ATTEMPT" not in prompt


def test_seam_ignores_a_doc_from_a_different_attempt():
    """Layer 2 alone, fail-closed: a doc tagged for one attempt must never
    be consumed while building the prompt for a different attempt number,
    and an unknown (None) attempt number must never trust a doc either."""
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.acceptance_criteria = ["widget renders"]
    t.context = {
        "distilled_state": "STALE-DOC-MARKER for attempt 2",
        "distilled_state_attempt": 2,
    }
    orch = _orch_for_prompt()

    mismatched = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=3)
    assert "STALE-DOC-MARKER" not in mismatched
    assert "DISTILLED STATE FROM YOUR PREVIOUS ATTEMPT" not in mismatched

    unknown = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=None)
    assert "STALE-DOC-MARKER" not in unknown
    assert "DISTILLED STATE FROM YOUR PREVIOUS ATTEMPT" not in unknown


async def test_kill_switch_off_drops_a_stale_doc(tmp_path):
    """The config kill switch is a second door: even if it is off, a stale
    doc left over from a run where it was on must not survive."""
    orch = _orch_min(_FakeStore())
    orch.config = {"context": {"attempt_state_distill_enabled": False}}
    events = []
    orch._sink = events.append
    repo = _repo_with_diff(tmp_path)
    t = Task.new("fix x", repo_path=str(repo.path))
    t.context = _stale_context()

    await orch._distill_attempt_state(t, repo, 2, "main")

    assert "distilled_state" not in (t.context or {})
    assert "distilled_state_attempt" not in (t.context or {})
    assert orch.store.updates, "the clear must be persisted"
    skipped = [e for e in events if e.get("kind") == "attempt_distill_skipped"]
    assert skipped and skipped[0].get("reason") == "disabled"


async def test_attempt_2_end_to_end_writes_then_consumes_the_doc(tmp_path, monkeypatch):
    """Write (_distill_attempt_state) and consume (_build_implement_prompt)
    agree on the lineage key — the mechanism this fix must keep intact for
    N>1 (AC2), proven end to end rather than by constructing context by
    hand as the other tests here do."""
    orch = _orch_min(_FakeStore())
    events = []
    orch._sink = events.append
    orch.ci_runner = None
    orch._active_profile = None
    orch._active_memories = None
    repo = _repo_with_diff(tmp_path)
    t = Task.new("fix x", repo_path=str(repo.path))
    t.acceptance_criteria = ["widget renders"]
    t.context = {"attempt_log": ["attempt 1: failed"]}

    await orch._distill_attempt_state(t, repo, 2, "main")

    assert t.context.get("distilled_state_attempt") == 2
    fired = [e for e in events if e.get("kind") == "attempt_distill"]
    assert fired, events

    import no_human.context.repo_map as rm
    monkeypatch.setattr(rm, "repo_map", lambda p: "MAP-SENTINEL")
    prompt = orch._build_implement_prompt(t, str(repo.path), attempt_n=2)

    assert "DISTILLED STATE FROM YOUR PREVIOUS ATTEMPT" in prompt
    assert "MAP-SENTINEL" not in prompt
    assert "Gathered context" not in prompt
