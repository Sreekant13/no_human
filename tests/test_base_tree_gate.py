"""Invocation errors are "infrastructure" only if the BASE tree shows them too
(docs/ARCH_REVIEW.md B2 #4).

An import/collection error used to be classified infrastructure uncondition-
ally: the reviewer was told to discount it and the test gate proceeded without
evidence — so a coder-INTRODUCED import breakage (a test importing a module
that doesn't exist, a gutted __init__) shipped as a PR with zero test signal.
The deterministic tiebreaker: run the same command on the base tree. Base
errors the same way → genuinely environmental, proceed as before. Base runs →
the change broke the runner; the attempt FAILS.
"""

import subprocess

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import (  # noqa: F401
    FakeBackend,
    _config,
    _git,
    bare_repo,
    store,
)


def _orch(store, tmp_path, backend):
    cfg = _config(tmp_path)
    # One attempt is enough to prove the gate; retries just repeat it slowly.
    cfg.data["bounds"] = {"max_attempts": 1}
    return Orchestrator(store, cfg.data, backend, SlackNotifier(None))


async def test_coder_introduced_import_breakage_fails_the_attempt(
    bare_repo, tmp_path, store
):
    # the fixture repo has no pytest marker — without one detect_command()
    # finds nothing and the whole test phase is skipped
    (bare_repo / "pytest.ini").write_text("[pytest]\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "pytest marker")

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )
        # a new test that cannot even be collected — import breakage the
        # tamper guard does not flag (test count goes UP)
        (cwd / "test_mul.py").write_text(
            "import nonexistent_dep_xyz  # noqa\nfrom calc import mul\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    orch = _orch(store, tmp_path, FakeBackend(mutate))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "failed"
    reason = (attempts[-1]["failure_reason"] or "").lower()
    assert "base" in reason, (
        "the failure must SAY the base-tree comparison decided it: " + reason
    )


async def test_env_dependent_project_downgrades_reproduces_to_undeterminable(
    bare_repo, tmp_path, store
):
    """Review F1: a bare detached worktree gets none of the attempt's
    env_setup, so on a setup-dependent project a base-tree invocation error
    proves nothing. The verdict must downgrade to undeterminable (None), not
    confidently read 'environmental'."""
    (bare_repo / "conftest.py").write_text("import nonexistent_dep_xyz  # noqa\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "error on base too")

    from no_human.vcs.git import GitRepo
    orch = _orch(store, tmp_path, FakeBackend(lambda cwd: None))
    repo = GitRepo(str(bare_repo))

    confident = await orch._invocation_error_reproduces_on_base(
        repo, None, "main", env_dependent=False)
    downgraded = await orch._invocation_error_reproduces_on_base(
        repo, None, "main", env_dependent=True)

    assert confident is True          # no setup needed → verdict stands
    assert downgraded is None         # setup-dependent → undeterminable


async def test_env_invocation_error_still_proceeds_without_evidence(
    bare_repo, tmp_path, store
):
    """The pre-existing behaviour holds when the base tree errors the same
    way — a broken conftest the task did not touch is not the coder's fault."""
    (bare_repo / "conftest.py").write_text("import nonexistent_dep_xyz  # noqa\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "break the env on base")

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )

    orch = _orch(store, tmp_path, FakeBackend(mutate))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # advisory as before: the change ships to its human gate, with the
    # invocation error on the record — not silently failed
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is not None
