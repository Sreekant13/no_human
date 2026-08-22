"""Liveness diagnostics: which guarded mechanisms have actually ever fired.

The system's worst historical bugs were not crashes but silences — TESTING
never ran for the system's entire life, the supervisor's guidance was dropped
on the floor, the wake watcher persisted nothing, distillation has never
fired once. A dead subsystem produces no error; it produces an absence. This
module makes the absences enumerable: every mechanism that should leave
evidence in ``task_events`` is listed with its lifetime firing count, and a
set of contradiction rules encodes the known silent-death patterns (evidence
of the *surrounding* activity without evidence of the mechanism itself).

Read-only by design; ``nh doctor`` renders the result.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core.db import Store
from .vcs.task_pr import (
    AWAITING_APPROVAL_EVIDENCE_KINDS,
    DONE_EVIDENCE_KINDS,
    PR_EVENT_KINDS,
)

# name → (evidence kinds, hint shown when the count is zero). The hint says
# whether zero is plausible or alarming — doctor reports, the human decides.
MECHANISMS: list[tuple[str, tuple[str, ...], str]] = [
    ("planning", ("planning",), "zero = planning disabled or no task got past intake"),
    ("moa_fanout", ("planning_moa",),
     "zero is normal when tasks stay below the MoA complexity gate"),
    ("supervisor", ("supervisor", "supervisor_decision"),
     "fires every N coder tool calls — zero alongside coder activity is a dead hook"),
    ("review_gate", ("review",),
     "zero while attempts complete = the gate is not being consulted (the M0 root cause)"),
    ("tests", ("tests",),
     "zero across attempts = TESTING is dead (it was, for the system's entire life)"),
    ("tamper_guard", ("tamper",), "zero = no diff ever tamper-checked"),
    # `total()` looks kinds up by EXACT key, so `tamper_adjudication` is not
    # swept into `tamper_guard` above — and would be counted by nothing at all
    # without this row, which is precisely the miss the grill comment below
    # records. Zero here is the healthy state: it means the guard never fired.
    ("tamper_adjudication", ("tamper_adjudication",),
     "zero is good — the test-tampering guard never fired. Non-zero alongside "
     "delivered PRs means fires are being WAIVED as ticket-required; the "
     "justification is on each PR and is worth spot-checking"),
    # Read these THREE TOGETHER, and read their `last_ts`, not just their
    # counts. The first hint used to say distillation "has never fired to
    # date"; by 2026-08-10 it had fired 162 times, the last on 2026-07-28, and
    # a LIFETIME count of 162 reported a mechanism that had been dead for
    # twelve days as alive. `_distill_large_chunks` emits exactly one of these
    # three per gather with nothing to weigh and one per chunk it does weigh,
    # so all three at zero means, and only means, that distillation is never
    # consulted — no single one of them carries that reading on its own.
    ("context_distill", ("context_distill",),
     "zero = no chunk has ever been distilled; the other two context_distill_* "
     "rows say whether the lever was even reachable"),
    ("context_distill_skipped", ("context_distill_skipped",),
     "zero here WITH both other context_distill_* rows at zero = distillation "
     "is not being consulted at all; zero here with either of them non-zero "
     "means it is consulted and every chunk it weighed either distilled or "
     "threw. Non-zero with a STALE context_distill last_ts is the live shape: "
     "consulted and never firing — each event carries a `reason` "
     "(no_large_chunk: nothing reached `_CHUNK_DISTILL_THRESHOLD`, with the "
     "largest chunk it saw; no_gain: the summary came back no smaller) so the "
     "gap is readable, not guessed"),
    ("context_distill_failed", ("context_distill_failed",),
     "zero is good — no distillation attempt has ever raised. Non-zero means "
     "the lever is being consulted and throwing (quota exhausted, credentials "
     "scrubbed, backend unavailable); the failures are swallowed by design so "
     "a gather never breaks, which makes this count the only evidence they "
     "happened — `distill_*` stays 0 because the call never billed"),
    # Retry-cost class (attempt N>1 distilled state doc, replacing repo-map +
    # context-digest re-accumulation). Read these three together exactly like
    # the context_distill_* trio above, for the same reason: attempt 1 emits
    # NONE of them (the early return in `_distill_attempt_state`), so a task
    # that never reached a second attempt reads identically to a dead lever —
    # check `attempt_start`/`attempt_failed` counts alongside these before
    # reading a zero as broken.
    ("attempt_distill", ("attempt_distill",),
     "zero = no attempt has been distilled yet — expected if no task has "
     "reached attempt 2, or the config kill switch is off (see "
     "attempt_distill_skipped's reason)"),
    ("attempt_distill_skipped", ("attempt_distill_skipped",),
     "reason=disabled means context.attempt_state_distill_enabled is false; "
     "reason=stale_cleared means a resumed attempt 1 (nh reply/requeue) "
     "dropped a prior run's distilled_state doc — expected and healthy, not "
     "a defect. Attempt 1 with no stale doc to clear emits nothing at all. "
     "Zero here WITH attempt_distill also at zero, alongside attempts that "
     "did reach a retry, means the seam is never being called at all"),
    ("attempt_distill_failed", ("attempt_distill_failed",),
     "zero is good — no distillation attempt has ever raised. Non-zero means "
     "the utility-tier call is failing (quota exhausted, credentials "
     "scrubbed, backend unavailable) and every one of those attempts fell "
     "back to full re-accumulation — check the ERROR-level log line "
     "alongside this event for the exception"),
    ("lifetime_budget", ("lifetime_budget",),
     "zero = no task ever hit its lifetime caps (good), or the gate is dead"),
    ("stuck_detection", ("stuck",), "zero = no attempt ever looped (or detector dead)"),
    ("pr_open", ("pr_open",), "zero = no task has ever reached a PR"),
    ("pr_open_retry", ("pr_open_retry",), "zero is good — no transient forge failures"),
    ("advisory_degradations", ("advisory",),
     "zero is good — no subsystem silently degraded mid-run"),
    ("citation_rule", ("review_citation_demoted",),
     "zero is good — no hallucinated citation tried to block the gate"),
    ("repro_gate", ("repro_gate",),
     "zero with attempts reaching review = the gate is off or dead; "
     "high waived-share means coders aren't writing manifests"),
    ("pr_watch_ladder",
     ("merged", "pr_closed", "pr_feedback", "pr_feedback_skipped",
      "pr_feedback_deferred", "pr_ci_red",
      "pr_ci_infra", "pr_ci_advisory",
      "escalated_ci", "escalated_revisions", "escalated_timeout", "resumed"),
     "zero = the watcher never had to act (fine if pr_watch_heartbeat is alive)"),
    ("pr_watch_heartbeat", ("wake_tick",),
     "zero while tasks sit parked = the watcher is silent or dead "
     "(it was, until 2026-07-10 — events before then were never persisted)"),
    ("ci_gate_integration", ("ci_gate_trigger", "ci_gate_pass", "ci_gate_fail"),
     "zero = the post-PR CI_GATE gate never ran — fine while ci_gate.enabled "
     "is off, dead if a governed PR sat green without a run"),
    # The intake grill's two LLM passes. Added 2026-08-07 to make a claim
    # TRUE: the commit that instrumented the answering pass said `nh doctor`
    # "picks it up by kind for free", and it did not — this list is hardcoded
    # and neither kind was in it, so the new events were counted by nothing
    # here. Their zero-hints are the point of the instrumentation: the grill
    # runs on EVERY task (operator directive 2026-07-17), so zero of either
    # while tasks have run is not "plausible", it is the pass being dead.
    ("grill_questions", ("grill_questions",),
     "zero while tasks have run = the intake scoping question pass never "
     "reported — it is dead, or running uninstrumented (it was, until "
     "2026-08-07: an unparseable block emitted no event at all)"),
    ("grill_answering", ("grill_answering",),
     "zero while the question pass fired = the answering pass never ran; see "
     "the answering-pass keys under /api/metrics for whether the passes that "
     "DID run actually applied any answers"),
]

# A parked task whose newest watcher evidence is older than this is unshepherded.
WATCHER_STALE_SECONDS = 2 * 3600.0

# Statuses that assert something happened → the event kinds that must exist to
# back the claim. A status without its evidence is a signal that lies. The
# SAME objects `task_pr`/`restore-approval` use — one predicate for "does
# this task have a PR / a legitimate completion", not three.
REQUIRED_EVIDENCE: dict[str, frozenset[str]] = {
    "awaiting_approval": AWAITING_APPROVAL_EVIDENCE_KINDS,
    "done": DONE_EVIDENCE_KINDS,
}

# Task kinds that legitimately finish WITHOUT opening a PR — a standalone code
# review produces cited comments, an investigation produces findings. Demanding
# a pr_open of them is a false positive (it flagged the done code_review task
# f71107e9 every run). Only applies to the pr_open requirement.
PR_LESS_KINDS: tuple[str, ...] = ("code_review", "investigation", "design_doc")


def ci_config_problems(config: dict[str, Any] | None) -> list[str]:
    """The global ``ci:`` block is switched ON but cannot produce a backend.

    Static config check rather than a history check: the failure mode leaves no
    events at all, so counting events could never find it. For a long time the
    global block was documented in ``config.py`` and ``docs/configuration.md``
    and read by nothing — a user who configured it exactly as documented got no
    gate, no warning and no diagnostic.

    Reported as a contradiction, not an advisory, on the same reasoning as the
    ``CODING BACKEND UNUSABLE`` live probe: ``ci.enabled: true`` is an explicit
    request, and a gate the operator believes in but does not have is worse
    than no gate at all. Returns [] when CI is off — which is the
    ``DEFAULT_CONFIG`` state, so an install that never configured CI is silent.
    """
    ci_conf = (config or {}).get("ci") or {}
    if not ci_conf.get("enabled"):
        return []
    from .ci import CIMisconfigured, ci_from_config
    try:
        # `ci.enabled` is truthy above and that is the ONLY reason
        # `ci_from_config` returns None, so returning at all means a backend
        # was built. (A `why = "project/repo/job are all empty"` fallback used
        # to live here for the None case; it is unreachable, and it was the
        # backend-agnostic string the per-key message replaced.)
        ci_from_config({"ci": ci_conf})
        return []
    except CIMisconfigured as exc:
        # The one exception this function exists to report: pass its message
        # through verbatim rather than wrapping it in a class name. It already
        # names the exact key to set, which is the difference between a
        # diagnostic a user can act on and one they have to research.
        why = str(exc)
    except Exception as exc:  # noqa: BLE001 — a bad block must not kill doctor
        why = f"{type(exc).__name__}: {exc}"
    return [
        f"CI BACKEND UNUSABLE: config `ci.enabled` is true "
        f"(backend={ci_conf.get('backend', 'gitlab')!r}) but no backend can be "
        f"built — {why}. Tasks run with NO CI gate."
    ]


def _running_checkout(start: Path | None = None) -> Path | None:
    """The checkout the current process is running from: git's own toplevel
    for ``start`` (or the cwd), else the nearest ancestor holding
    ``pyproject.toml``. ``None`` when neither exists.

    ``git rev-parse --show-toplevel`` already resolves subdirectories
    correctly (it walks up from ``cwd`` internally) — this must NOT
    special-case "primary checkout" vs. "linked worktree": every worktree
    is a legitimate place to run ``nh`` from, and reporting a linked
    worktree's own toplevel as "wrong" is exactly the false positive a
    prior version of this check produced.
    """
    try:
        start = (start or Path.cwd()).resolve()
    except OSError:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def _repo_worktree_roots(start: Path) -> list[Path]:
    """Every worktree toplevel sharing ``start``'s repository (primary and
    linked). A shared ``.venv`` can hold an editable install that resolves
    into *any one* of these — e.g. a linked worktree's venv installed from
    the primary checkout — and that is healthy, not dangling. Only a
    package dir outside every one of them is a real problem. Returns []
    (never raises) when ``start`` is not a git checkout or the command
    fails.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=start, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    roots: list[Path] = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            try:
                roots.append(Path(line[len("worktree "):]).resolve())
            except (OSError, ValueError):
                continue
    return roots


def _package_dir_from_spec(spec: Any) -> Path | None:
    """The directory ``import no_human`` actually resolves to, or ``None``
    when ``spec.origin`` is missing/``None``/not a usable string — the
    dangling-.pth shape (a namespace package has no ``__file__``).
    """
    origin = getattr(spec, "origin", None)
    if not origin or not isinstance(origin, str):
        return None
    return Path(origin).resolve().parent


def _dangling_install_warning(checkout: Path, pkg: Path | None) -> str:
    where = str(pkg) if pkg is not None else "<namespace package: no __file__>"
    return (
        f"editable install points outside this checkout: `import no_human` "
        f"resolves to {where} but you are running from {checkout}. A "
        f"garbage-collected worktree leaves a dangling "
        f"_editable_impl_no_human.pth. Repair: run `uv pip install -e .` "
        f"from {checkout}."
    )


def editable_install_problem(
    *, spec: Any | None = None, checkout: Path | None = None
) -> str | None:
    """Warn when ``import no_human`` does not resolve inside the checkout
    the process is running from — the dangling-``.pth`` symptom left behind
    when a coder worktree that a shared venv was editable-installed against
    gets garbage-collected. Read-only, idempotent, and NEVER raises: a
    broken probe must never block startup, so any failure here is silence,
    not a guess. Returns a one-line warning string, or ``None`` when there
    is nothing to report (including when there isn't enough signal to be
    sure — a false silence is far cheaper here than a false alarm).
    """
    try:
        if getattr(sys, "frozen", False):
            # A PyInstaller/DMG binary has no checkout and no editable
            # install to dangle; the warning would be pure noise there.
            return None

        resolved_checkout = checkout if checkout is not None else _running_checkout()
        if resolved_checkout is None:
            return None
        resolved_checkout = Path(resolved_checkout).resolve()

        # False-positive guard: only speak when the cwd checkout actually
        # IS a no_human source tree (running `nh` from an unrelated
        # project, or a plain wheel install, must stay silent).
        if not (resolved_checkout / "src" / "no_human" / "__init__.py").is_file():
            return None

        resolved_spec = (
            spec if spec is not None else importlib.util.find_spec("no_human")
        )
        if resolved_spec is None:
            return _dangling_install_warning(resolved_checkout, None)

        origin = getattr(resolved_spec, "origin", None)
        pkg = _package_dir_from_spec(resolved_spec)
        if pkg is None:
            if origin is None:
                return _dangling_install_warning(resolved_checkout, None)
            # origin exists but is not a usable string — too malformed to
            # trust; stay silent rather than report a guess.
            return None

        if pkg == resolved_checkout or resolved_checkout in pkg.parents:
            return None
        for root in _repo_worktree_roots(resolved_checkout):
            if pkg == root or root in pkg.parents:
                return None
        return _dangling_install_warning(resolved_checkout, pkg)
    except Exception:  # noqa: BLE001 — a diagnostic must never break startup
        return None


@dataclass
class Diagnosis:
    mechanisms: list[dict[str, Any]] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    evidence_gaps: list[str] = field(default_factory=list)
    # Non-blocking notices (resource leaks, prunable leftovers). Deliberately
    # NOT part of `healthy` — an advisory must never fail the doctor gate.
    advisories: list[str] = field(default_factory=list)
    # The codex coding-backend readiness row (CLI presence, flag compatibility,
    # OPENAI_API_KEY presence). None-shaped ({"selected": False}, []) unless the
    # codex backend is actually in play — see `codex_readiness`.
    codex: dict[str, Any] | None = None

    @property
    def healthy(self) -> bool:
        return not self.contradictions and not self.evidence_gaps


async def _kind_stats(store: Store) -> dict[str, tuple[int, float]]:
    """kind → (count, last_ts) over all persisted task events."""
    rows = await store.query(
        "SELECT json_extract(data, '$.kind') AS kind, COUNT(*), MAX(ts) "
        "FROM task_events GROUP BY kind"
    )
    return {row[0]: (row[1], row[2] or 0.0) for row in rows if row[0]}


def codex_readiness(
    config: dict[str, Any] | None, *, requested_by_task: bool
) -> tuple[dict[str, Any], list[str]]:
    """Codex coding-backend readiness: CLI presence, flag compatibility, key.

    Returns ``({"selected": False}, [])`` — spawning nothing — unless the codex
    backend is actually in play: selected globally (``worker.backend``) or
    requested by at least one live task's ``config.backend`` override. When it
    IS in play, the subprocess probes this makes (``codex exec --help``,
    ``codex exec resume --help``, ``codex --version``) are the same read-only,
    per-process-cached probes ``CodexBackend._command`` itself uses
    (``codex_backend._HELP_CACHE`` / ``_VERSION_CACHE``) to choose the approval
    flag — this never spawns a session and never bills a call. BOTH the
    non-resume and the resume launch shape are checked, because
    ``codex exec resume`` has its own, narrower flag surface (no ``--cd``, no
    ``--sandbox`` — verified live) and a CLI that accepts one argv can still
    reject the other. Model ENTITLEMENT (whether ``llm.codex_model`` is
    actually callable on ``/v1/responses`` for this key) is a different
    question that costs a billed request to answer and is never asked here —
    the row always carries a note saying so.

    Wrapped by the caller in ``try/except Exception`` — a diagnostic must never
    break ``nh doctor``.
    """
    from .agent.backend import BackendUnavailable, resolve_backend_name

    config = config or {}
    selected = resolve_backend_name(config, role="coder") == "codex"
    if not (selected or requested_by_task):
        return {"selected": False}, []

    from .agent.codex_backend import (
        DEFAULT_CODEX_MODEL,
        CodexBackend,
        approval_args,
        codex_exec_help,
        codex_version,
        emitted_flags,
        find_codex_cli,
    )
    from .config import CODEX_API_KEY_VAR, credential_status

    llm = config.get("llm") or {}
    model = llm.get("codex_model", DEFAULT_CODEX_MODEL)
    cli = find_codex_cli(llm.get("codex_cli_path"))
    contradictions: list[str] = []
    row: dict[str, Any] = {
        "selected": True,
        "cli_path": cli,
        "version": None,
        "flags_ok": False,
        "flag_detail": None,
        "api_key_present": False,
        "entitlement_note": (
            f"model entitlement for llm.codex_model={model!r} cannot be "
            "checked without a billed call to /v1/responses — a listing in "
            "`GET /v1/models` is not entitlement."
        ),
    }
    if cli is None:
        contradictions.append(
            "CODEX BACKEND UNUSABLE: the 'codex' CLI is not on PATH — "
            "npm install -g @openai/codex"
        )
    else:
        version = codex_version(cli)
        help_text = codex_exec_help(cli)
        resume_help_text = codex_exec_help(cli, resume=True)
        row["version"] = version
        try:
            args = approval_args(help_text, version)
            row["flag_detail"] = " ".join(args)
            backend = CodexBackend(cli_path=cli)
            cmd = backend._command(Path.cwd(), effort=None, resume=None)
            resume_cmd = backend._command(
                Path.cwd(), effort=None, resume="doctor-probe-thread"
            )
            missing = [f for f in emitted_flags(cmd)
                      if help_text and f not in help_text]
            # `resume:`-prefixed so a report never conflates a non-resume flag
            # gap with a resume-only one — they point at different fixes.
            missing += [
                f"resume:{f}" for f in emitted_flags(resume_cmd)
                if resume_help_text and f not in resume_help_text
            ]
            if missing:
                row["flag_detail"] = f"UNSUPPORTED: {', '.join(missing)}"
                contradictions.append(
                    f"CODEX CLI INCOMPATIBLE: codex-cli {version} rejects "
                    f"{', '.join(missing)} — the installed CLI does not "
                    "accept every flag this build emits. Update codex or "
                    "check llm.codex_cli_path."
                )
            else:
                row["flags_ok"] = True
        except BackendUnavailable as exc:
            row["flag_detail"] = "UNSUPPORTED"
            contradictions.append(
                f"CODEX CLI INCOMPATIBLE: codex-cli {version} accepts none "
                f"of the non-interactive approval modes this build knows — "
                f"{exc}"
            )

    # Presence only — the value is never read into the row, a contradiction,
    # or a log line.
    row["api_key_present"] = credential_status([CODEX_API_KEY_VAR]).get(
        CODEX_API_KEY_VAR, False
    )
    if not row["api_key_present"]:
        contradictions.append(
            "CODEX BACKEND UNUSABLE: no OPENAI_API_KEY on file — codex is "
            "BYO-API-key only; expected in ~/.no_human/.env (chmod 600) or "
            "the environment."
        )
    return row, contradictions


async def diagnose(store: Store, config: dict[str, Any] | None = None) -> Diagnosis:
    d = Diagnosis()
    d.contradictions.extend(ci_config_problems(config))

    # Codex coding-backend readiness — wrapped so a diagnostic can never break
    # `nh doctor` (this file's own rule, applied consistently below to every
    # optional check: an older schema or an unresolvable CLI must not raise).
    try:
        requested_by_task = (await store.query_one(
            "SELECT COUNT(*) FROM tasks WHERE status NOT IN "
            "('done', 'failed', 'cancelled') AND LOWER(TRIM(COALESCE("
            "json_extract(config, '$.backend'), ''))) = 'codex'"
        ))[0] > 0
        d.codex, codex_contradictions = codex_readiness(
            config, requested_by_task=requested_by_task
        )
        d.contradictions.extend(codex_contradictions)
    except Exception as exc:  # noqa: BLE001 — must never CRASH `nh doctor`,
        # but a crash while codex is actually the selected backend must never
        # read back as "codex isn't in play" either — that was the exact
        # silent-failure a prior review flagged here: blanking to
        # `{"selected": False}` with nothing logged and no advisory, so a
        # broken readiness probe or store query read as healthy.
        # `resolve_backend_name` is pure config (no subprocess, no DB), so it
        # can still answer "is codex selected" even though the probe or the
        # query above just blew up — and it is itself guarded so a second
        # failure here still can't crash doctor.
        from .agent.backend import resolve_backend_name

        try:
            selected = resolve_backend_name(config or {}, role="coder") == "codex"
        except Exception:  # noqa: BLE001 — see above; never let this crash either
            selected = False
        note = f"codex readiness check raised {exc.__class__.__name__}: {exc}"
        d.codex = {"selected": selected, "error": str(exc)}
        if selected:
            d.contradictions.append(
                "CODEX READINESS CHECK FAILED: "
                f"{note} — codex is the selected coding backend but nh doctor "
                "could not verify it; treat it as unusable until this is "
                "resolved."
            )
        else:
            d.advisories.append(note)

    stats = await _kind_stats(store)

    def total(kinds: tuple[str, ...]) -> tuple[int, float]:
        count = sum(stats.get(k, (0, 0.0))[0] for k in kinds)
        last = max((stats.get(k, (0, 0.0))[1] for k in kinds), default=0.0)
        return count, last

    for name, kinds, hint in MECHANISMS:
        count, last = total(kinds)
        d.mechanisms.append(
            {"name": name, "count": count, "last_ts": last,
             "hint": hint if count == 0 else ""}
        )

    counts = {m["name"]: m["count"] for m in d.mechanisms}

    # Contradiction rules — each one is a silent death the project has really
    # had. Evidence of surrounding activity without evidence of the mechanism.
    coder_activity, _ = total(("tool_use",))
    if counts["tests"] == 0 and counts["review_gate"] > 0:
        d.contradictions.append(
            f"TESTS NEVER RAN while the review gate fired {counts['review_gate']}× "
            "— the exact failure that went unnoticed for the system's entire life."
        )
    if counts["supervisor"] == 0 and coder_activity > 50:
        d.contradictions.append(
            f"SUPERVISOR SILENT across {coder_activity} coder tool calls — the "
            "every-N-calls hook is not firing."
        )
    if counts["review_gate"] == 0 and counts["pr_open"] > 0:
        d.contradictions.append(
            f"UNREVIEWED PRs: {counts['pr_open']} pr_open event(s) with zero "
            "review events — the gate was bypassed."
        )
    parked = (await store.query_one(
        "SELECT COUNT(*) FROM tasks WHERE status = 'awaiting_approval'"
    ))[0]
    if parked > 0:
        # A healthy watcher leaves either actions or heartbeats. Neither, or
        # both stale, means nothing is shepherding the parked PRs right now.
        _, last_action = total(
            next(k for n, k, _ in MECHANISMS if n == "pr_watch_ladder"))
        _, last_beat = total(("wake_tick",))
        newest = max(last_action, last_beat)
        if newest == 0.0:
            d.contradictions.append(
                f"WATCHER SILENT: {parked} task(s) parked at awaiting_approval "
                "with zero persisted watcher events — nothing is shepherding "
                "their PRs."
            )
        elif time.time() - newest > WATCHER_STALE_SECONDS:
            age_h = (time.time() - newest) / 3600
            d.contradictions.append(
                f"WATCHER STALE: {parked} task(s) parked but the newest watcher "
                f"evidence is {age_h:.1f}h old (heartbeat is hourly)."
            )

    # A CI_GATE validation that STARTED must have finished green before the
    # task may claim done — a done task whose integration run never passed is
    # a verdict without its evidence (the M6 contradiction).
    rows = await store.query(
        """SELECT t.id FROM tasks t WHERE t.status = 'done' AND EXISTS (
              SELECT 1 FROM task_events e WHERE e.task_id = t.id
              AND json_extract(e.data, '$.kind') = 'ci_gate_trigger')
           AND NOT EXISTS (
              SELECT 1 FROM task_events e WHERE e.task_id = t.id
              AND json_extract(e.data, '$.kind') = 'ci_gate_pass')""")
    for (task_id,) in rows:
        d.contradictions.append(
            f"CI_GATE UNPROVEN: task {task_id[:8]} is 'done' but its CI_GATE "
            "integration run was triggered and never passed."
        )

    # A task that escalated BUDGET_EXHAUSTED after its integration validation
    # passed, with no new coder work in between, was almost certainly resumed
    # by something that wasn't human feedback (the 2026-07-10 self-comment
    # incident) — the spend was already capped, so the escalation is noise
    # pointing at a resume-trigger bug, not at the budget.
    #
    # BOTH terminal statuses, since 2026-08-09: `budget.exhaustion_terminal`
    # (default on) routes BUDGET_EXHAUSTED to `failed` instead of `escalated`.
    # Pinned to 'escalated' alone, this detector would have gone quietly blind
    # on the default configuration — the diagnostic still needs to see the
    # shape, and the shape is the blocker category, not the status it parked in.
    rows = await store.query(
        """SELECT t.id FROM tasks t
           WHERE t.status IN ('escalated', 'failed')
             AND json_extract(t.blocker, '$.category') = 'BUDGET_EXHAUSTED'
             AND EXISTS (
               SELECT 1 FROM task_events e WHERE e.task_id = t.id
               AND json_extract(e.data, '$.kind') = 'ci_gate_pass')
             AND NOT EXISTS (
               SELECT 1 FROM task_events e2 WHERE e2.task_id = t.id
               AND json_extract(e2.data, '$.kind') = 'attempt_start'
               AND e2.ts > (SELECT MAX(e3.ts) FROM task_events e3
                            WHERE e3.task_id = t.id
                            AND json_extract(e3.data, '$.kind') = 'ci_gate_pass'))""")
    for (task_id,) in rows:
        d.contradictions.append(
            f"SPURIOUS ESCALATION: task {task_id[:8]} stopped on "
            "BUDGET_EXHAUSTED after its integration validation passed with no "
            "new coder work — a resume fired on a non-human trigger. Repair: "
            "nh task restore-approval."
        )

    # W2.6: orphaned worktrees. A crash between acquire and cleanup leaves a
    # full checkout under ~/.no_human/worktrees/ holding a stale git
    # registration in the primary repo — invisible until the next acquire
    # fails or the disk fills. A worktree whose task is not currently ACTIVE
    # is an orphan (worktrees are disposable by design; resume re-creates).
    #
    # Directory names are `<task_id>.<owner_pid>.<token>` (one per RUN, so two
    # overlapping attempts of a task cannot share a checkout); the pre-fix bare
    # `<task_id>` shape still exists under older roots and `worktree_owner`
    # parses both. Reading the name rather than comparing it whole is what keeps
    # this check alive across the rename — matching `entry.name` against task ids
    # would simply have stopped finding anything, silently.
    from .config import NO_HUMAN_HOME, pid_alive, worktree_owner
    wt_root = NO_HUMAN_HOME / "worktrees"
    if wt_root.is_dir():
        # A worktree is this store's orphan only if its dir name is a task
        # KNOWN to this store but not currently active. A worktree whose id
        # isn't in this store at all belongs to a different install/db (or is
        # an isolated test's tmp DB against the real ~/.no_human) — not our
        # concern, and flagging it made the empty-DB doctor test fail.
        rows = await store.query("SELECT id, status FROM tasks")
        known = {r[0]: r[1] for r in rows}
        active_states = {"pending", "context", "planning",
                         "implementing", "reviewing", "testing"}
        for entry in sorted(wt_root.iterdir()):
            if not entry.is_dir():
                continue
            task_id, owner_pid = worktree_owner(entry.name)
            st = known.get(task_id)
            if st is None:
                continue
            # An ACTIVE task can still own a leftover: it may be running in a
            # different per-run directory while a killed earlier run's is still
            # on disk. A dead owner pid settles that without guessing.
            dead_owner = owner_pid is not None and not pid_alive(owner_pid)
            if dead_owner or st not in active_states:
                why = (f"its owner process {owner_pid} is gone" if dead_owner
                       else f"its task is {st}, not active")
                d.contradictions.append(
                    f"ORPHANED WORKTREE: {entry} — {why}; "
                    "a crashed/finished run left the checkout behind. "
                    f"Remove it with `git worktree remove --force {entry}` "
                    "(from the primary repo) or delete it and "
                    "`git worktree prune`."
                )

    # 0.4: leaked eval sandboxes. The eval harness clones into
    # tempfile.mkdtemp(nh-eval-*/nh-shadow-*); a crash before cleanup used to
    # leave them behind (the cleanup is now in eval/harness.py). Advisory — a
    # disk leak, not a state contradiction, so it never fails the doctor gate.
    # >2h old avoids flagging a sandbox from an eval that is still running.
    tmp_root = Path(tempfile.gettempdir())
    stale_cut = time.time() - 2 * 3600
    for pat in ("nh-eval-*", "nh-shadow-*"):
        for entry in sorted(tmp_root.glob(pat)):
            try:
                if entry.is_dir() and entry.stat().st_mtime < stale_cut:
                    d.advisories.append(
                        f"LEAKED EVAL SANDBOX: {entry} (>2h old) — a crashed eval "
                        f"left it behind; `rm -rf {entry}` to reclaim disk."
                    )
            except OSError:
                pass

    # Per-status required evidence: a task claiming a status must have the
    # events that back the claim.
    for status, kinds in REQUIRED_EVIDENCE.items():
        sorted_kinds = sorted(kinds)
        placeholders = ",".join("?" for _ in sorted_kinds)
        # A code-review / investigation task finishes with an artifact, not a
        # PR — don't demand pr_open evidence of it.
        kind_filter = ""
        kind_params: tuple[str, ...] = ()
        if "pr_open" in kinds:
            kf_ph = ",".join("?" for _ in PR_LESS_KINDS)
            kind_filter = f" AND COALESCE(t.kind, '') NOT IN ({kf_ph})"
            kind_params = PR_LESS_KINDS
        rows = await store.query(
            f"""SELECT t.id FROM tasks t WHERE t.status = ?{kind_filter}
                AND NOT EXISTS (
                  SELECT 1 FROM task_events e WHERE e.task_id = t.id
                  AND json_extract(e.data, '$.kind') IN ({placeholders}))""",
            (status, *kind_params, *sorted_kinds),
        )
        for (task_id,) in rows:
            d.evidence_gaps.append(
                f"task {task_id[:8]} is '{status}' with none of "
                f"{'/'.join(sorted_kinds)} on record — the status is not "
                "backed by evidence."
            )

    # A failed attempt must SAY why (C2): an empty failure_reason is exactly
    # the "post-implement failure came back empty" gap that made task 6cfdb936
    # undiagnosable for 6 attempts. The store backstop prevents new ones; this
    # catches historical rows and any path that bypasses the store.
    rows = await store.query(
        """SELECT id, task_id, status FROM attempts
           WHERE status IN ('failed', 'interrupted')
           AND COALESCE(TRIM(failure_reason), '') = ''""")
    for (attempt_id, task_id, a_status) in rows:
        d.evidence_gaps.append(
            f"attempt {attempt_id[:8]} (task {task_id[:8]}) is '{a_status}' "
            "with an empty failure_reason — the stop is undiagnosable."
        )
    rows = await store.query(
        "SELECT id, blocker FROM tasks WHERE status = 'escalated'"
    )
    for task_id, blocker in rows:
        data = json.loads(blocker) if blocker else {}
        if not data.get("question") and not data.get("root_cause_hypothesis"):
            d.evidence_gaps.append(
                f"task {task_id[:8]} is 'escalated' with an empty blocker — "
                "a human was summoned with nothing to decide on."
            )
    return d
