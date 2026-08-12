"""Liveness diagnostics: the silences must be enumerable.

Every contradiction rule in doctor.py is a silent death the project really
had; these tests pin each one to a synthetic DB that reproduces it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.doctor import MECHANISMS, diagnose


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _ev(kind: str, **extra) -> dict:
    return {"source": "test", "kind": kind, "text": "", "ts": time.time(), **extra}


async def test_empty_db_reports_all_mechanisms_as_never_fired(store):
    d = await diagnose(store)
    assert len(d.mechanisms) == len(MECHANISMS)
    assert all(m["count"] == 0 and m["hint"] for m in d.mechanisms)
    assert d.healthy, "an empty install has nothing to contradict"


async def test_the_testing_dead_pattern_is_a_contradiction(store):
    """Reviews ran while tests never did — unnoticed for the system's life."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [_ev("review"), _ev("review")])
    d = await diagnose(store)
    assert any("TESTS NEVER RAN" in c for c in d.contradictions)
    assert not d.healthy


async def test_stale_eval_sandbox_is_an_advisory_not_a_contradiction(store):
    """0.4: a leaked eval sandbox is surfaced as an advisory — it must inform
    without failing the doctor gate (healthy stays True)."""
    import os
    import shutil
    import tempfile
    from pathlib import Path

    sandbox = Path(tempfile.gettempdir()) / f"nh-eval-doctortest-{os.getpid()}"
    sandbox.mkdir(exist_ok=True)
    old = time.time() - 3 * 3600  # older than the 2h staleness cutoff
    os.utime(sandbox, (old, old))
    try:
        d = await diagnose(store)
        assert any(str(sandbox) in a for a in d.advisories)
        assert not any(str(sandbox) in c for c in d.contradictions)
        assert d.healthy, "an advisory must never fail the doctor gate"
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


async def test_the_silent_watcher_pattern_is_a_contradiction(store):
    """A task parked at awaiting_approval with zero watcher events ever."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [_ev("pr_open"), _ev("review"), _ev("tests")])
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    d = await diagnose(store)
    assert any("WATCHER SILENT" in c for c in d.contradictions)
    # One fresh persisted watcher event clears it.
    await store.save_events(t.id, [_ev("pr_feedback_skipped", source="watcher")])
    d = await diagnose(store)
    assert not any("WATCHER" in c for c in d.contradictions)


async def test_stale_watcher_evidence_is_a_contradiction(store):
    """Heartbeats are hourly; a parked task whose newest watcher evidence is
    hours old means the watcher stopped ticking after it last acted."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("pr_open"), _ev("review"), _ev("tests"),
        {**_ev("wake_tick", source="watcher"), "ts": time.time() - 10 * 3600},
    ])
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    d = await diagnose(store)
    assert any("WATCHER STALE" in c for c in d.contradictions)


async def test_a_status_without_its_evidence_is_a_gap(store):
    """awaiting_approval with no pr_open event = a signal that lies."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    d = await diagnose(store)
    assert any(t.id[:8] in g and "pr_open" in g for g in d.evidence_gaps)
    await store.save_events(t.id, [_ev("pr_open")])
    d = await diagnose(store)
    assert not d.evidence_gaps


async def test_an_escalation_with_an_empty_blocker_is_a_gap(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.ESCALATED, validate=False)
    d = await diagnose(store)
    assert any("empty blocker" in g for g in d.evidence_gaps)
    t.blocker = {"question": "Spend more, or stop here?"}
    await store.update_task(t)
    d = await diagnose(store)
    assert not any("empty blocker" in g for g in d.evidence_gaps)


async def test_unreviewed_pr_is_a_contradiction(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [_ev("pr_open"), _ev("tests")])
    d = await diagnose(store)
    assert any("UNREVIEWED" in c for c in d.contradictions)


async def test_ci_gate_triggered_but_never_passed_on_a_done_task_contradicts(store):
    """M6: a done task whose CI_GATE integration run started and never went
    green is a verdict without its evidence."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("pr_open"), _ev("review"), _ev("tests"),
        _ev("ci_gate_trigger"),
    ])
    await store.set_status(t, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    d = await diagnose(store)
    assert any("CI_GATE UNPROVEN" in c for c in d.contradictions)
    # The pass event clears it.
    await store.save_events(t.id, [_ev("ci_gate_pass")])
    d = await diagnose(store)
    assert not any("CI_GATE UNPROVEN" in c for c in d.contradictions)


async def test_ci_gate_integration_is_an_enumerated_mechanism(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [_ev("ci_gate_trigger"), _ev("ci_gate_pass")])
    d = await diagnose(store)
    m = next(m for m in d.mechanisms if m["name"] == "ci_gate_integration")
    assert m["count"] == 2


async def test_spurious_budget_escalation_after_ci_gate_pass_contradicts(store):
    """The 2026-07-10 shape: validation passed, no new coder work, yet the
    task sits escalated BUDGET_EXHAUSTED — a resume fired on a non-human
    trigger."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("pr_open"), _ev("attempt_start"), _ev("ci_gate_pass"),
    ])
    t.blocker = {"category": "BUDGET_EXHAUSTED", "question": "raise?"}
    await store.update_task(t)
    await store.set_status(t, TaskStatus.ESCALATED, validate=False)
    d = await diagnose(store)
    assert any("SPURIOUS ESCALATION" in c for c in d.contradictions)
    # Real coder work AFTER the pass = a legitimate escalation — no flag.
    await store.save_events(t.id, [_ev("attempt_start")])
    d = await diagnose(store)
    assert not any("SPURIOUS ESCALATION" in c for c in d.contradictions)


async def test_orphaned_worktree_is_a_contradiction(store, tmp_path, monkeypatch):
    """W2.6: a crashed run's worktree lingers invisibly until the next acquire
    fails or the disk fills. A worktree whose task is KNOWN to this store but
    inactive (failed/done) is an orphan; one owned by a running task is not;
    one whose id is unknown to this store belongs to a different install and
    must NOT be flagged (that false positive broke the empty-DB doctor test)."""
    fake_home = tmp_path / ".no_human"
    (fake_home / "worktrees" / "deadbeef1234").mkdir(parents=True)
    monkeypatch.setattr("no_human.config.NO_HUMAN_HOME", fake_home)

    # Unknown to this store → NOT flagged (different install / isolated test).
    d = await diagnose(store)
    assert not any("ORPHANED WORKTREE" in c for c in d.contradictions)

    # A known but FAILED task with a lingering worktree → orphan.
    t = Task.new("crashed", repo_path="/tmp/x")
    t.id = "deadbeef1234"
    await store.create_task(t)
    await store.set_status(t, TaskStatus.FAILED, validate=False)
    d = await diagnose(store)
    assert any("ORPHANED WORKTREE" in c and "deadbeef1234" in c
               for c in d.contradictions)

    # The same worktree owned by an actively-implementing task: not an orphan.
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    d = await diagnose(store)
    assert not any("ORPHANED WORKTREE" in c for c in d.contradictions)


async def test_orphaned_worktree_is_found_under_the_per_run_name(
    store, tmp_path, monkeypatch,
):
    """Worktree directories are named `<task_id>.<owner_pid>.<token>` — one per
    RUN, so two overlapping attempts of a task cannot share a checkout. The
    orphan check has to READ that name: matching the directory against task ids
    whole would simply have stopped finding anything, with no test failing.

    It also gains a signal the per-task shape could not carry. An ACTIVE task
    can own a leftover — killed run, live run, both on disk — and a dead owner
    pid says which is which without guessing.
    """
    fake_home = tmp_path / ".no_human"
    wt = fake_home / "worktrees"
    dead_owner = wt / "deadbeef1234.4194303.a1b2c3d4"
    dead_owner.mkdir(parents=True)
    monkeypatch.setattr("no_human.config.NO_HUMAN_HOME", fake_home)

    t = Task.new("crashed mid-run", repo_path="/tmp/x")
    t.id = "deadbeef1234"
    await store.create_task(t)

    # ACTIVE task, but this directory's owner process is gone: still an orphan.
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    d = await diagnose(store)
    assert any("ORPHANED WORKTREE" in c and str(dead_owner) in c
               for c in d.contradictions), "the per-run name was not attributed"
    assert any("owner process 4194303 is gone" in c for c in d.contradictions)

    # The SAME task's live directory — owner alive — is not an orphan.
    import os

    live = wt / f"deadbeef1234.{os.getpid()}.b2c3d4e5"
    live.mkdir()
    d = await diagnose(store)
    assert not any(str(live) in c for c in d.contradictions)


async def test_done_code_review_needs_no_pr_open(store):
    """A standalone code-review finishes with cited comments, not a PR — 'done'
    without pr_open must NOT be flagged as an evidence gap for it (false positive
    that flagged f71107e9 every run). A done FEATURE task still must have one."""
    cr = Task.new("review PR 123", repo_path="/tmp/x")
    cr.kind = "code_review"
    await store.create_task(cr)
    await store.set_status(cr, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    d = await diagnose(store)
    assert not any(cr.id[:8] in g and "pr_open" in g for g in d.evidence_gaps)

    feat = Task.new("add feature", repo_path="/tmp/x")
    feat.kind = "feature"
    await store.create_task(feat)
    await store.set_status(feat, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    d = await diagnose(store)
    assert any(feat.id[:8] in g and "pr_open" in g for g in d.evidence_gaps)


async def test_doctor_flags_failed_attempts_with_empty_reason(tmp_path):
    """Historical rows (and any path that bypasses the store backstop) must
    surface as an evidence gap, not stay invisible."""
    from no_human.core.db import Store
    from no_human.core.task import Task
    from no_human.doctor import diagnose

    store = await Store(tmp_path / "d.db").connect()
    try:
        t = Task.new("x", repo_path="/tmp/r")
        await store.create_task(t)
        a = await store.create_attempt(t.id, 1)
        # bypass the backstop deliberately (simulates a historical row)
        await store.db.execute(
            "UPDATE attempts SET status='failed', failure_reason=NULL WHERE id=?",
            (a,))
        await store.db.commit()
        d = await diagnose(store)
        assert any("failure_reason" in g for g in d.evidence_gaps), d.evidence_gaps
    finally:
        await store.close()


async def test_doctor_accepts_report_only_design_doc_as_done(tmp_path):
    """design_doc joins PR_LESS_KINDS: done-without-PR is its success shape
    (this fix was silently dropped from PR #29 — pinned this time)."""
    from no_human.core.db import Store
    from no_human.core.task import Task, TaskStatus
    from no_human.doctor import diagnose

    store = await Store(tmp_path / "d.db").connect()
    try:
        t = Task.new("design doc", repo_path="/tmp/r", kind="design_doc")
        await store.create_task(t)
        await store.db.execute("UPDATE tasks SET status='done' WHERE id=?", (t.id,))
        await store.db.commit()
        d = await diagnose(store)
        assert not any(t.id[:8] in g for g in d.evidence_gaps), d.evidence_gaps
    finally:
        await store.close()


# --------------------------------------------------------------------------- #
# Configured-but-unusable CI. Not a history check: this failure mode leaves no #
# events at all, so no amount of event counting could ever have found it.      #
# --------------------------------------------------------------------------- #

async def test_ci_enabled_but_targetless_is_a_contradiction(store):
    d = await diagnose(store, {"ci": {"enabled": True, "backend": "gitlab",
                                      "project": ""}})
    assert any("CI BACKEND UNUSABLE" in c for c in d.contradictions)
    assert not d.healthy, "a gate the operator believes in but does not have"


@pytest.mark.parametrize("backend,key", [
    ("gitlab", "ci.project"),
    ("github_actions", "ci.repo"),
    ("jenkins", "ci.job"),
])
async def test_ci_contradiction_names_the_key_to_set(store, backend, key):
    """`nh doctor` is the surface a user checks when they suspect this, so the
    line has to end their search, not start it. It used to say "project/repo/job
    are all empty" for every backend — true, and it leaves the user to work out
    which one THEIR backend needs."""
    d = await diagnose(store, {"ci": {"enabled": True, "backend": backend}})
    assert d.contradictions
    assert any(key in c for c in d.contradictions), d.contradictions


async def test_ci_unknown_backend_is_a_contradiction(store):
    d = await diagnose(store, {"ci": {"enabled": True, "backend": "travis",
                                      "project": "g/r"}})
    # Asserts the MESSAGE, not the exception class name: `unknown ci.backend`
    # is what the operator reads, and CIMisconfigured (a ValueError) now
    # carries it. A diagnostic that leaked "ValueError" told them nothing.
    assert any("unknown ci.backend" in c for c in d.contradictions)
    assert any("travis" in c for c in d.contradictions)


async def test_working_ci_config_is_not_flagged(store):
    d = await diagnose(store, {"ci": {"enabled": True, "backend": "gitlab",
                                      "project": "g/r"}})
    assert not any("CI BACKEND" in c for c in d.contradictions)
    assert d.healthy


async def test_shipped_default_ci_config_is_silent(store):
    """Devil's advocate: doctor must stay green for an install that never
    configured CI. Read from DEFAULT_CONFIG — load_config() deep-merges the
    operator's own ~/.no_human/config.yaml, so asserting on a loaded config
    would prove something about this machine, not about the product.
    """
    from no_human.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["ci"]["enabled"] is False
    d = await diagnose(store, DEFAULT_CONFIG)
    assert not any("CI BACKEND" in c for c in d.contradictions)
    assert d.healthy


async def test_diagnose_without_config_is_unchanged(store):
    """26 existing callers pass only the store — they must keep working."""
    d = await diagnose(store)
    assert d.healthy


# --------------------------------------------------------------------------- #
# `nh doctor`'s EXIT CODE — the machine-readable half of the command.          #
#                                                                              #
# It used to be a constant 0: the command printed a red contradiction and told #
# its caller everything was fine, so `nh doctor || exit 1` in a CI job could   #
# never fire and every gate reporting through doctor was invisible to          #
# automation. These run the real CLI in a subprocess, because an in-process    #
# CliRunner would not exercise the process exit code at all — and would read   #
# the operator's REAL ~/.no_human, since NO_HUMAN_HOME is resolved at import.  #
# HOME and TMPDIR are therefore redirected per test.                           #
# --------------------------------------------------------------------------- #

DOCTOR_SRC = Path(__file__).resolve().parent.parent / "src"


def _run_doctor(home: Path, tmpdir: Path, *, config: str | None = None,
                token: bool = True,
                args: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    """Run `nh doctor` against an isolated HOME. Returns the completed process
    so the caller can assert on ``returncode`` directly."""
    (home / ".no_human").mkdir(parents=True, exist_ok=True)
    if config is not None:
        (home / ".no_human" / "config.yaml").write_text(config)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANTHROPIC_", "CLAUDE_", "AWS_"))}
    env.update({"HOME": str(home), "TMPDIR": str(tmpdir),
                "PYTHONPATH": str(DOCTOR_SRC), "NO_COLOR": "1",
                "COLUMNS": "200"})
    if token:
        # Presence-only probe (backend_check never makes a live auth call), so
        # a placeholder is enough and no real credential is ever read here.
        env["CLAUDE_CODE_OAUTH_TOKEN"] = "sk-ant-oat-not-a-real-token"
    return subprocess.run(
        [sys.executable, "-m", "no_human.cli.commands", "doctor", *args],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(tmpdir),
    )


def test_doctor_exits_nonzero_on_a_contradiction(tmp_path):
    """`nh doctor || exit 1` must actually fire. `ci.enabled: true` with no
    pipeline target is a contradiction the doctor already prints in red."""
    proc = _run_doctor(tmp_path / "home", _mktmp(tmp_path),
                       config="ci:\n  enabled: true\n  backend: gitlab\n")
    assert "CI BACKEND UNUSABLE" in proc.stdout, proc.stdout
    assert proc.returncode != 0, (
        "doctor printed a contradiction and reported success — the exit code "
        f"carries no information:\n{proc.stdout}"
    )
    assert proc.returncode == 1, f"expected 1, got {proc.returncode}"


def test_doctor_exits_zero_when_healthy(tmp_path):
    """The control that proves the fix is not just "always fail": a fresh
    install with a usable backend and no contradictions still exits 0."""
    proc = _run_doctor(tmp_path / "home", _mktmp(tmp_path))
    assert "no contradictions, no evidence gaps" in proc.stdout, proc.stdout
    assert proc.returncode == 0, (
        f"a healthy install must exit 0, got {proc.returncode}:\n{proc.stdout}"
    )


def test_doctor_advisory_alone_does_not_change_the_exit_code(tmp_path):
    """An advisory is informational — a leaked eval sandbox is a disk leak, not
    a broken gate. If advisories flipped the exit code, `nh doctor || exit 1`
    would fire on benign conditions, someone would delete it from their
    pipeline, and the check would protect nothing."""
    tmpdir = _mktmp(tmp_path)
    sandbox = tmpdir / "nh-eval-advisory-only"
    sandbox.mkdir()
    old = time.time() - 3 * 3600  # older than doctor's 2h staleness cutoff
    os.utime(sandbox, (old, old))

    proc = _run_doctor(tmp_path / "home", tmpdir)
    assert "LEAKED EVAL SANDBOX" in proc.stdout, (
        f"the advisory was not even reported:\n{proc.stdout}")
    assert "✗" not in proc.stdout, (  # the contradiction bullet
        f"this fixture must produce an advisory ONLY:\n{proc.stdout}")
    assert "no contradictions, no evidence gaps" in proc.stdout, proc.stdout
    assert proc.returncode == 0, (
        f"an advisory must never fail the doctor gate, got {proc.returncode}:"
        f"\n{proc.stdout}"
    )


def test_doctor_leads_with_a_three_line_verdict(tmp_path):
    """A first run printed 149 lines of all-zero mechanism rows and internal
    history before saying anything a newcomer could act on (walkthrough B6/Q11).
    The first three lines now answer: is it healthy, has anything run, how many
    tasks."""
    proc = _run_doctor(tmp_path / "home", _mktmp(tmp_path))
    head = [line.strip() for line in proc.stdout.splitlines()[:3]]

    assert head[0].startswith("install healthy"), proc.stdout
    assert head[1] == "nothing has run yet", proc.stdout
    assert head[2] == "0 task(s)", proc.stdout


def test_doctor_reports_the_auth_profile_and_mode(tmp_path):
    """quickstart.md promises `nh doctor` "reports your auth profile and mode".
    It loaded both values and printed neither — 149 lines with no occurrence of
    "auth" at all (walkthrough B5/Q4)."""
    proc = _run_doctor(tmp_path / "home", _mktmp(tmp_path),
                       config="llm:\n  auth_profile: enterprise\n"
                              "  auth_mode: subscription\n")
    auth = [ln for ln in proc.stdout.splitlines() if ln.startswith("auth")]

    assert auth, f"no auth line at all:\n{proc.stdout}"
    assert "enterprise" in auth[0], auth
    assert "subscription" in auth[0], auth
    # And it must not overclaim: the probe is presence-only by design
    # (backend_check never spends quota on a live call).
    assert "presence only" in auth[0], auth


def test_the_mechanism_table_is_behind_verbose_and_the_exit_code_is_not(tmp_path):
    """The mechanism table moves behind `--verbose`; the summary line, the
    `healthy` predicate and the exit code do not move at all.

    Row count comes from `MECHANISMS`, not a literal: registering a mechanism
    is a one-line change and used to fail HERE, in a rendering test that has
    nothing to say about it."""
    home, tmpdir = tmp_path / "home", _mktmp(tmp_path)
    quiet = _run_doctor(home, tmpdir)
    loud = _run_doctor(home, tmpdir, args=("--verbose",))

    # Default: the header survives (it is what `nh doctor` renders at all),
    # the per-mechanism rows do not.
    assert "mechanism liveness" in quiet.stdout, quiet.stdout
    assert "last: never" not in quiet.stdout, quiet.stdout
    assert "review_gate" not in quiet.stdout, quiet.stdout
    assert f"0/{len(MECHANISMS)} have ever fired" in quiet.stdout, quiet.stdout

    # --verbose: the whole table, exactly as it always rendered.
    assert "review_gate" in loud.stdout, loud.stdout
    assert loud.stdout.count("last: never") == len(MECHANISMS), loud.stdout
    assert len(loud.stdout.splitlines()) > len(quiet.stdout.splitlines())

    # Same verdict, same exit code either way.
    assert quiet.returncode == loud.returncode == 0, (quiet.stdout, loud.stdout)
    for out in (quiet.stdout, loud.stdout):
        assert "no contradictions, no evidence gaps" in out, out


async def test_a_dead_distiller_is_readable_from_the_three_distill_mechanisms(store):
    """A LIFETIME count cannot show a death: `context_distill` stood at 162 on
    2026-08-10 with its last firing on 2026-07-28, so doctor read "alive" for
    twelve days while the lever was dead, and the resulting `distill_* == 0`
    was misdiagnosed as lost spend.

    The other two kinds are what separate the causes. All three carry
    `last_ts`, so "distilled 162×, last a fortnight ago; skipped 73×, last
    today" is readable off one screen. Deliberately NOT a contradiction rule:
    keying one off lifetime counts would inherit the exact blindness above."""
    names = {n for n, _, _ in MECHANISMS}
    assert {"context_distill", "context_distill_skipped",
            "context_distill_failed"} <= names

    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("context_distill_skipped", chunks=3, largest=879, threshold=2000,
            reason="no_large_chunk"),
        _ev("context_distill_skipped", chunks=4, largest=782, threshold=2000,
            reason="no_large_chunk"),
    ])
    d = await diagnose(store)
    counts = {m["name"]: m["count"] for m in d.mechanisms}
    assert counts["context_distill_skipped"] == 2
    assert counts["context_distill"] == 0
    assert counts["context_distill_failed"] == 0
    # A skip is not a firing: it must never be swept into the liveness count.
    hints = {m["name"]: m["hint"] for m in d.mechanisms}
    assert hints["context_distill"]          # still flagged as never-fired
    assert not hints["context_distill_skipped"]
    # ...and neither is a health failure — doctor reports, the human decides.
    assert d.evidence_gaps == []


async def test_a_distiller_that_only_throws_is_not_read_as_never_consulted(store):
    """The state the skip kind alone could not see. A gather WITH an oversized
    chunk and a backend that raises emits neither a firing nor a skip, so both
    of those counts sit at zero while distillation is being consulted on every
    gather and failing on every call — and the zero-hint on the skip row said
    that shape meant "not being consulted at all". The failure kind is what
    makes that sentence true: its count is the only surviving evidence,
    because the exception is swallowed and the call never bills `distill_*`."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("context_distill_failed", error="RuntimeError", chars_before=4096),
    ])
    d = await diagnose(store)
    counts = {m["name"]: m["count"] for m in d.mechanisms}
    assert counts["context_distill_failed"] == 1
    assert counts["context_distill"] == counts["context_distill_skipped"] == 0
    hints = {m["name"]: m["hint"] for m in d.mechanisms}
    assert not hints["context_distill_failed"]   # non-zero: no zero-hint shown
    # The skip row's zero-hint may only claim "not consulted" for the case
    # where this row is zero too — the reading it now spells out.
    assert "both other context_distill_* rows at zero" in hints[
        "context_distill_skipped"]
    assert d.evidence_gaps == []


def _mktmp(tmp_path: Path) -> Path:
    """A private TMPDIR, so the machine's real /tmp leftovers cannot leak into
    (or out of) a test that asserts on advisories."""
    d = tmp_path / "tmp"
    d.mkdir(exist_ok=True)
    return d


async def test_the_intake_grill_passes_are_listed_mechanisms(store):
    """A claim in evaluator.py said `nh doctor` picked the grill's outcome
    events up "by kind for free". It did not — MECHANISMS is a hardcoded list
    and neither kind was in it, so the events were counted by nothing here and
    a dead grill stayed a dead grill silently. This test is what makes the
    sentence true: break the entry and the claim fails out loud.
    """
    names = {n for n, _, _ in MECHANISMS}
    assert {"grill_questions", "grill_answering"} <= names

    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("grill_questions", outcome="parsed_first_try"),
        _ev("grill_answering", outcome="parsed_first_try"),
        _ev("grill_answering", outcome="no_block_after_retry"),
    ])
    d = await diagnose(store)
    counts = {m["name"]: m["count"] for m in d.mechanisms}
    assert counts["grill_questions"] == 1
    assert counts["grill_answering"] == 2
    # A mechanism that HAS fired carries no zero-hint.
    assert all(not m["hint"] for m in d.mechanisms
               if m["name"].startswith("grill_"))


# --------------------------------------------------------------------------- #
# `nh doctor --verify-auth` — the one gap presence-checking cannot close       #
#                                                                              #
# A valid-SHAPED but expired or revoked credential passes every check in this  #
# file and dies at the first task (walkthrough B5). The live call is OPT-IN,   #
# because the rule that no diagnostic spends quota unasked is what makes the   #
# rest of doctor safe to run anywhere. The live call itself is mocked at its   #
# boundary in every test below: no credential, real or otherwise, is used.     #
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, is_error=False, final_text="ok"):
        self.is_error = is_error
        self.final_text = final_text


def _fake_backend(monkeypatch, *, result=None, raises=None, hang=False):
    """Replace ClaudeBackend.run — the boundary where quota would be spent."""
    import no_human.agent.claude_backend as cb_mod

    seen = {"constructed": 0, "kwargs": None}

    class _FakeBackend:
        def __init__(self, **kw):
            seen["constructed"] += 1
            seen["kwargs"] = kw

        async def run(self, prompt, **kw):
            import asyncio

            seen["prompt"] = prompt
            seen["run_kwargs"] = kw
            if raises is not None:
                raise raises
            if hang:
                await asyncio.sleep(30)
            return result or _FakeResult()

    monkeypatch.setattr(cb_mod, "ClaudeBackend", _FakeBackend)
    return seen


@pytest.fixture
def _no_auth_assertion(monkeypatch):
    """Neutralise the credential export so no test reads ~/.no_human/.env."""
    from no_human import config as config_mod

    monkeypatch.setattr(config_mod, "assert_subscription_mode",
                        lambda **kw: None)


async def test_verify_credential_live_returns_nothing_when_the_call_lands(
        monkeypatch, _no_auth_assertion):
    """The verdict is "an authenticated request succeeded", not "the answer was
    correct" — holding a diagnostic hostage to a model's phrasing would fail
    installs that work."""
    from no_human.agent.backend_check import verify_credential_live

    seen = _fake_backend(monkeypatch, result=_FakeResult(final_text="banana"))
    assert await verify_credential_live(model="claude-haiku-4-5") is None
    # …and it is the CHEAP shape: one turn, low effort, readonly.
    assert seen["run_kwargs"]["max_turns"] == 1
    assert seen["run_kwargs"]["effort"] == "low"
    assert seen["kwargs"]["readonly"] is True


async def test_verify_credential_live_reports_a_rejected_credential(
        monkeypatch, _no_auth_assertion):
    from no_human.agent.backend_check import verify_credential_live

    _fake_backend(monkeypatch, result=_FakeResult(
        is_error=True, final_text="API Error: 401 OAuth token is invalid"))
    problem = await verify_credential_live(model="claude-haiku-4-5")
    assert problem is not None
    assert problem[0] == "rejected"
    assert "401" in problem[1]


async def test_verify_credential_live_reports_a_crash_instead_of_raising(
        monkeypatch, _no_auth_assertion):
    """A diagnostic that dies with a traceback tells the operator less than one
    that says what happened."""
    from no_human.agent.backend_check import verify_credential_live

    _fake_backend(monkeypatch, raises=RuntimeError("CLI not found"))
    problem = await verify_credential_live(model="claude-haiku-4-5")
    assert problem == ("rejected", "RuntimeError: CLI not found")


async def test_a_slow_link_is_not_reported_as_a_dead_credential(
        monkeypatch, _no_auth_assertion):
    """Reporting a timeout as a rejected token would send the operator off to
    regenerate a credential that was fine."""
    from no_human.agent.backend_check import verify_credential_live

    _fake_backend(monkeypatch, hang=True)
    problem = await verify_credential_live(model="claude-haiku-4-5",
                                           timeout_s=0.05)
    assert problem is not None
    assert problem[0] == "inconclusive"
    assert "nothing about the credential" in problem[1]


async def test_a_dead_network_is_not_reported_as_a_dead_credential(
        monkeypatch, _no_auth_assertion):
    """The independent review demonstrated the first cut folding
    OSError("Network is unreachable") into CREDENTIAL DOES NOT WORK — a cron
    doctor on a flaky link must not send the operator to rotate a credential
    the API never even saw."""
    from no_human.agent.backend_check import verify_credential_live

    _fake_backend(monkeypatch, raises=OSError("Network is unreachable"))
    problem = await verify_credential_live(model="claude-haiku-4-5")
    assert problem is not None
    assert problem[0] == "inconclusive"
    assert "never reached the API" in problem[1]


async def test_verify_credential_live_never_calls_out_with_no_credential(
        monkeypatch):
    """An AuthError is the answer, not a reason to spend: the backend is never
    even constructed."""
    from no_human import config as config_mod
    from no_human.agent.backend_check import verify_credential_live

    def _boom(**kw):
        raise config_mod.AuthError("no ANTHROPIC_API_KEY was found")

    monkeypatch.setattr(config_mod, "assert_subscription_mode", _boom)
    seen = _fake_backend(monkeypatch)

    problem = await verify_credential_live(model="claude-haiku-4-5",
                                           auth_mode="api_key")
    assert problem == ("rejected", "no ANTHROPIC_API_KEY was found")
    assert seen["constructed"] == 0


def _doctor(monkeypatch, tmp_path, *args, live=None):
    """Run the `doctor` COMMAND (not the group) against a tmp DB.

    The command object directly, so the group callback's update notice never
    runs; `load_config` and `check_backend` are replaced so nothing here reads
    the operator's real ~/.no_human.
    """
    from click.testing import CliRunner

    import no_human.agent.backend_check as bc_mod
    from no_human.cli import commands as cmd_mod

    class _Cfg:
        data: dict = {}
        db_path = tmp_path / "doctor.db"
        utility_model = "claude-haiku-4-5"

        def get(self, key, default=None):
            return self.data.get(key, default)

    calls = []

    async def _live(**kw):
        calls.append(kw)
        return live

    monkeypatch.setattr(cmd_mod, "load_config", lambda *a, **k: _Cfg())
    monkeypatch.setattr(bc_mod, "check_backend", lambda **kw: bc_mod.BackendStatus(
        cli_path="/fake/claude", token_present=True))
    monkeypatch.setattr(bc_mod, "verify_credential_live", _live)
    result = CliRunner().invoke(cmd_mod.doctor, list(args))
    return result, calls


def test_doctor_makes_no_live_call_unless_asked(monkeypatch, tmp_path):
    """The default is byte-for-byte what it was: presence only, no spend."""
    result, calls = _doctor(monkeypatch, tmp_path,
                            live=("rejected", "would have failed"))

    assert result.exit_code == 0, result.output
    assert calls == [], "doctor spent quota without being asked"
    assert "presence only — no live auth call" in result.output, result.output


def test_verify_auth_says_so_on_the_auth_line_when_the_credential_works(
        monkeypatch, tmp_path):
    result, calls = _doctor(monkeypatch, tmp_path, "--verify-auth", live=None)

    assert result.exit_code == 0, result.output
    assert len(calls) == 1, "exactly one live call, or the flag is not cheap"
    assert calls[0]["model"] == "claude-haiku-4-5", calls
    assert "verified by one live call" in result.output, result.output
    assert "presence only" not in result.output, result.output


def test_a_transport_failure_does_not_fail_the_doctor_gate(
        monkeypatch, tmp_path):
    """The review's exact demonstration, inverted: a network failure must be
    reported as NOT VERIFIED — never as a dead credential, and never exit 1.
    An operator's cron on a flaky link must not rotate a working token."""
    result, calls = _doctor(
        monkeypatch, tmp_path, "--verify-auth",
        live=("inconclusive",
              "OSError: Network is unreachable — the request never reached "
              "the API, so this says nothing about the credential; try again"))

    assert len(calls) == 1
    assert result.exit_code == 0, (
        f"a transport failure must not fail the gate:\n{result.output}")
    flat = " ".join(result.output.split())  # Rich wraps lines mid-phrase
    assert "NOT VERIFIED (transport failure)" in flat, result.output
    assert "credential not verified" in flat, result.output
    assert "CREDENTIAL DOES NOT WORK" not in flat, result.output


def test_a_credential_the_live_call_rejects_fails_the_doctor_gate(
        monkeypatch, tmp_path):
    """The B5 gap closed: `nh doctor --verify-auth || exit 1` fires on an
    install whose token is present, well-shaped, and dead."""
    result, calls = _doctor(
        monkeypatch, tmp_path, "--verify-auth",
        live=("rejected", "API Error: 401 OAuth token is invalid"))

    assert len(calls) == 1
    assert "CREDENTIAL DOES NOT WORK" in result.output, result.output
    assert "401" in result.output, result.output
    assert "live call REJECTED" in result.output, result.output
    assert result.exit_code == 1, (
        f"a dead credential must fail the gate:\n{result.output}")
