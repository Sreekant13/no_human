"""North-star benchmark runner (plan Task A3): replay a real historical task
through the REAL Orchestrator in a push-proof sandbox and score it against the
original session's economics.

Safety invariant (the one that matters): the sandbox clone's ``origin`` is
re-pointed at a workdir-local bare BEFORE orchestration, and a hard guard
asserts it — the agent's branch pushes can never reach the operator's real
repo. A post-run check additionally asserts the source repo's refs are
untouched.

Cheating invariant: the GoalJudge sees ONLY the spec's request/criteria and
the agent's diff/outcome — never the source transcript or the original
solution.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..core.db import Store
from ..core.orchestrator import Orchestrator
from ..core.task import Task, TaskStatus
from ..notify.slack import SlackNotifier
from .bench_task import BenchTask

BackendFactory = Callable[[BenchTask], Any]

# Off-ramps that count as "reached the human gate" for gate-kind outcomes vs
# honest-escalation outcomes (expect_escalation specs).
_GATE_STATES = {TaskStatus.AWAITING_APPROVAL}
_HONEST_STOPS = {TaskStatus.ESCALATED, TaskStatus.AWAITING_INPUT,
                 TaskStatus.BLOCKED}


@dataclass
class BenchScore:
    task_id: str
    title: str
    outcome_status: str
    goal_satisfied: bool | None      # GoalJudge verdict; None = judge skipped
    escalated_honestly: bool
    mergeable: bool | None           # holdout tests, when the spec has them
    nh_tokens: int                   # non-cache in/out, coder + reviewer
    nh_cache_tokens: int             # cache-read, coder + reviewer
    nh_cache_creation_tokens: int
    nh_turns: int
    nh_wall_clock_s: float
    orig_tokens: int                 # non-cache: input+output
    orig_cache_tokens: int
    orig_cache_creation_tokens: int
    orig_wall_clock_s: float
    orig_corrections: int
    expected_escalation: bool = False   # spec said: correct = honest stop
    subset: str = "full"                # "core" = hand-curated, PR-reviewed
    notes: str = ""

    @property
    def token_ratio(self) -> float | None:
        """nh non-cache burn / original non-cache burn. <1.0 = no_human was
        cheaper than the babysat session. None when the original is unknown.

        nh_tokens INCLUDES the reviewer (B1 angle-4 finding: coder-only
        summation rigged the ratio in no_human's favor). Planner/supervisor
        burn is not yet persisted to any column (tracked as a B2 fix) — until
        it lands, this ratio still under-counts no_human on complex-tier
        tasks; the report labels it accordingly."""
        if self.orig_tokens <= 0:
            return None
        return self.nh_tokens / self.orig_tokens

    @property
    def cost_ratio(self) -> float | None:
        """Price-weighted ratio using Anthropic's cache multipliers
        (fresh=1.0, cache_read=0.1, cache_creation=1.25) applied SYMMETRICALLY
        to both sides — tracks dollar cost, where cache-read is ~95% of real
        burn and the plain token_ratio is blind to it."""
        orig = (self.orig_tokens
                + 0.1 * self.orig_cache_tokens
                + 1.25 * self.orig_cache_creation_tokens)
        if orig <= 0:
            return None
        nh = (self.nh_tokens
              + 0.1 * self.nh_cache_tokens
              + 1.25 * self.nh_cache_creation_tokens)
        return nh / orig

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "title": self.title,
            "outcome_status": self.outcome_status,
            "goal_satisfied": self.goal_satisfied,
            "escalated_honestly": self.escalated_honestly,
            "mergeable": self.mergeable,
            "nh_tokens": self.nh_tokens,
            "nh_cache_tokens": self.nh_cache_tokens,
            "nh_cache_creation_tokens": self.nh_cache_creation_tokens,
            "nh_turns": self.nh_turns,
            "nh_wall_clock_s": round(self.nh_wall_clock_s, 2),
            "orig_tokens": self.orig_tokens,
            "orig_cache_tokens": self.orig_cache_tokens,
            "orig_cache_creation_tokens": self.orig_cache_creation_tokens,
            "orig_wall_clock_s": self.orig_wall_clock_s,
            "orig_corrections": self.orig_corrections,
            "expected_escalation": self.expected_escalation,
            "subset": self.subset,
            "token_ratio": (round(self.token_ratio, 3)
                            if self.token_ratio is not None else None),
            "cost_ratio": (round(self.cost_ratio, 3)
                           if self.cost_ratio is not None else None),
            "notes": self.notes,
        }


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


def _setup_sandbox(spec: BenchTask, workdir: Path) -> Path:
    """Clone the real repo at the spec's pin; re-point origin to a local bare.

    The clone uses --no-hardlinks so the sandbox shares nothing mutable with
    the source repo's object store."""
    src = Path(spec.repo.get("path", ""))
    work = workdir / "work"
    subprocess.run(["git", "clone", "--no-hardlinks", str(src), str(work)],
                   check=True, capture_output=True)
    pin = spec.repo.get("pin") or "HEAD"
    if pin != "HEAD":
        _git(work, "checkout", "--detach", pin)
        # The coder needs a branch to work from.
        _git(work, "checkout", "-b", "bench-base")
    _git(work, "config", "user.email", "bench@no_human")
    _git(work, "config", "user.name", "nh-bench")

    bare = workdir / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)],
                   check=True, capture_output=True)
    _git(work, "remote", "set-url", "origin", str(bare))

    # HARD GUARD — BEFORE any push (review finding: guard-after-push detects
    # an escape only after the write has landed in the real repo).
    origin = Path(_git(work, "remote", "get-url", "origin")).resolve()
    if not str(origin).startswith(str(workdir.resolve())):
        raise RuntimeError(
            f"push-proofing failed: origin {origin} escapes sandbox {workdir}")

    _git(work, "push", "origin", "HEAD")
    return work


def _ref_signature(repo: Path) -> str:
    """Cheap tamper check for the SOURCE repo: all refs + their targets."""
    try:
        return subprocess.run(["git", "for-each-ref"], cwd=repo,
                              capture_output=True, text=True).stdout
    except OSError:
        return ""


class NorthStarRunner:
    """Mirrors eval.replay.ReplayRunner but for real-repo bench specs."""

    def __init__(self, config: dict, *, backend_factory: BackendFactory,
                 reviewer: Any | None = None, goal_judge: Any | None = None,
                 event_sink: Callable[[dict], None] | None = None):
        self.config = config
        self.backend_factory = backend_factory
        self.reviewer = reviewer
        self.goal_judge = goal_judge
        self._event_sink = event_sink

    async def run_one(self, spec: BenchTask, *, workdir: Path) -> BenchScore:
        if not spec.runnable:
            return self._skipped(spec)

        src_repo = Path(spec.repo.get("path", ""))
        refs_before = _ref_signature(src_repo)
        work = _setup_sandbox(spec, workdir)
        base_sha = _git(work, "rev-parse", "HEAD")

        store = await Store(workdir / "bench.db").connect()
        try:
            orch = Orchestrator(
                store, self.config, self.backend_factory(spec),
                SlackNotifier(None), reviewer=self.reviewer,
                event_sink=self._event_sink,
            )
            task = Task.new(spec.title, repo_path=str(work),
                            description=spec.request)
            task.acceptance_criteria = list(spec.acceptance_criteria)
            await store.create_task(task)

            t0 = time.monotonic()
            outcome = await orch.run_task(task)
            elapsed = time.monotonic() - t0

            attempts = await store.list_attempts(task.id)
        finally:
            await store.close()

        if _ref_signature(src_repo) != refs_before:
            raise RuntimeError(
                f"SOURCE REPO REFS CHANGED during bench run of {spec.id} — "
                "push-proofing failed; halt the bench")

        return await self._score(spec, outcome, work, base_sha,
                                 attempts, elapsed)

    def _skipped(self, spec: BenchTask) -> BenchScore:
        orig = spec.original or {}
        toks = orig.get("tokens", {}) or {}
        return BenchScore(
            task_id=spec.id, title=spec.title, outcome_status="skipped",
            goal_satisfied=None, escalated_honestly=False, mergeable=None,
            nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
            nh_turns=0, nh_wall_clock_s=0.0,
            orig_tokens=int(toks.get("input_tokens", 0)) + int(toks.get("output_tokens", 0)),
            orig_cache_tokens=int(toks.get("cache_read_input_tokens", 0)),
            orig_cache_creation_tokens=int(
                toks.get("cache_creation_input_tokens", 0)),
            orig_wall_clock_s=float(orig.get("wall_clock_s", 0.0)),
            orig_corrections=int(orig.get("corrections", 0)),
            expected_escalation=spec.expect_escalation,
            subset=spec.subset,
            notes=f"skipped: {spec.skip_reason}",
        )

    async def _score(self, spec: BenchTask, outcome, work: Path,
                     base_sha: str, attempts: list[dict],
                     elapsed: float) -> BenchScore:
        status = outcome.status
        # Coder + reviewer buckets (angle-4 finding: coder-only summation
        # rigged the north-star ratio; planner/supervisor columns pending B2).
        nh_tokens = sum(int(a.get("tokens_used") or 0)
                        + int(a.get("review_tokens_used") or 0)
                        for a in attempts)
        nh_cache = sum(int(a.get("cache_read_tokens") or 0)
                       + int(a.get("review_cache_read_tokens") or 0)
                       for a in attempts)
        nh_creation = sum(int(a.get("cache_creation_tokens") or 0)
                          + int(a.get("review_cache_creation_tokens") or 0)
                          for a in attempts)
        turns = sum(int(a.get("turns_used") or 0) for a in attempts)
        orig = spec.original or {}
        toks = orig.get("tokens", {}) or {}

        score = BenchScore(
            task_id=spec.id, title=spec.title, outcome_status=status.value,
            goal_satisfied=None,
            escalated_honestly=status in _HONEST_STOPS,
            mergeable=None,
            nh_tokens=nh_tokens, nh_cache_tokens=nh_cache,
            nh_cache_creation_tokens=nh_creation, nh_turns=turns,
            nh_wall_clock_s=elapsed,
            orig_tokens=int(toks.get("input_tokens", 0)) + int(toks.get("output_tokens", 0)),
            orig_cache_tokens=int(toks.get("cache_read_input_tokens", 0)),
            orig_cache_creation_tokens=int(
                toks.get("cache_creation_input_tokens", 0)),
            orig_wall_clock_s=float(orig.get("wall_clock_s", 0.0)),
            orig_corrections=int(orig.get("corrections", 0)),
            expected_escalation=spec.expect_escalation,
            subset=spec.subset,
        )

        if spec.expect_escalation:
            # Credential-gated task: CORRECT = honest stop, never a faked PR.
            score.goal_satisfied = score.escalated_honestly
            score.notes = ("honestly escalated as expected" if score.goal_satisfied
                           else f"expected escalation, got {status.value}")
            return score

        if status not in _GATE_STATES:
            score.goal_satisfied = False
            score.notes = f"did not reach the human gate ({status.value})"
            return score

        score.mergeable = self._holdout_ok(spec, work)
        if self.goal_judge is not None:
            agent_diff = subprocess.run(
                ["git", "diff", base_sha, "HEAD"], cwd=work,
                capture_output=True, text=True).stdout
            verdict = await self.goal_judge.judge(
                request=spec.request, criteria=spec.acceptance_criteria,
                agent_diff=agent_diff, outcome_status=status.value,
                repo_path=str(work))
            score.goal_satisfied = bool(verdict.satisfied) and \
                score.mergeable in (True, None)
            score.notes = verdict.evidence[:400]
        else:
            score.goal_satisfied = score.mergeable in (True, None)
            score.notes = "no judge injected; holdout-only scoring"
        return score

    def _holdout_ok(self, spec: BenchTask, work: Path) -> bool | None:
        if not spec.holdout:
            return None
        # Same bounded held-out mechanics as replay._mergeable.
        import os
        import signal as _signal
        import sys
        held = work / "tests" / "bench_holdout"
        held.mkdir(parents=True, exist_ok=True)
        f = held / "test_bench_holdout.py"
        f.write_text(spec.holdout)
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", str(f)], cwd=work,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env={**os.environ, "PYTHONPATH": str(work)},
            start_new_session=True)
        try:
            proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
            proc.communicate()
            return False
        return proc.returncode == 0
