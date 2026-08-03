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

import asyncio
import contextlib
import logging
import shutil
import subprocess
import sys
import time
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..core.db import USAGE_ROLES, usage_columns_for, Store
from ..core.pricing import CACHE_CREATION_WEIGHT, CACHE_READ_WEIGHT
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
    # Which repetition of this spec produced this score (0-based). A run with
    # `--trials N` records N scores per spec, all sharing `task_id` and
    # differing only here — so (task_id, trial) is the identity a checkpoint
    # resumes on, and a single-trial run is exactly today's shape with every
    # trial == 0.
    trial: int = 0
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
        burn and the plain token_ratio is blind to it.

        The multipliers used to be inline literals here, which made this the
        second price table in the tree; they now come from `core.pricing`, the
        same one the budget gate weights its caps with. Values unchanged — this
        is a de-duplication, not a re-pricing, so published ratios still stand.
        The arithmetic is kept in float (rather than routed through
        `pricing.weighted_tokens`, which floors to an int for the caps) so a
        ratio over small samples is not quantised.
        """
        orig = (self.orig_tokens
                + CACHE_READ_WEIGHT * self.orig_cache_tokens
                + CACHE_CREATION_WEIGHT * self.orig_cache_creation_tokens)
        if orig <= 0:
            return None
        nh = (self.nh_tokens
              + CACHE_READ_WEIGHT * self.nh_cache_tokens
              + CACHE_CREATION_WEIGHT * self.nh_cache_creation_tokens)
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
            "trial": self.trial,
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
            # (a real work repo did ship an active pre-push), which would execute
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

    NOT all refs. A live repo has automation of its own — background jobs that
    push state to their own branches continuously, outside any namespace the
    agent uses — and comparing every ref made one such push look like a bench
    escape (it crashed two specs on a run where the bench wrote
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


# Watchdog cadence and how long a cancellation gets to unwind before we stop
# waiting on it — the observed hang was inside subprocess teardown.
_WATCHDOG_POLL_S = 30.0
_WATCHDOG_UNWIND_S = 60.0


def _make_sink(persister, forward, task_id):
    """Build the bench event sink AND the last-event clock it feeds.

    Extracted so the WIRING is testable. Left inline, the one line that
    refreshes the clock could be deleted with every watchdog test still green —
    and deleting it makes the watchdog fire on a spec that is long but ALIVE,
    which is the false-kill direction: a slow success recorded as a capability
    failure.
    """
    last_event = [time.monotonic()]

    def sink(event: dict) -> None:
        event.setdefault("ts", time.time())
        event.setdefault("task_id", task_id)
        last_event[0] = time.monotonic()      # proof of life for the watchdog
        persister.record(event)
        forward(event)

    return sink, last_event


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

    class SpecStalled(RuntimeError):
        """A spec emitted no event for longer than the stuck-active threshold."""

    async def _run_with_watchdog(self, orch, task, last_event):
        """`orch.run_task` under the SAME stuck-active policy the server applies.

        The product already has this watchdog — `blockers.stuck_active_minutes`
        (added for the 2026-07-11 reviewer hang) — but it lives in
        `blockers/wake.py`, which only the SERVER runs. `nh bench run` drives
        the orchestrator directly, so nothing was watching, and one hung
        Agent-SDK session stopped an entire run silently and indefinitely.
        Observed live: a spec sat 9 minutes at 0% CPU, no children, no sockets,
        blocked in `__wait4`, and would have sat there forever.

        Same knob and same semantic as wake.py deliberately — NO EVENT for N
        minutes, not total wall clock. A spec that is slow but ALIVE keeps
        emitting, so it is never killed; only silence trips this. Reusing the
        key means the two cannot drift, and <= 0 disables, exactly as there.
        """
        limit_min = float((self.config.get("blockers") or {})
                          .get("stuck_active_minutes", 40))
        runner = asyncio.ensure_future(orch.run_task(task))
        if limit_min <= 0:
            return await runner                      # watchdog disabled
        limit_s = limit_min * 60.0
        while True:
            done, _ = await asyncio.wait({runner}, timeout=_WATCHDOG_POLL_S)
            if done:
                return await runner
            silent_s = time.monotonic() - last_event[0]
            if silent_s >= limit_s:
                runner.cancel()
                # Give the cancellation a bounded chance to unwind. The hang
                # observed live was INSIDE subprocess teardown, so waiting on
                # it forever would reproduce the very defect being fixed.
                # Only the cancel's own CancelledError and the unwind
                # TimeoutError are expected; anything else is a real teardown
                # bug and must not be swallowed under the SpecStalled below.
                with contextlib.suppress(asyncio.CancelledError,
                                         asyncio.TimeoutError):
                    await asyncio.wait_for(runner, timeout=_WATCHDOG_UNWIND_S)
                raise NorthStarRunner.SpecStalled(
                    f"no event for {silent_s / 60:.0f} min "
                    f"(limit {limit_min:.0f}); the agent session hung")

    async def run_one(self, spec: BenchTask, *, workdir: Path) -> BenchScore:
        if not spec.runnable:
            return self._skipped(spec)

        # `runnable` was decided at spec GENERATION time; nothing re-checked it
        # here. A spec whose repo path no longer resolves therefore reached
        # _setup_sandbox, died in `git clone`, and was booked as
        # outcome_status="crashed", goal_satisfied=False — a broken INSTRUMENT
        # scored as a capability failure of the agent. An unavailable repo is a
        # skip, exactly as it is at generation time.
        #
        # Resolve BEFORE building the Path: Path("") is PosixPath("."), and so
        # are "." and "./", all of which would make the .git probe test the
        # RUNNER's own cwd, pass inside any checkout, and sandbox-copy no_human
        # itself as the spec's subject. A relative path is meaningless here for
        # the same reason — the spec records the original session's absolute cwd.
        raw_path = spec.repo.get("path") or ""
        if not str(raw_path).strip():
            return self._skipped(spec, "spec has no repo.path")
        src_repo = Path(str(raw_path).strip())
        if not src_repo.is_absolute():
            return self._skipped(
                spec, f"repo.path is not absolute: {raw_path!r}")
        # .exists(), not .is_dir(): a git worktree or submodule has .git as a
        # FILE, and treating those as missing would skip perfectly good repos.
        if not (src_repo / ".git").exists():
            return self._skipped(spec, f"repo missing at run time: {src_repo}")
        # Hand the VALIDATED path on. _setup_sandbox re-derives
        # Path(spec.repo["path"]) itself, so validating a stripped copy while it
        # cloned the raw one meant a whitespace-padded path passed this guard
        # and then died in `git clone` — this guard's own code producing the
        # crash it exists to prevent.
        spec.repo["path"] = str(src_repo)

        # Every subprocess below runs off-loop: under `--parallel` a sandbox
        # copy (multi-GB cp/clone) or the 300s holdout pytest would otherwise
        # block EVERY in-flight spec's SDK stream — and, worse, freeze the
        # stuck-watchdog clock so a quiet-but-alive spec could be booked as a
        # false SpecStalled crash.
        refs_before = await asyncio.to_thread(_ref_signature, src_repo)
        work = await asyncio.to_thread(_setup_sandbox, spec, workdir)
        base_sha = await asyncio.to_thread(_git, work, "rev-parse", "HEAD")

        store = await Store(workdir / "bench.db").connect()
        try:
            # Persist the event stream into the sandbox's own bench.db.
            # Supervisor/budget events only exist in this stream; dropping it
            # left the v9 budget-class regression undrillable — whether the
            # 85% wrap-up nudge fired or was ignored was unknowable post hoc.
            task = _bench_task(spec, work)
            async with EventPersister(store, task.id) as persister:
                forward = self._event_sink or (lambda e: None)

                sink, last_event = _make_sink(persister, forward, task.id)

                orch = Orchestrator(
                    store, self.config, self.backend_factory(spec),
                    SlackNotifier(None), reviewer=self.reviewer,
                    event_sink=sink,
                )
                await store.create_task(task)

                t0 = time.monotonic()
                outcome = await self._run_with_watchdog(
                    orch, task, last_event)
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

        if await asyncio.to_thread(_ref_signature, src_repo) != refs_before:
            raise RuntimeError(
                f"SOURCE REPO REFS CHANGED during bench run of {spec.id} — "
                "push-proofing failed; halt the bench")

        return await self._score(spec, outcome, work, base_sha,
                                 attempts, elapsed,
                                 events=event_digest)

    def _skipped(self, spec: BenchTask, reason: str = "") -> BenchScore:
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
            # REDACTED. The run-time reasons below name the repo path, which
            # after the repo-map translation is the operator's real local
            # checkout — and this note is rendered into the tracked report.
            # Unredacted it also blocks publication outright: the report
            # writer refuses any rendered artifact containing a /Users/ path,
            # with no --force override, so a single run-time skip would kill
            # an otherwise-clean run at the final write.
            notes=redact_local_path(
                f"skipped: {reason or spec.skip_reason}", spec),
        )

    async def _score(self, spec: BenchTask, outcome, work: Path,
                     base_sha: str, attempts: list[dict],
                     elapsed: float,
                     events: list[dict] | None = None) -> BenchScore:
        status = outcome.status
        # EVERY registered role, per price class (angle-4 finding: coder-only
        # summation rigged the north-star ratio). The role list is imported,
        # never enumerated here: this comment used to end "planner/supervisor
        # columns pending B2" while the code summed exactly four literals, and
        # the 10%-of-cost target the card publishes is only as honest as the
        # longest of those lists. A role that exists in the schema but not in
        # this sum is spend the benchmark hands the product for free.
        def _class_total(idx: int) -> int:
            keys = [usage_columns_for(t)[idx] for t in USAGE_ROLES]
            return sum(int(a.get(k) or 0) for a in attempts for k in keys)

        nh_tokens = _class_total(0)
        nh_cache = _class_total(1)
        nh_creation = _class_total(2)
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
        diff_ref = await asyncio.to_thread(self._agent_diff_ref, outcome, work)
        if diff_ref != "HEAD":
            r = await asyncio.to_thread(
                subprocess.run,
                ["git", "checkout", "-q", "-f", "--detach", diff_ref],
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
            agent_diff = (await asyncio.to_thread(
                subprocess.run,
                ["git", "diff", base_sha, diff_ref], cwd=work,
                capture_output=True, text=True)).stdout
            verdict = await self.goal_judge.judge(
                request=spec.request, criteria=spec.acceptance_criteria,
                agent_diff=agent_diff, outcome_status=status.value,
                report=(getattr(outcome, "report", "") or getattr(outcome, "detail", "") or ""),
                repo_path=str(work))
        score.mergeable = await asyncio.to_thread(self._holdout_ok, spec, work)
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
