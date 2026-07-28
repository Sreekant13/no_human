"""LeadAgent: decomposition, DAG validation, sub-task lifecycle, and parsing."""

import json

import pytest

from no_human.core.db import Store
from no_human.core.lead_agent import DecompositionError, LeadAgent
from no_human.core.orchestrator import TaskOutcome, _parse_decomposition
from no_human.core.task import Task, TaskStatus


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "t.db").connect()
    yield s
    await s.close()


# ------------------------------------------------------------------ #
# _parse_decomposition                                                #
# ------------------------------------------------------------------ #

def test_parse_decomposition_valid():
    plan = (
        "Some plan text...\n"
        "DECOMPOSE_PLAN_START\n"
        "```json\n"
        '{"decompose": true, "justification": "complex", '
        '"subtasks": [{"title": "A", "kind": "investigation"}]}\n'
        "```\n"
        "DECOMPOSE_PLAN_END\n"
    )
    result = _parse_decomposition(plan)
    assert result is not None
    assert result["decompose"] is True
    assert len(result["subtasks"]) == 1
    assert result["subtasks"][0]["title"] == "A"


def test_parse_decomposition_no_markers():
    assert _parse_decomposition("just a normal plan") is None


def test_parse_decomposition_invalid_json():
    plan = (
        "DECOMPOSE_PLAN_START\n"
        "```json\n"
        "{invalid json}\n"
        "```\n"
        "DECOMPOSE_PLAN_END\n"
    )
    assert _parse_decomposition(plan) is None


def test_parse_decomposition_empty_subtasks():
    plan = (
        "DECOMPOSE_PLAN_START\n"
        "```json\n"
        '{"decompose": true, "subtasks": []}\n'
        "```\n"
        "DECOMPOSE_PLAN_END\n"
    )
    assert _parse_decomposition(plan) is None


def test_parse_decomposition_no_decompose_flag():
    plan = (
        "DECOMPOSE_PLAN_START\n"
        "```json\n"
        '{"decompose": false, "subtasks": [{"title": "A"}]}\n'
        "```\n"
        "DECOMPOSE_PLAN_END\n"
    )
    assert _parse_decomposition(plan) is None


# ------------------------------------------------------------------ #
# DAG validation                                                      #
# ------------------------------------------------------------------ #

def test_dag_valid():
    specs = [
        {"title": "A", "depends_on": []},
        {"title": "B", "depends_on": ["A"]},
        {"title": "C", "depends_on": ["B"]},
    ]
    LeadAgent._validate_dag(specs)  # should not raise


def test_dag_cycle_detected():
    specs = [
        {"title": "A", "depends_on": ["C"]},
        {"title": "B", "depends_on": ["A"]},
        {"title": "C", "depends_on": ["B"]},
    ]
    with pytest.raises(DecompositionError, match="cycle"):
        LeadAgent._validate_dag(specs)


def test_dag_unknown_dependency():
    specs = [
        {"title": "A", "depends_on": ["Z"]},
    ]
    with pytest.raises(DecompositionError, match="unknown"):
        LeadAgent._validate_dag(specs)


def test_dag_self_cycle():
    specs = [
        {"title": "A", "depends_on": ["A"]},
    ]
    with pytest.raises(DecompositionError, match="cycle"):
        LeadAgent._validate_dag(specs)


def test_dag_rejects_multi_parent():
    """v1: multi-parent (diamond) dependencies are not supported."""
    specs = [
        {"title": "A", "depends_on": []},
        {"title": "B", "depends_on": []},
        {"title": "C", "depends_on": ["A", "B"]},
    ]
    with pytest.raises(DecompositionError, match="multi-parent"):
        LeadAgent._validate_dag(specs)


# ------------------------------------------------------------------ #
# decompose()                                                         #
# ------------------------------------------------------------------ #

async def test_decompose_creates_subtasks(store):
    parent = Task.new("upgrade everything", repo_path="/tmp/r")
    await store.create_task(parent)

    plan = {
        "decompose": True,
        "subtasks": [
            {"title": "Investigate", "kind": "investigation",
             "description": "find stuff", "acceptance_criteria": ["found it"]},
            {"title": "Implement", "kind": "feature",
             "description": "do stuff", "depends_on": ["Investigate"]},
        ],
    }
    lead = LeadAgent(store)
    created = await lead.decompose(parent, plan)

    assert len(created) == 2

    # First sub-task: no deps → PENDING.
    assert created[0].title == "Investigate"
    assert created[0].kind == "investigation"
    assert created[0].parent_id == parent.id
    assert created[0].status == TaskStatus.PENDING
    assert created[0].repo_path == parent.repo_path

    # Second sub-task: has deps → BLOCKED.
    assert created[1].title == "Implement"
    assert created[1].parent_id == parent.id
    assert created[1].status == TaskStatus.BLOCKED
    assert created[1].blocker is not None
    assert created[1].blocker["category"] == "DEPENDENCY_WAIT"

    # Verify DB persistence.
    subs = await store.list_subtasks(parent.id)
    assert len(subs) == 2


async def test_decompose_inherits_backend(store):
    parent = Task.new("big task", repo_path="/tmp/r")
    parent.config = {"backend": "agent-a"}
    await store.create_task(parent)

    plan = {
        "decompose": True,
        "subtasks": [{"title": "sub", "kind": "feature"}],
    }
    lead = LeadAgent(store)
    created = await lead.decompose(parent, plan)
    assert created[0].config.get("backend") == "agent-a"


async def test_decompose_rejects_too_many_subtasks(store):
    parent = Task.new("huge", repo_path="/tmp/r")
    await store.create_task(parent)

    plan = {
        "decompose": True,
        "subtasks": [{"title": f"sub-{i}", "kind": "feature"} for i in range(51)],
    }
    lead = LeadAgent(store)
    with pytest.raises(DecompositionError, match="max"):
        await lead.decompose(parent, plan)


async def test_decompose_rejects_empty(store):
    parent = Task.new("empty", repo_path="/tmp/r")
    await store.create_task(parent)

    lead = LeadAgent(store)
    with pytest.raises(DecompositionError, match="no subtasks"):
        await lead.decompose(parent, {"decompose": True, "subtasks": []})


async def test_decompose_rejects_missing_title(store):
    parent = Task.new("no title sub", repo_path="/tmp/r")
    await store.create_task(parent)

    plan = {
        "decompose": True,
        "subtasks": [{"kind": "feature", "description": "missing title"}],
    }
    lead = LeadAgent(store)
    with pytest.raises(DecompositionError, match="no title"):
        await lead.decompose(parent, plan)


# ------------------------------------------------------------------ #
# _unblock_ready()                                                    #
# ------------------------------------------------------------------ #

async def test_unblock_ready_moves_to_pending(store):
    parent = Task.new("parent", repo_path="/tmp/r")
    await store.create_task(parent)

    dep = Task.new("dep", repo_path="/tmp/r", parent_id=parent.id)
    dep.status = TaskStatus.DONE
    await store.create_task(dep)

    blocked = Task.new("blocked", repo_path="/tmp/r", parent_id=parent.id)
    blocked.status = TaskStatus.BLOCKED
    blocked.blocker = {"category": "DEPENDENCY_WAIT",
                       "wake_condition": "subtask_deps_met"}
    blocked.context = {"depends_on_ids": [dep.id]}
    await store.create_task(blocked)

    lead = LeadAgent(store)
    count = await lead._unblock_ready(parent.id)
    assert count == 1

    refreshed = await store.get_task(blocked.id)
    assert refreshed.status == TaskStatus.PENDING
    assert refreshed.blocker is None


async def test_unblock_ready_propagates_base_branch(store):
    """When dep A completes with pr_branch, dependent B gets it as base_branch."""
    parent = Task.new("parent", repo_path="/tmp/r")
    await store.create_task(parent)

    dep = Task.new("dep", repo_path="/tmp/r", parent_id=parent.id)
    dep.status = TaskStatus.DONE
    dep.context = {"pr_branch": "no-human/dep-branch-abc"}
    await store.create_task(dep)

    blocked = Task.new("blocked", repo_path="/tmp/r", parent_id=parent.id)
    blocked.status = TaskStatus.BLOCKED
    blocked.blocker = {"category": "DEPENDENCY_WAIT",
                       "wake_condition": "subtask_deps_met"}
    blocked.context = {"depends_on_ids": [dep.id]}
    await store.create_task(blocked)

    lead = LeadAgent(store)
    count = await lead._unblock_ready(parent.id)
    assert count == 1

    refreshed = await store.get_task(blocked.id)
    assert refreshed.status == TaskStatus.PENDING
    assert refreshed.context["base_branch"] == "no-human/dep-branch-abc"


async def test_unblock_ready_no_pr_branch_uses_default(store):
    """When dep finishes without pr_branch (e.g. investigation), no base_branch is set."""
    parent = Task.new("parent", repo_path="/tmp/r")
    await store.create_task(parent)

    dep = Task.new("dep", repo_path="/tmp/r", parent_id=parent.id)
    dep.status = TaskStatus.DONE
    dep.context = {}  # no pr_branch — investigation task
    await store.create_task(dep)

    blocked = Task.new("blocked", repo_path="/tmp/r", parent_id=parent.id)
    blocked.status = TaskStatus.BLOCKED
    blocked.blocker = {"category": "DEPENDENCY_WAIT",
                       "wake_condition": "subtask_deps_met"}
    blocked.context = {"depends_on_ids": [dep.id]}
    await store.create_task(blocked)

    lead = LeadAgent(store)
    count = await lead._unblock_ready(parent.id)
    assert count == 1

    refreshed = await store.get_task(blocked.id)
    assert refreshed.status == TaskStatus.PENDING
    assert "base_branch" not in refreshed.context


async def test_unblock_ready_keeps_blocked_when_deps_not_met(store):
    parent = Task.new("parent", repo_path="/tmp/r")
    await store.create_task(parent)

    dep = Task.new("dep", repo_path="/tmp/r", parent_id=parent.id)
    dep.status = TaskStatus.IMPLEMENTING
    await store.create_task(dep)

    blocked = Task.new("blocked", repo_path="/tmp/r", parent_id=parent.id)
    blocked.status = TaskStatus.BLOCKED
    blocked.blocker = {"category": "DEPENDENCY_WAIT",
                       "wake_condition": "subtask_deps_met"}
    blocked.context = {"depends_on_ids": [dep.id]}
    await store.create_task(blocked)

    lead = LeadAgent(store)
    count = await lead._unblock_ready(parent.id)
    assert count == 0

    refreshed = await store.get_task(blocked.id)
    assert refreshed.status == TaskStatus.BLOCKED


# ------------------------------------------------------------------ #
# parent_id round-trip                                                #
# ------------------------------------------------------------------ #

async def test_parent_id_roundtrip(store):
    parent = Task.new("parent", repo_path="/tmp/r")
    await store.create_task(parent)

    child = Task.new("child", repo_path="/tmp/r", parent_id=parent.id)
    await store.create_task(child)

    got = await store.get_task(child.id)
    assert got.parent_id == parent.id


async def test_list_subtasks(store):
    parent = Task.new("parent", repo_path="/tmp/r")
    other = Task.new("other", repo_path="/tmp/r")
    child1 = Task.new("c1", repo_path="/tmp/r", parent_id=parent.id)
    child2 = Task.new("c2", repo_path="/tmp/r", parent_id=parent.id)
    for t in [parent, other, child1, child2]:
        await store.create_task(t)

    subs = await store.list_subtasks(parent.id)
    assert {s.id for s in subs} == {child1.id, child2.id}

    count = await store.count_subtasks(parent.id)
    assert count == 2

    # Other task has no children.
    assert await store.count_subtasks(other.id) == 0


# ------------------------------------------------------------------ #
# park_parent()                                                       #
# ------------------------------------------------------------------ #

async def test_park_parent_sets_compound_parent_status(store):
    """park_parent() sets status to COMPOUND_PARENT and returns immediately."""
    parent = Task.new("compound", repo_path="/tmp/r")
    parent.status = TaskStatus.IMPLEMENTING
    await store.create_task(parent)

    lead = LeadAgent(store)
    outcome = await lead.park_parent(parent)

    assert outcome.status is TaskStatus.COMPOUND_PARENT
    refreshed = await store.get_task(parent.id)
    assert refreshed.status is TaskStatus.COMPOUND_PARENT


# ------------------------------------------------------------------ #
# check_completion()                                                   #
# ------------------------------------------------------------------ #

async def test_check_completion_still_in_progress(store):
    """check_completion returns False when sub-tasks are still running."""
    parent = Task.new("compound", repo_path="/tmp/r")
    parent.status = TaskStatus.COMPOUND_PARENT
    await store.create_task(parent)

    sub = Task.new("sub", repo_path="/tmp/r", parent_id=parent.id)
    sub.status = TaskStatus.IMPLEMENTING
    await store.create_task(sub)

    lead = LeadAgent(store)
    finished = await lead.check_completion(parent)
    assert finished is False

    refreshed = await store.get_task(parent.id)
    assert refreshed.status is TaskStatus.COMPOUND_PARENT


async def test_check_completion_retries_failed_subtask(store):
    """check_completion retries a failed sub-task once."""
    parent = Task.new("compound", repo_path="/tmp/r")
    parent.status = TaskStatus.COMPOUND_PARENT
    await store.create_task(parent)

    sub = Task.new("sub", repo_path="/tmp/r", parent_id=parent.id)
    sub.status = TaskStatus.FAILED
    await store.create_task(sub)

    events = []
    lead = LeadAgent(store, emit=lambda kind, text, **kw: events.append(kind))
    finished = await lead.check_completion(parent)

    assert finished is False
    assert "retry" in events
    refreshed_sub = await store.get_task(sub.id)
    assert refreshed_sub.status is TaskStatus.PENDING


async def test_check_completion_escalates_after_retry(store):
    """check_completion escalates if a sub-task fails after retry."""
    parent = Task.new("compound", repo_path="/tmp/r")
    parent.status = TaskStatus.COMPOUND_PARENT
    parent.context = {"retried_subtask_ids": []}
    await store.create_task(parent)

    sub = Task.new("sub", repo_path="/tmp/r", parent_id=parent.id)
    sub.status = TaskStatus.FAILED
    await store.create_task(sub)

    events = []
    lead = LeadAgent(store, emit=lambda kind, text, **kw: events.append(kind))

    # First call: retries
    await lead.check_completion(parent)
    # Simulate sub-task failing again
    await store.set_status(sub, TaskStatus.FAILED, validate=False)

    # Refresh parent from DB (retry updated context)
    parent = await store.get_task(parent.id)
    finished = await lead.check_completion(parent)

    assert finished is True
    assert "escalate" in events
    refreshed = await store.get_task(parent.id)
    assert refreshed.status is TaskStatus.ESCALATED


# ------------------------------------------------------------------ #
# Quality gate                                                        #
# ------------------------------------------------------------------ #

async def test_quality_gate_passes_when_tests_succeed(store, tmp_path):
    """Quality gate runs the repo's test command; on pass → DONE."""
    import subprocess
    repo = tmp_path / "qg_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "u@e"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "u"], check=True, capture_output=True)
    (repo / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

    from no_human.profile import ProjectProfile
    prof = ProjectProfile(
        repo_path=str(repo), ecosystem="custom",
        test_cmd="exit 0",
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    parent = Task.new("compound", repo_path=str(repo))
    await store.create_task(parent)

    sub = Task.new("sub", repo_path=str(repo), parent_id=parent.id)
    sub.status = TaskStatus.DONE
    await store.create_task(sub)

    events = []
    config = {"lead_agent": {"quality_gate": True, "poll_interval": 0.01}}
    lead = LeadAgent(store, config=config, emit=lambda kind, text, **kw: events.append(kind))
    await store.set_status(parent, TaskStatus.COMPOUND_PARENT, validate=False)
    finished = await lead.check_completion(parent)

    assert finished is True
    refreshed = await store.get_task(parent.id)
    assert refreshed.status is TaskStatus.DONE
    assert "quality_gate" in events


async def test_quality_gate_escalates_on_test_failure(store, tmp_path):
    """Quality gate escalates when integration tests fail."""
    import subprocess
    repo = tmp_path / "qg_repo_fail"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "u@e"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "u"], check=True, capture_output=True)
    (repo / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

    from no_human.profile import ProjectProfile
    prof = ProjectProfile(
        repo_path=str(repo), ecosystem="custom",
        test_cmd="echo 'FAIL: assertion error'; exit 1",
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    parent = Task.new("compound fail", repo_path=str(repo))
    await store.create_task(parent)

    sub = Task.new("sub", repo_path=str(repo), parent_id=parent.id)
    sub.status = TaskStatus.DONE
    await store.create_task(sub)

    events = []
    config = {"lead_agent": {"quality_gate": True, "poll_interval": 0.01}}
    lead = LeadAgent(store, config=config, emit=lambda kind, text, **kw: events.append(kind))
    await store.set_status(parent, TaskStatus.COMPOUND_PARENT, validate=False)
    finished = await lead.check_completion(parent)

    assert finished is True
    refreshed = await store.get_task(parent.id)
    assert refreshed.status is TaskStatus.ESCALATED
    assert "quality_gate_failed" in events


async def test_quality_gate_skipped_when_disabled(store):
    """Quality gate is off by default — all-done → DONE without tests."""
    parent = Task.new("compound default", repo_path="/tmp/r")
    await store.create_task(parent)

    sub = Task.new("sub", repo_path="/tmp/r", parent_id=parent.id)
    sub.status = TaskStatus.DONE
    await store.create_task(sub)

    events = []
    lead = LeadAgent(store, emit=lambda kind, text, **kw: events.append(kind))
    await store.set_status(parent, TaskStatus.COMPOUND_PARENT, validate=False)
    finished = await lead.check_completion(parent)

    assert finished is True
    refreshed = await store.get_task(parent.id)
    assert refreshed.status is TaskStatus.DONE
    assert "quality_gate" not in events


async def test_unblock_ready_clears_a_checkpoint_it_never_chose(store):
    """🔴 A MACHINE re-entry must not inherit another actor's provenance.

    `_unblock_ready` moves a sub-task BLOCKED→PENDING when its dependencies are
    done. That is the loop deciding, not a human. If a stale `resume_from`
    survives it, the zero-diff honesty gate reads that pair and — when it says
    `by: "human"` — believes a human gated this run. An attempt that edits
    nothing is then credited and a PR opens on work no attempt produced.

    This test exists because an architecture audit caught the fix for it
    shipping with a LABEL and no demonstrated RED: the new AST registry test
    pins the ENUMERATION of re-entry paths, not their behaviour, so
    `("core/lead_agent.py", "_unblock_ready"): CLEARS` passed whether or not the
    clearing line existed. An enumeration guard and a behaviour guard are not
    substitutes for each other.
    """
    parent = Task.new("parent", repo_path="/tmp/repo")
    await store.create_task(parent)
    dep = Task.new("dep", repo_path="/tmp/repo")
    dep.parent_id = parent.id
    await store.create_task(dep)
    await store.set_status(dep, TaskStatus.DONE, validate=False)

    blocked = Task.new("blocked on dep", repo_path="/tmp/repo")
    blocked.parent_id = parent.id
    # The real gate is the wake condition plus `depends_on_ids` in context —
    # NOT a `depends_on` attribute. My first version of this fixture set the
    # latter, `_unblock_ready` returned 0, and the test "failed" for a reason
    # that had nothing to do with the claim. Instrument, not product.
    blocked.blocker = {"category": "DEPENDENCY_WAIT",
                       "question": "waiting on dep",
                       "wake_condition": "subtask_deps_met"}
    await store.create_task(blocked)
    await store.set_status(blocked, TaskStatus.BLOCKED, validate=False)
    # Residue of an earlier human resume. Nothing clears it on its own.
    await store.merge_context(blocked.id, {
        "depends_on_ids": [dep.id],
        "resume_from": {"sha": "75c68e08", "branch": "old", "by": "human"}})

    lead = LeadAgent(store)
    unblocked = await lead._unblock_ready(parent.id)

    assert unblocked == 1, unblocked
    ctx = (await store.get_task(blocked.id)).context or {}
    assert ctx.get("resume_from") is None, (
        "a dependency-met unblock inherited a checkpoint no actor chose for it, "
        f"still labelled human: {ctx.get('resume_from')}")


async def test_check_completion_retry_clears_a_checkpoint_it_never_chose(store):
    """The TWIN of `_unblock_ready`, and it shipped unpinned.

    Audit 4 said two `LeadAgent` CLEARS fixes had a label and no demonstrated
    RED. Audit 5 checked BOTH and found only one had been closed: deleting
    `check_completion`'s clearing line still left 2826 tests green. So the fix
    for "a fix landed on one half of a pair" landed on one half of a pair —
    the fifth occurrence of that shape in this branch.

    The auto-retry of a FAILED sub-task is a MACHINE decision. A stale
    `by:"human"` surviving it tells the zero-diff honesty gate a human gated
    this run, and an attempt that edits nothing is credited.
    """
    parent = Task.new("parent", repo_path="/tmp/repo")
    await store.create_task(parent)
    await store.set_status(parent, TaskStatus.COMPOUND_PARENT, validate=False)

    sub = Task.new("failed sub-task", repo_path="/tmp/repo")
    sub.parent_id = parent.id
    await store.create_task(sub)
    await store.set_status(sub, TaskStatus.FAILED, validate=False)
    await store.merge_context(sub.id, {
        "resume_from": {"sha": "75c68e08", "branch": "old", "by": "human"}})

    lead = LeadAgent(store)
    await lead.check_completion(await store.get_task(parent.id))

    refreshed = await store.get_task(sub.id)
    assert refreshed.status is TaskStatus.PENDING, refreshed.status
    assert (refreshed.context or {}).get("resume_from") is None, (
        "the machine retry inherited a checkpoint no actor chose for it, still "
        f"labelled human: {(refreshed.context or {}).get('resume_from')}")
