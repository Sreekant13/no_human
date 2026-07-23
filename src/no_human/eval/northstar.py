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

import logging
import shutil
import subprocess
import sys
import time
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..core.db import Store
from ..core.events import EventPersister
from ..core.orchestrator import Orchestrator
from ..core.task import Task, TaskStatus
from ..notify.slack import SlackNotifier
from .bench_task import BenchTask, redact_local_path, spec_project_name

BackendFactory = Callable[[BenchTask], Any]

# Off-ramps that count as "reached the human gate" for gate-kind outcomes vs
# honest-escalation outcomes (expect_escalation specs). DONE is a gate state
# too (review D9): report-deliverable pipelines (investigation / design_doc /
# clean code_review) terminate DONE with the report as the deliverable — the
# judge evaluates that report; auto-failing DONE would score every successful
# rerouted report task ❌ without the judge ever seeing it.
_GATE_STATES = {TaskStatus.AWAITING_APPROVAL, TaskStatus.DONE}
_HONEST_STOPS = {TaskStatus.ESCALATED, TaskStatus.AWAITING_INPUT,
                 TaskStatus.BLOCKED}


_DIGEST_MAX_EVENTS = 300
_DIGEST_MAX_TEXT = 200


def _digest_events(rows: list[dict]) -> list[dict]:
    """Compact the persisted event stream for the score record: kind + text
    (truncated) + ts. Keeps the LAST _DIGEST_MAX_EVENTS — terminal behavior
    (budget nudges, escalations, wrap-ups) clusters at the end."""
    out = []
    for e in rows[-_DIGEST_MAX_EVENTS:]:
        text = str(e.get("text") or e.get("message") or "")[:_DIGEST_MAX_TEXT]
        out.append({"kind": str(e.get("kind") or e.get("source") or ""),
                    "text": text, "ts": e.get("ts")})
    return out


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
    project: str = ""                   # repo basename — per-project payoff view
    notes: str = ""
    # Capped digest of the run's event stream. bench.db dies with the
    # sandbox cleanup; the digest rides the score into progress.json /
    # latest.json so completed specs stay drillable post hoc (v11 live:
    # early escalations whose reasons were already deleted).
    events: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    @property
    def token_ratio(self) -> float | None:
        """nh non-cache burn / original non-cache burn. <1.0 = no_human was
        cheaper than the babysat session. None when the original is unknown.

        nh_tokens INCLUDES the reviewer (B1 angle-4 finding: coder-only
        summation rigged the ratio in no_human's favor). Planner burn lands in plan_* columns (B2 #5); supervisor/
        distillation burn is still uncaptured (B2 #6) — a small residual
        under-count, labeled in the report."""
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
            "project": self.project,
            "token_ratio": (round(self.token_ratio, 3)
                            if self.token_ratio is not None else None),
            "cost_ratio": (round(self.cost_ratio, 3)
                           if self.cost_ratio is not None else None),
            "notes": self.notes,
            "events": self.events,
        }


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


def _sandbox_copy(src: Path, dst: Path) -> None:
    """Copy a repo into a sandbox with ISOLATED inodes, fast.

    Darwin/APFS: `cp -c` clonefile — instant copy-on-write, new inodes, so
    no write in the sandbox can ever reach the source (review of PR #115
    proved hardlinked object stores CAN be written through). Elsewhere:
    `git clone --no-hardlinks` — slower, equally isolated."""
    if sys.platform == "darwin":
        proc = subprocess.run(["cp", "-Rc", str(src), str(dst)],
                              capture_output=True)
        if proc.returncode == 0:
            # Clone parity: a file copy carries the source's ACTIVE hooks
            # (metrics-core-query-service ships a pre-push), which would execute
            # foreign code on the coder's sandbox pushes — git clone never
            # copies hooks. Strip them.
            hooks = dst / ".git" / "hooks"
            if hooks.exists():
                shutil.rmtree(hooks)
                hooks.mkdir()
            return
        # cp failed (mid-copy ENOSPC, exotic volume) — clear any partial
        # dst or the fallback clone dies on "destination not empty",
        # masking the real error (review F5).
        if dst.exists():
            shutil.rmtree(dst)
    subprocess.run(["git", "clone", "--no-hardlinks", str(src), str(dst)],
                   check=True, capture_output=True)


def _setup_sandbox(spec: BenchTask, workdir: Path) -> Path:
    """Clone the real repo at the spec's pin; re-point origin to a local bare.

    The sandbox is an APFS clonefile copy (cp -c): instant copy-on-write at
    any size — v8's two crashes were `git clone --no-hardlinks` COPYING a
    3.8GB object store onto a starved disk (exit 128) — with fully ISOLATED
    inodes. Hardlinked clones were rejected in review by experiment: a
    sandboxed `chmod +w` + in-place append writes THROUGH a shared object
    inode into the operator's live repo, and neither the guard nor the
    ref-signature tamper check sees it. Non-APFS platforms fall back to the
    slow-but-isolated copy clone."""
    src = Path(spec.repo.get("path", ""))
    work = workdir / "work"
    _sandbox_copy(src, work)
    pin = spec.repo.get("pin") or "HEAD"
    # The source may be DIRTY (uncommitted/untracked files ride along in a
    # file-level copy, and v7's ns-4092c756 crash was a checkout over a dirty
    # tree): force the worktree to exactly the pinned commit, clean.
    _git(work, "reset", "--hard", "HEAD" if pin == "HEAD" else pin)
    _git(work, "clean", "-fdx")
    if pin != "HEAD":
        _git(work, "checkout", "--detach", pin)
        # The coder needs a branch to work from.
        _git(work, "checkout", "-b", "bench-base")
    _git(work, "config", "user.email", "bench@no_human")
    _git(work, "config", "user.name", "nh-bench")

    bare = workdir / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)],
                   check=True, capture_output=True)
    # A copied repo carries the SOURCE's remotes (possibly none, possibly
    # several — the push-proof guard resolves only origin, so a ride-along
    # upstream would be a guard-invisible escape, review F4). Remove them
    # ALL, then add origin at the local bare.
    existing = subprocess.run(["git", "remote"], cwd=work,
                              capture_output=True, text=True).stdout.split()
    for r in existing:
        subprocess.run(["git", "remote", "remove", r], cwd=work,
                       capture_output=True)
    subprocess.run(["git", "config", "--unset-all", "remote.pushDefault"],
                   cwd=work, capture_output=True)
    _git(work, "remote", "add", "origin", str(bare))

    # HARD GUARD — BEFORE any push (review finding: guard-after-push detects
    # an escape only after the write has landed in the real repo).
    origin = Path(_git(work, "remote", "get-url", "origin")).resolve()
    if not str(origin).startswith(str(workdir.resolve())):
        raise RuntimeError(
            f"push-proofing failed: origin {origin} escapes sandbox {workdir}")

    _git(work, "push", "origin", "HEAD")
    return work


def _ref_signature(repo: Path) -> str:
    """Tamper check for the SOURCE repo: the refs a bench escape would touch.

    NOT all refs. A live repo has automation of its own — incident-monitor's
    data-* branches carry alert state pushed continuously by the operator's
    jobs, and comparing every ref made an unrelated background push look like
    a bench escape (it crashed two specs on a run where the bench wrote
    nothing). The bench can only ever create refs under the agent's own
    namespaces, so the signature watches exactly those plus the refs a task
    could rewrite: HEAD's branch and any no-human/* or bench-* ref.
    """
    try:
        out = subprocess.run(
            ["git", "for-each-ref",
             "--format=%(refname) %(objectname)",
             "refs/heads/no-human/", "refs/heads/bench-", "refs/heads/nh-"],
            cwd=repo, capture_output=True, text=True).stdout
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                              capture_output=True, text=True).stdout.strip()
        branch = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=repo,
                                capture_output=True, text=True).stdout.strip()
        return f"{out}\nHEAD {branch} {head}\n"
    except OSError:
        return ""


def _bench_task(spec: BenchTask, work: Path) -> Task:
    """Build the orchestrator Task for a spec EXACTLY as the product front door
    would — including kind classification (`nh` runs classify_kind at intake,
    cli/commands.py). Without it every replayed task defaulted to the feature
    pipeline, so review/question/plan specs never reached their read-only
    report terminals (v6 taxonomy, 2026-07-16)."""
    from ..intake.classify import classify_kind

    task = Task.new(spec.title, repo_path=str(work), description=spec.request)
    task.acceptance_criteria = list(spec.acceptance_criteria)
    task.kind = classify_kind(task).kind.value
    return task


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
            # Persist the event stream into the sandbox's own bench.db.
            # Supervisor/budget events only exist in this stream; dropping it
            # left the v9 budget-class regression undrillable — whether the
            # 85% wrap-up nudge fired or was ignored was unknowable post hoc.
            task = _bench_task(spec, work)
            async with EventPersister(store, task.id) as persister:
                forward = self._event_sink or (lambda e: None)

                def sink(event: dict) -> None:
                    event.setdefault("ts", time.time())
                    event.setdefault("task_id", task.id)
                    persister.record(event)
                    forward(event)

                orch = Orchestrator(
                    store, self.config, self.backend_factory(spec),
                    SlackNotifier(None), reviewer=self.reviewer,
                    event_sink=sink,
                )
                await store.create_task(task)

                t0 = time.monotonic()
                outcome = await orch.run_task(task)
                elapsed = time.monotonic() - t0

                attempts = await store.list_attempts(task.id)
            # OUTSIDE the persister context: its __aexit__ drains the buffer,
            # so only here is the stream fully flushed to bench.db. The digest
            # is telemetry, never a verdict — a harvest failure must not
            # downgrade a completed spec to "crashed" (r1 F2).
            try:
                event_digest = _digest_events(
                    await store.list_events(task.id))
            except Exception:  # noqa: BLE001 — telemetry only
                logging.getLogger(__name__).warning(
                    "event digest harvest failed for %s", spec.id)
                event_digest = []
        finally:
            await store.close()

        if _ref_signature(src_repo) != refs_before:
            raise RuntimeError(
                f"SOURCE REPO REFS CHANGED during bench run of {spec.id} — "
                "push-proofing failed; halt the bench")

        return await self._score(spec, outcome, work, base_sha,
                                 attempts, elapsed,
                                 events=event_digest)

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
            project=spec_project_name(spec),
            notes=f"skipped: {spec.skip_reason}",
        )

    async def _score(self, spec: BenchTask, outcome, work: Path,
                     base_sha: str, attempts: list[dict],
                     elapsed: float,
                     events: list[dict] | None = None) -> BenchScore:
        status = outcome.status
        # Coder + reviewer buckets (angle-4 finding: coder-only summation
        # rigged the north-star ratio; planner/supervisor columns pending B2).
        nh_tokens = sum(int(a.get("tokens_used") or 0)
                        + int(a.get("review_tokens_used") or 0)
                        + int(a.get("plan_tokens_used") or 0)
                        + int(a.get("utility_tokens_used") or 0)
                        for a in attempts)
        nh_cache = sum(int(a.get("cache_read_tokens") or 0)
                       + int(a.get("review_cache_read_tokens") or 0)
                       + int(a.get("plan_cache_read_tokens") or 0)
                       + int(a.get("utility_cache_read_tokens") or 0)
                       for a in attempts)
        nh_creation = sum(int(a.get("cache_creation_tokens") or 0)
                          + int(a.get("review_cache_creation_tokens") or 0)
                          + int(a.get("plan_cache_creation_tokens") or 0)
                          + int(a.get("utility_cache_creation_tokens") or 0)
                          for a in attempts)
        turns = sum(int(a.get("turns_used") or 0) for a in attempts)
        orig = spec.original or {}
        toks = orig.get("tokens", {}) or {}

        score = BenchScore(
            events=list(events or []),
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
            project=spec_project_name(spec),
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

        # Put the work dir on the coder's PR branch. The orchestrator commits the
        # coder's work there and can leave HEAD at the base pin — so the holdout
        # tests AND the judge's own filesystem checks (ls / git status in
        # repo_path) would otherwise see BASE and contradict the real work
        # (verified live: ns-f5cb4cb0's 3 review files appear only after checking
        # out the PR branch; the judge ran `ls`/`git status`, saw none, and
        # false-scored a "fabrication"). #92 fixed the agent_diff; this makes the
        # holdout + judge repo view consistent with it. FORCE the checkout: the
        # deliverable is COMMITTED on the branch, so uncommitted sandbox cruft that
        # could block a plain checkout is not part of the PR — a silent
        # keep-HEAD-at-base would reintroduce the exact empty-view bug (review D1).
        diff_ref = self._agent_diff_ref(outcome, work)
        if diff_ref != "HEAD":
            r = subprocess.run(["git", "checkout", "-q", "-f", "--detach", diff_ref],
                               cwd=work, capture_output=True)
            if r.returncode != 0:
                logging.getLogger(__name__).warning(
                    "bench: could not checkout %s in %s — judge repo view is stale, "
                    "may under-score: %s", diff_ref, work, r.stderr.decode()[:200])
        # Judge FIRST, on the clean coder work — BEFORE _holdout_ok writes its own
        # tests/bench_holdout/ file into the tree, which a judge told to `ls`/`git
        # status` would otherwise see and puzzle over (review D2).
        verdict = None
        if self.goal_judge is not None:
            # Diff base against the PR branch (robust even if the checkout failed —
            # the coder's commits live there regardless). The judge also sees the
            # agent's REPORT (its answer/review/plan), preferred over the terse
            # status `detail`.
            agent_diff = subprocess.run(
                ["git", "diff", base_sha, diff_ref], cwd=work,
                capture_output=True, text=True).stdout
            verdict = await self.goal_judge.judge(
                request=spec.request, criteria=spec.acceptance_criteria,
                agent_diff=agent_diff, outcome_status=status.value,
                report=(getattr(outcome, "report", "") or getattr(outcome, "detail", "") or ""),
                repo_path=str(work))
        score.mergeable = self._holdout_ok(spec, work)
        if verdict is not None:
            score.goal_satisfied = bool(verdict.satisfied) and \
                score.mergeable in (True, None)
            # 2000, not 400: the drill of every done-but-unsatisfied spec
            # starts from this field, and 400 chars cut ns-7ef821b2's verdict
            # off mid-"BUT ..." — the reason it failed was unrecoverable.
            # Defence in depth: the judge only ever sees the tmp sandbox path,
            # so this is not a known leak channel — but it is the one remaining
            # free-text field that reaches the tracked report, and "is redaction
            # applied everywhere notes are written" should have one answer.
            score.notes = redact_local_path(verdict.evidence, spec)[:2000]
        else:
            score.goal_satisfied = score.mergeable in (True, None)
            score.notes = "no judge injected; holdout-only scoring"
        return score

    @staticmethod
    def _agent_diff_ref(outcome, work: Path) -> str:
        """The ref to diff the agent's work against. The orchestrator can leave
        the work-dir HEAD at base while the coder's commits live on the PR branch,
        so prefer the recorded ``pr_branch`` (local ref, then ``origin/``) when it
        exists — else fall back to HEAD. Fixes false-empty diffs that made the
        judge score a real PR as a fabrication."""
        ctx = getattr(getattr(outcome, "task", None), "context", None) or {}
        pr_branch = ctx.get("pr_branch")
        if pr_branch:
            for cand in (pr_branch, f"origin/{pr_branch}"):
                if subprocess.run(["git", "rev-parse", "--verify", cand],
                                  cwd=work, capture_output=True).returncode == 0:
                    return cand
            # Recorded but unresolvable — WARN rather than silently fall back to
            # HEAD, which would re-mask the empty-diff false-failure (review nit).
            logging.getLogger(__name__).warning(
                "bench: pr_branch %r not resolvable in %s — diffing HEAD (may "
                "under-capture the deliverable)", pr_branch, work)
        return "HEAD"

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
