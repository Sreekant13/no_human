"""Seed a TEMP demo DB across every board lane and serve the board for E2E.

The real ~/.no_human DB is never touched — we monkeypatch load_config in the
api.app module so the real lifespan opens a temp database instead.

    uv run python e2e/serve_demo.py [port]
"""
import asyncio
import os
import subprocess
import sys

import uvicorn

from no_human.config import load_config as _orig_load_config
import no_human.api.app  # noqa: F401 — ensure the submodule is imported
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus

# The package __init__ exports `app`, shadowing the submodule on attribute
# access; fetch the real module object from sys.modules.
appmod = sys.modules["no_human.api.app"]

TEMP_DB = os.environ.get("NH_DEMO_DB", "/tmp/nh_demo_board.db")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8488


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


async def seed() -> int:
    store = await Store(TEMP_DB).connect()
    demo_repo, demo_sha = build_demo_repo()

    async def mk(title, status, *, blocker=None, attempt=None, criteria=None,
                 repo="/tmp/demo-repo"):
        t = Task.new(title, repo_path=repo, description=f"Demo task: {title}")
        t.acceptance_criteria = criteria or [f"{title} works", "tests cover it"]
        await store.create_task(t)
        if status != TaskStatus.PENDING:
            await store.set_status(t, status, validate=False)
        if blocker is not None:
            t.blocker = blocker
            await store.update_task(t)
        if attempt is not None:
            aid = await store.create_attempt(t.id, 1)
            await store.update_attempt(aid, **attempt)
        return t

    await mk("Add greet(name) to greet.py", TaskStatus.PENDING)
    await mk("Gather context for export bug", TaskStatus.CONTEXT)
    await mk("Implement pagination on /events", TaskStatus.IMPLEMENTING)
    await mk("Review: refactor auth middleware", TaskStatus.REVIEWING)
    await mk("Run suite for date-parsing fix", TaskStatus.TESTING)
    await mk("Wait on upstream PR #42", TaskStatus.BLOCKED, blocker={
        "category": "DEPENDENCY_WAIT", "confidence": 0.9,
        "wake_condition": "pr_merged:org/repo#42",
        "root_cause_hypothesis": "needs upstream helper merged first",
        "question": None, "options": [], "tried": ["pinned to local stub: rejected by reviewer"],
        "goal": "use upstream helper", "evidence": "ImportError until #42 lands",
        "resume_branch": "no-human/abc12345", "resume_commit": "deadbeefcafe",
    })
    await mk("Paused: subscription quota", TaskStatus.PAUSED_QUOTA, blocker={
        "category": "QUOTA", "confidence": 1.0, "wake_condition": "quota_refreshed",
        "root_cause_hypothesis": "hit subscription usage limit", "goal": "implement X",
        "evidence": "rate limit exceeded", "tried": [],
    })
    await mk("Ambiguous: empty-input behavior", TaskStatus.AWAITING_INPUT, blocker={
        "category": "AMBIGUITY", "confidence": 0.85, "wake_condition": None,
        "root_cause_hypothesis": "criterion 2 contradicts criterion 1",
        "question": "What should mul() return on empty input?",
        "options": ["raise ValueError", "return 0"],
        "tried": ["interpreted as raise: failed test", "interpreted as 0: failed other test"],
        "goal": "implement mul edge case", "evidence": "spec silent on empty input",
        "resume_branch": "no-human/def67890", "resume_commit": "0011223344",
    })
    await mk("Add mul() to calc — ready for you", TaskStatus.AWAITING_APPROVAL,
             criteria=["mul(a,b) returns a*b", "a test covers mul()"],
             repo=demo_repo,
             attempt={
                 "branch_name": "no-human/aabbccdd", "commit_sha": demo_sha,
                 "pr_url": "local-pr://remote.git/no-human/aabbccdd", "status": "succeeded",
                 "review_passed": 1, "turns_used": 7,
                 "review_checklist": {"passed": True, "items": [
                     {"criterion": "mul(a,b) returns a*b", "passed": True,
                      "evidence": "calc.py:5 returns a*b; verified by test_mul"},
                     {"criterion": "a test covers mul()", "passed": True,
                      "evidence": "test_calc.py:9 asserts mul(2,3)==6"},
                 ]},
                 "test_results": {"ran": True, "ok": True, "passed": 3, "failed": 0,
                                  "total": 3, "tamper_flag": False, "output": "3 passed in 0.04s"},
             })
    await mk("Fix timezone off-by-one — merged", TaskStatus.DONE, attempt={
        "branch_name": "no-human/99887766", "pr_url": "local-pr://remote.git/no-human/99887766",
        "status": "succeeded", "review_passed": 1, "turns_used": 5,
    })
    await mk("Impossible: use nonexistent API", TaskStatus.ESCALATED, blocker={
        "category": "IMPOSSIBLE", "confidence": 0.95, "wake_condition": None,
        "root_cause_hypothesis": "requested stdlib function does not exist",
        "question": "This cannot be done as specified; drop or change it?",
        "options": ["drop the task", "use a real library instead"],
        "tried": ["searched stdlib: not found", "checked PyPI: no such symbol"],
        "goal": "call calc.fast_matmul()", "evidence": "AttributeError: module 'calc' has no 'fast_matmul'",
        "resume_branch": "no-human/55443322", "resume_commit": "aabb00ff",
    })
    n = len(await store.list_tasks())
    await store.close()
    return n


def _fake_load_config():
    c = _orig_load_config()
    c.data["database"]["path"] = TEMP_DB
    return c


def main() -> None:
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(TEMP_DB + ext)
        except FileNotFoundError:
            pass
    count = asyncio.run(seed())
    print(f"[seed] {count} demo tasks in {TEMP_DB}", flush=True)
    appmod.load_config = _fake_load_config  # lifespan resolves this at call time
    uvicorn.run(appmod.app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
