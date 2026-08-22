"""Seed a TEMP demo DB across every board lane and serve the board for E2E.

The real ~/.no_human DB is never touched — we monkeypatch load_config in the
api.app module so the real lifespan opens a temp database instead.

Importing this module must be side-effect-free (no argv parsing, no port
binding) so tests/test_board_e2e_lane_model.py can import it directly to
derive expected counts without starting a server. Port parsing and server
startup only happen in main(), guarded by `if __name__ == "__main__"`.

    uv run python e2e/serve_demo.py [port]
"""
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Sibling import — works whether this file is run as a script (its own
# directory is already sys.path[0]) or loaded by file path via importlib
# (tests/test_board_e2e_lane_model.py), which does not get that for free.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lane_model  # noqa: E402

import uvicorn  # noqa: E402

from no_human.config import load_config as _orig_load_config  # noqa: E402
import no_human.api.app  # noqa: F401,E402 — ensure the submodule is imported
from no_human.core.db import Store  # noqa: E402
from no_human.core.lanes import lane_for  # noqa: E402
from no_human.core.task import Task, TaskStatus  # noqa: E402
from no_human.project_model import Project  # noqa: E402

# The package __init__ exports `app`, shadowing the submodule on attribute
# access; fetch the real module object from sys.modules.
appmod = sys.modules["no_human.api.app"]

TEMP_DB = os.environ.get("NH_DEMO_DB", "/tmp/nh_demo_board.db")

# Sentinels substituted at seed() time with the real 2-commit git repo built
# by build_demo_repo() — DEMO_TASKS is module-level (importable without a
# DB or a server) but that repo/sha can only exist once seed() runs.
_DEMO_REPO_SENTINEL = "__DEMO_REPO__"
_DEMO_SHA_SENTINEL = "__DEMO_SHA__"

# Every demo task, as data — the single seed list used both to populate the
# temp DB (seed()) and to derive expected board/outcome counts without a
# server (board_card_count()/outcome_counts()/lane_counts() below). The
# original eleven tasks' titles/statuses/blockers/attempts are unchanged from
# before this refactor — only their shape (inline calls -> data) changed.
# Three tasks were added at the end so both arms of blocked-status routing
# fire and both approve and send-back flows have a distinct AWAITING_APPROVAL
# task to exercise: a second AWAITING_APPROVAL task (send-back target), a
# FAILED task (Outcomes > Failed needs a row), and a blocked-WITHOUT-wake
# task (Needs Answer's blocked arm — the existing blocked task all has a wake
# condition, so without this addition only the working-arm of blocked status
# routing is ever exercised by the demo).
DEMO_TASKS: tuple[dict, ...] = (
    {"title": "Add greet(name) to greet.py", "status": TaskStatus.PENDING},
    {"title": "Gather context for export bug", "status": TaskStatus.CONTEXT},
    {"title": "Implement pagination on /events", "status": TaskStatus.IMPLEMENTING},
    {"title": "Review: refactor auth middleware", "status": TaskStatus.REVIEWING},
    {"title": "Run suite for date-parsing fix", "status": TaskStatus.TESTING},
    {
        "title": "Wait on upstream PR #42", "status": TaskStatus.BLOCKED,
        "blocker": {
            "category": "DEPENDENCY_WAIT", "confidence": 0.9,
            "wake_condition": "pr_merged:org/repo#42",
            "root_cause_hypothesis": "needs upstream helper merged first",
            "question": None, "options": [], "tried": ["pinned to local stub: rejected by reviewer"],
            "goal": "use upstream helper", "evidence": "ImportError until #42 lands",
            "resume_branch": "no-human/abc12345", "resume_commit": "deadbeefcafe",
        },
    },
    {
        "title": "Paused: subscription quota", "status": TaskStatus.PAUSED_QUOTA,
        "blocker": {
            "category": "QUOTA", "confidence": 1.0, "wake_condition": "quota_refreshed",
            "root_cause_hypothesis": "hit subscription usage limit", "goal": "implement X",
            "evidence": "rate limit exceeded", "tried": [],
        },
    },
    {
        "title": "Ambiguous: empty-input behavior", "status": TaskStatus.AWAITING_INPUT,
        "blocker": {
            "category": "AMBIGUITY", "confidence": 0.85, "wake_condition": None,
            "root_cause_hypothesis": "criterion 2 contradicts criterion 1",
            "question": "What should mul() return on empty input?",
            "options": ["raise ValueError", "return 0"],
            "tried": ["interpreted as raise: failed test", "interpreted as 0: failed other test"],
            "goal": "implement mul edge case", "evidence": "spec silent on empty input",
            "resume_branch": "no-human/def67890", "resume_commit": "0011223344",
        },
    },
    {
        "title": "Add mul() to calc — ready for you", "status": TaskStatus.AWAITING_APPROVAL,
        "criteria": ["mul(a,b) returns a*b", "a test covers mul()"],
        "repo": _DEMO_REPO_SENTINEL,
        "attempt": {
            "branch_name": "no-human/aabbccdd", "commit_sha": _DEMO_SHA_SENTINEL,
            "pr_url": "local-pr://remote.git/no-human/aabbccdd", "status": "succeeded",
            "review_passed": 1, "turns_used": 7,
            "review_checklist": {"passed": True, "items": [
                {"label": "mul(a,b) returns a*b", "passed": True,
                 "evidence": "calc.py:5 returns a*b; verified by test_mul"},
                {"label": "a test covers mul()", "passed": True,
                 "evidence": "test_calc.py:9 asserts mul(2,3)==6"},
            ]},
            "test_results": {"ran": True, "ok": True, "passed": 3, "failed": 0,
                              "total": 3, "tamper_flag": False, "output": "3 passed in 0.04s"},
        },
    },
    {
        "title": "Fix timezone off-by-one — merged", "status": TaskStatus.DONE,
        # DONE transitions require an {kind, source} event (SilentCompletion
        # guard in core/db.py:set_status) — this is a demo seed, not a real
        # completion, so it's tagged accordingly rather than impersonating a
        # real orchestrator/watcher/human event.
        "event": {"source": "demo", "kind": "seed_done"},
        "attempt": {
            "branch_name": "no-human/99887766", "pr_url": "local-pr://remote.git/no-human/99887766",
            "status": "succeeded", "review_passed": 1, "turns_used": 5,
        },
    },
    {
        "title": "Impossible: use nonexistent API", "status": TaskStatus.ESCALATED,
        "blocker": {
            "category": "IMPOSSIBLE", "confidence": 0.95, "wake_condition": None,
            "root_cause_hypothesis": "requested stdlib function does not exist",
            "question": "This cannot be done as specified; drop or change it?",
            "options": ["drop the task", "use a real library instead"],
            "tried": ["searched stdlib: not found", "checked PyPI: no such symbol"],
            "goal": "call calc.fast_matmul()", "evidence": "AttributeError: module 'calc' has no 'fast_matmul'",
            "resume_branch": "no-human/55443322", "resume_commit": "aabb00ff",
        },
    },
    # --- added for e2e coverage (see comment above DEMO_TASKS) ---
    {
        "title": "Send back: off-by-one in div() — needs changes", "status": TaskStatus.AWAITING_APPROVAL,
        "criteria": ["div(a,b) returns a/b", "a test covers div()"],
        "attempt": {
            "branch_name": "no-human/ff00aa11", "commit_sha": "cafefeed12345678",
            "pr_url": "local-pr://remote.git/no-human/ff00aa11", "status": "succeeded",
            "review_passed": 0, "turns_used": 4,
            "review_checklist": {"passed": False, "items": [
                {"label": "div(a,b) returns a/b", "passed": False,
                 "evidence": "calc.py:9 raises ZeroDivisionError on b=0, no guard"},
            ]},
            "test_results": {"ran": True, "ok": False, "passed": 1, "failed": 1,
                              "total": 2, "tamper_flag": False, "output": "1 passed, 1 failed in 0.03s"},
        },
    },
    {
        "title": "Broken: sub() returns wrong sign", "status": TaskStatus.FAILED,
        "attempt": {
            "branch_name": "no-human/22dd33ee", "status": "failed",
            "review_passed": 0, "turns_used": 3,
            "test_results": {"ran": True, "ok": False, "passed": 0, "failed": 2,
                              "total": 2, "tamper_flag": False, "output": "2 failed in 0.02s"},
        },
    },
    {
        "title": "Unclear: which retry policy applies", "status": TaskStatus.BLOCKED,
        "blocker": {
            "category": "STUCK", "confidence": 0.6, "wake_condition": None,
            "root_cause_hypothesis": "two retry policies conflict in the spec",
            "question": None, "options": [], "tried": ["read the spec twice"],
            "goal": "implement retry()", "evidence": "spec section 3 and 7 disagree",
            "resume_branch": "no-human/77aa88bb", "resume_commit": "0099887766",
        },
    },
)

# web/src/answerLane.js splits the Needs-Answer lane into fresh/stale
# (STALE_ANSWER_MS = 24h, on escalationTs -> falls back to `updated_at` since
# neither `escalated_at` nor `last_activity` is a real API field) and renders
# a `.lane-stale-divider` toggle once any row is stale. Every DEMO_TASKS row
# is created "now" by mk(), so without this, the divider could never render
# in the demo and board_e2e.py could never exercise that UI path. This backdates
# ONE Needs-Answer row (seed()) well past the threshold — a seed-time fixup,
# not a change to the row's title/status/blocker/attempt "shape".
STALE_ANSWER_TASK_TITLE = "Unclear: which retry policy applies"
STALE_ANSWER_AGE = timedelta(hours=30)


def build_demo_repo() -> tuple[str, str]:
    """Create a real 2-commit git repo so the diff tab renders an actual diff.
    Returns (repo_path, head_sha)."""
    repo = "/tmp/nh_demo_repo"
    subprocess.run(["rm", "-rf", repo], check=True)
    os.makedirs(repo)

    def git(*a):
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True)

    git("init", "-b", "main")
    git("config", "user.email", "demo@e.com")
    git("config", "user.name", "demo")
    with open(f"{repo}/calc.py", "w") as f:
        f.write("def add(a, b):\n    return a + b\n")
    git("add", "-A")
    git("commit", "-m", "base")
    with open(f"{repo}/calc.py", "w") as f:
        f.write("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    git("add", "-A")
    git("commit", "-m", "add mul")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()
    return repo, sha


def _resolve_spec(spec: dict, demo_repo: str, demo_sha: str) -> dict:
    """Substitute the runtime repo/sha sentinels into a DEMO_TASKS entry."""
    resolved = dict(spec)
    if resolved.get("repo") == _DEMO_REPO_SENTINEL:
        resolved["repo"] = demo_repo
    attempt = resolved.get("attempt")
    if attempt is not None and attempt.get("commit_sha") == _DEMO_SHA_SENTINEL:
        attempt = dict(attempt)
        attempt["commit_sha"] = demo_sha
        resolved["attempt"] = attempt
    return resolved


async def seed() -> int:
    store = await Store(TEMP_DB).connect()
    demo_repo, demo_sha = build_demo_repo()

    async def mk(title, status, *, blocker=None, attempt=None, criteria=None,
                 repo="/tmp/demo-repo", event=None):
        t = Task.new(title, repo_path=repo, description=f"Demo task: {title}")
        t.acceptance_criteria = criteria or [f"{title} works", "tests cover it"]
        await store.create_task(t)
        if status != TaskStatus.PENDING:
            # store.set_status requires an {kind, source} event whenever
            # new_status is DONE (SilentCompletion otherwise) — a DONE
            # transition must never write silently. Non-DONE statuses ignore
            # `event` entirely, so this is safe to always pass through.
            await store.set_status(t, status, validate=False, event=event)
        if blocker is not None:
            t.blocker = blocker
            await store.update_task(t)
        if attempt is not None:
            aid = await store.create_attempt(t.id, 1)
            await store.update_attempt(aid, **attempt)
        return t

    for spec in DEMO_TASKS:
        resolved = _resolve_spec(spec, demo_repo, demo_sha)
        await mk(
            resolved["title"], resolved["status"],
            blocker=resolved.get("blocker"), attempt=resolved.get("attempt"),
            criteria=resolved.get("criteria"), event=resolved.get("event"),
            **({"repo": resolved["repo"]} if "repo" in resolved else {}),
        )

    # Backdate the one row that must render past the Needs-Answer stale
    # threshold (see the comment on STALE_ANSWER_TASK_TITLE above) — direct
    # SQL because there is no lifecycle transition for "this happened 30h
    # ago"; store.update_task doesn't touch created_at/updated_at at all.
    stale_ts = (datetime.now(timezone.utc) - STALE_ANSWER_AGE).isoformat()
    await store.db.execute(
        "UPDATE tasks SET created_at = ?, updated_at = ? WHERE title = ?",
        (stale_ts, stale_ts, STALE_ANSWER_TASK_TITLE),
    )
    await store.db.commit()

    # Seed a project so the Settings > Projects tab has data.
    proj = Project.new("demo-project", repo_paths=[demo_repo, "/tmp/demo-repo"])
    await store.create_project(proj)

    # Seed a confirmed rule so Settings > Rules tab has data.
    await store.add_memory(
        mem_type="rule", title="Always add tests",
        content="Every feature or bugfix must include at least one new test.",
        tags=["testing"], source="human", confirmed=True,
    )

    n = len(await store.list_tasks())
    await store.close()
    return n


def _synthetic_task(spec: dict) -> dict:
    """A minimal task-shaped mapping good enough for core.lanes.lane_for —
    it only ever reads `status` and `blocker_wake_condition`."""
    blocker = spec.get("blocker") or {}
    return {"status": spec["status"], "blocker_wake_condition": blocker.get("wake_condition")}


def lane_counts() -> dict[str, int]:
    """{lane_key: count} over DEMO_TASKS, derived via the real server-side
    lane_for() — never hardcoded. Covers board lanes AND outcome lanes."""
    counts = {lane.key: 0 for lane in lane_model.LANES}
    for spec in DEMO_TASKS:
        key = lane_for(_synthetic_task(spec))
        counts[key] = counts.get(key, 0) + 1
    return counts


def board_card_count() -> int:
    """Total number of task cards the board renders — every DEMO_TASKS entry
    that routes to a non-outcome lane (Needs Answer / Working / Review PR)."""
    counts = lane_counts()
    return sum(counts.get(lane.key, 0) for lane in lane_model.BOARD_LANES)


def outcome_counts() -> dict[str, int]:
    """{lane_key: count} restricted to the outcome lanes (Done/Failed) shown
    on the sidebar Outcomes pages, not on the board."""
    counts = lane_counts()
    return {lane.key: counts.get(lane.key, 0) for lane in lane_model.OUTCOME_LANES}


def _fake_load_config():
    c = _orig_load_config()
    c.data["database"]["path"] = TEMP_DB
    c.data.setdefault("onboarding", {})["completed"] = True
    # The e2e approve check must observe a deterministic non-merging approve
    # flow (HTTP 200, `approved_at` stamped, status stays awaiting_approval,
    # no actual git-host merge attempted) — load-bearing for that check.
    c.data.setdefault("approve_merge", {})["enabled"] = False
    return c


async def _no_claimable_tasks(self, status):  # noqa: ARG001 - Store method shape
    """Monkeypatched onto `Store` for this demo process only (see main()).

    `api.app`'s lifespan always starts the embedded worker ("board up =
    worker up" — that invariant is not ours to change here), and the
    scheduler's `_claimable()` calls this method to find PENDING/IMPLEMENTING
    rows to dispatch. Left alone, it finds the demo's own seeded PENDING task
    ("Add greet(name) to greet.py") and the seeded IMPLEMENTING task
    ("Implement pagination on /events") within its first, unguarded tick —
    before `run_forever` ever sleeps — and tries to actually run them against
    `repo="/tmp/demo-repo"`, which is not a real git checkout. That failed
    open flips both rows to FAILED seconds after boot, silently corrupting
    the very fixture counts `board_e2e.py`/`lane_model.py` derive from.

    This demo is a static UI fixture, not a live work queue (the same reason
    `_fake_load_config` pins `approve_merge.enabled = False` above): returning
    no claimable rows keeps the embedded worker alive and ticking (so
    `/api/worker/status` still reports a running pool) while guaranteeing it
    never mutates a seeded row's status. `Store.list_claimable_tasks` has
    exactly one caller in the whole tree (`Scheduler._claimable`), so this
    monkeypatch cannot silently change any board/API read path this harness
    checks.
    """
    return []


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8488
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(TEMP_DB + ext)
        except FileNotFoundError:
            pass
    count = asyncio.run(seed())
    print(f"[seed] {count} demo tasks in {TEMP_DB}", flush=True)
    appmod.load_config = _fake_load_config  # lifespan resolves this at call time
    # See _no_claimable_tasks: without this, the embedded worker claims and
    # fails the demo's own PENDING/IMPLEMENTING rows within seconds of boot.
    Store.list_claimable_tasks = _no_claimable_tasks
    uvicorn.run(appmod.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
