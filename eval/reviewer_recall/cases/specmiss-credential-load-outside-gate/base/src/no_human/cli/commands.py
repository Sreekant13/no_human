"""`nh` command-line interface (PLAN.md Part 6).

Phase 0 runs the orchestrator synchronously in-process (no daemon yet — that is
Phase 4). `nh task add` runs a task end-to-end with live streaming; `nh watch`
runs a staged task inside the Textual TUI.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .. import __version__
from ..agent.claude_backend import ClaudeBackend
from ..config import AuthError, assert_subscription_mode, load_config
from ..context import ContextGatherer, build_default_sources
from ..core.db import Store
from ..core.events import EventPersister
from ..core.orchestrator import CODER_ROLE, Orchestrator, is_agent_session
from ..core.task import Task, TaskStatus
from ..intake import classify_kind, ingest_from_url, parse_source
from ..notify.slack import SlackNotifier

console = Console()


def _server_owns_worker(config) -> bool:
    """True when an `nh start` server is up, and therefore owns the worker pool.

    Its scheduler claims every PENDING or IMPLEMENTING task (scheduler.py
    ``_CLAIMABLE``). A CLI command that ALSO runs the task in-process gives one
    task two orchestrators driving the same git checkout — two coders, two
    reviewers, two commits, and potentially two PRs. Observed on task 84251cb2:
    duplicate `commit`/`reviewing` events and a doubled escalation.

    Any failure to reach the server is treated as "no server": the cost of a
    false negative is the old behavior, while a false positive would silently
    strand the task.
    """
    import json as _json
    import urllib.error
    import urllib.request

    srv = config.get("server", {}) or {}
    host = srv.get("host", "127.0.0.1")
    port = srv.get("port", 8420)
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/tasks", timeout=1.5
        ) as resp:
            if resp.status != 200:
                return False
            _json.loads(resp.read() or b"null")  # it really is our API
            return True
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False


def _bootstrap(*, require_auth: bool = True):
    """Load config + enforce subscription mode. Returns (config, scrub_report)."""
    config = load_config()
    report = None
    if require_auth:
        try:
            # `or {}`: config.yaml is hand-edited, and a bare `llm:` with its
            # body commented out deep-merges to None, not to a dict.
            _llm = config.get("llm") or {}
            report = assert_subscription_mode(
                profile=_llm.get("auth_profile"),
                auth_mode=_llm.get("auth_mode", "subscription"),
            )
        except AuthError as exc:
            console.print(f"[bold red]auth error:[/] {exc}")
            console.print(
                "\n[bold]Fix:[/] run [bold]nh init[/] to set up authentication, or:\n"
                "  1. [bold]claude setup-token[/]  (creates a subscription token)\n"
                "  2. Add it to ~/.no_human/.env:\n"
                "     [bold]echo 'CLAUDE_CODE_OAUTH_TOKEN=<token>' >> ~/.no_human/.env[/]\n"
                "  3. If ANTHROPIC_API_KEY is set, unset it:\n"
                "     [bold]unset ANTHROPIC_API_KEY[/]"
            )
            sys.exit(2)
    return config, report


def _assert_backend_usable() -> None:
    """Refuse to start the server when the coding backend can't run a task.

    A present OAuth token is necessary but not sufficient: the Claude Agent SDK
    shells out to the ``claude`` CLI for every task, and a fresh install can have
    a valid token yet no CLI on PATH. Without this the board renders green and
    EVERY task then dies at launch with CLINotFoundError — a silent cliff. The
    token half is already enforced by ``assert_subscription_mode``; this closes
    the CLI half. Blocking and loud, exactly where the operator will see it.
    """
    from ..agent.backend_check import find_claude_cli

    if find_claude_cli() is None:
        console.print(
            "[bold red]coding backend unavailable:[/] the `claude` CLI was not "
            "found.\n"
            "The Claude Agent SDK shells out to it for every task, so the board "
            "would load but every task would fail at launch.\n\n"
            "[bold]Fix:[/] install the Claude Code CLI, then restart:\n"
            "  [bold]npm install -g @anthropic-ai/claude-code[/]\n"
            "Verify with [bold]nh doctor[/] (it now checks this)."
        )
        sys.exit(2)


def _build_orchestrator(config, store: Store, *, event_sink=None, task=None) -> Orchestrator:
    # Single in-process Claude Agent SDK backend (lean-stack; no alternate
    # backend abstraction).
    backend = ClaudeBackend(
        model=config.primary_model,
        forbidden_paths=config["safety"]["forbidden_paths"],
        never_push_to=config["git"]["never_push_to"],
    )
    review_backend = None  # reviewer defaults to ClaudeBackend(readonly=True)
    notifier = SlackNotifier(config["notifications"].get("slack_webhook_url"))
    gatherer = ContextGatherer(build_default_sources(store, config.data))
    from ..learning import LearningQueue
    from ..review.reviewer import AdversarialReviewer
    reviewer = AdversarialReviewer(model=config.review_model, backend=review_backend)
    return Orchestrator(store, config.data, backend, notifier,
                        event_sink=event_sink, context_gatherer=gatherer,
                        learning_queue=LearningQueue(store),
                        reviewer=reviewer)


async def _run_cli_grill(config, task: Task) -> Task:
    """B2: Interactive intake grill — one question at a time in the terminal."""
    from ..intake.grill import GrillQuestion, GrillResult, grill_step

    grill_backend = ClaudeBackend(
        model=config.primary_model,
        forbidden_paths=config["safety"]["forbidden_paths"],
        never_push_to=config["git"]["never_push_to"],
        readonly=True,
    )
    qa_history: list[dict] = []
    console.rule("[bold blue]intake grill — refining your task spec")
    console.print("[dim]The grill explores the repo and asks clarifying questions.[/]\n")

    while True:
        console.print("[dim]thinking…[/]", end="")
        step = await grill_step(
            task.title, task.description, task.repo_path,
            qa_history, grill_backend,
        )
        console.print("\r", end="")  # clear "thinking…"

        if isinstance(step, GrillResult):
            task.title = step.title or task.title
            task.description = step.description or task.description
            if step.acceptance_criteria:
                task.acceptance_criteria = step.acceptance_criteria
            # Human-answered Q&A joins the same audit surface the unattended
            # grill uses (context["intake_qa"] → prompt + PR body), and
            # grill_complete stops the orchestrator's auto-grill from
            # re-asking what the human already answered.
            ctx = task.context or {}
            ctx["intake_qa"] = [
                {"question": qa["question"], "decision_it_changes": "",
                 "answer": qa["answer"], "source": "human", "carve_out": "none"}
                for qa in qa_history
            ]
            ctx["grill_complete"] = True
            task.context = ctx
            console.print()
            console.rule("[bold green]grill complete")
            console.print(f"  [bold]Title:[/] {task.title}")
            if task.description:
                console.print(f"  [bold]Description:[/] {task.description[:200]}")
            for i, ac in enumerate(task.acceptance_criteria, 1):
                console.print(f"  [green]AC{i}:[/] {ac}")
            console.print()
            return task

        # GrillQuestion — show and get answer
        console.print(f"[bold yellow]Q{step.round}:[/] {step.question}")
        for s in step.suggestions:
            console.print(f"  [cyan]{s}[/]")
        answer = click.prompt("Your answer", default="")
        if not answer.strip():
            answer = "Proceed with what we have"
        qa_history.append({"question": step.question, "answer": answer})


def _persisting(persister, task_id: str, inner):
    """Wrap a console sink so a CLI in-process run also records its events.

    `nh task add --run` and `nh reply --run` drive an Orchestrator directly, so
    they never touched the scheduler's persistence path: nothing reached
    task_events and the board showed "Waiting for events…" forever. Mirrors the
    scheduler's stamping, including leaving a subagent's own task_id alone.
    """
    def sink(event: dict) -> None:
        event.setdefault("ts", time.time())
        event.setdefault("task_id", task_id)
        persister.record(event)
        inner(event)

    return sink


def render_event(event: dict) -> None:
    """Format one orchestrator/agent event as a console line (verbose mode)."""
    src, kind, text = event.get("source"), event.get("kind"), event.get("text", "")
    if is_agent_session(src):
        # The coder is the unlabelled default; a planner lens or the aggregator
        # is named, so the console says which role is doing the work.
        who = "" if src == CODER_ROLE else f"[magenta]{src}[/] "
        if kind == "tool_use":
            args = event.get("tool_input") or {}
            summary = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(args.items())[:3])
            console.print(f"  {who}[cyan]→ {event.get('tool_name')}[/]([dim]{summary}[/])")
        elif kind == "text" and text.strip():
            console.print(f"  {who}[white]{text.strip()[:500]}[/]")
        elif kind == "thinking" and text.strip():
            console.print(f"  {who}[dim italic]· {text.strip()[:200]}[/]")
        elif kind == "tool_result" and text.strip():
            console.print(f"    [dim]{text.strip()[:200]}[/]")
        elif kind == "result" and not event.get("is_error"):
            # Error results (e.g. max_turns) are reported by the orchestrator's
            # own agent_error/attempt_failed lines; don't double-print them here.
            console.print(
                f"  {who}[dim]· agent done: {event.get('num_turns')} turns, "
                f"{event.get('tokens_used')} tokens[/]"
            )
    else:  # orchestrator
        color = {
            "state": "blue", "commit": "green", "pr_open": "bold green",
            "tests": "yellow", "tamper": "magenta", "escalated": "bold red",
            "failed": "bold red", "stuck": "red", "paused_quota": "yellow",
            "attempt_start": "blue",
        }.get(kind, "dim")
        console.print(f"[{color}]● {kind}[/] {text}")


class CompactProgress:
    """Compact single-line progress for default (non-verbose) mode.

    Shows: [task-id] step | turns=N | elapsed=Xs | last tool: Edit
    Milestones (commit, PR, errors) are printed as persistent lines.
    """
    _MILESTONE_KINDS = {
        "commit", "pr_open", "tests", "escalated", "failed",
        "attempt_start", "attempt_failed", "stuck", "paused_quota",
        "blocker", "profile", "ci_backend",
        # Which model has which role. Printed even in compact mode: it is the
        # one line that makes a silently-shadowed config visible.
        "models",
    }

    def __init__(self, task_id: str):
        self.task_id = task_id[:8]
        self._step = "starting"
        self._turns = 0
        self._last_tool = ""
        self._start = __import__("time").monotonic()
        self._status = console.status(
            self._format(), spinner="dots", spinner_style="blue"
        )
        self._status.start()

    def _format(self) -> str:
        elapsed = int(__import__("time").monotonic() - self._start)
        parts = [
            f"[bold]{self.task_id}[/]",
            f"[blue]{self._step}[/]",
            f"turns={self._turns}",
            f"{elapsed}s",
        ]
        if self._last_tool:
            parts.append(f"[cyan]{self._last_tool}[/]")
        return " · ".join(parts)

    def __call__(self, event: dict) -> None:
        src = event.get("source")
        kind = event.get("kind", "")
        text = event.get("text", "")

        if is_agent_session(src):
            if kind == "tool_use":
                self._last_tool = event.get("tool_name") or ""
            elif kind == "result" and src == CODER_ROLE:
                # Only the implementer's turns count against the turn budget;
                # the planner's own turns are a separate, earlier budget.
                self._turns = event.get("num_turns", self._turns)
        else:
            if "status" in event:
                self._step = event["status"]
            if kind in self._MILESTONE_KINDS:
                # Print milestone as a persistent line, then resume spinner.
                self._status.stop()
                color = {
                    "commit": "green", "pr_open": "bold green",
                    "tests": "yellow", "escalated": "bold red",
                    "failed": "bold red", "attempt_start": "blue",
                    "attempt_failed": "red", "profile": "dim",
                }.get(kind, "dim")
                console.print(f"  [{color}]● {kind}[/] {text}")
                self._status.start()

        self._status.update(self._format())

    def stop(self) -> None:
        self._status.stop()


# --------------------------------------------------------------------------- #


@click.group()
@click.version_option(__version__, prog_name="nh")
def cli() -> None:
    """no_human — autonomous AI software delivery (runs on your subscription)."""


@cli.command("init")
def init_cmd():
    """Set up no_human from scratch: prerequisites, token, config, first repo.

    Safe to run again — never overwrites existing config, secrets, or data.
    """
    from .init_cmd import (
        check_prerequisites,
        ensure_config,
        ensure_home_dir,
        offer_onboard,
        print_summary,
        setup_token,
    )

    console.rule("[bold]no_human — first-time setup")

    # 1. Prerequisites.
    console.print("\n[bold]1. Checking prerequisites[/]")
    errors, warnings = check_prerequisites()
    if errors:
        console.print(f"\n[red]Missing {len(errors)} required tool(s). "
                       "Install them and re-run `nh init`.[/]")
        sys.exit(1)

    # 2. Home directory.
    console.print("\n[bold]2. Home directory[/]")
    created = ensure_home_dir()
    if created:
        console.print("  [green]✓[/] created ~/.no_human/ (mode 700)")
    else:
        console.print("  [green]✓[/] ~/.no_human/ exists")

    # 3. Billing / authentication.
    console.print("\n[bold]3. Authentication[/]")
    token_ready, auth_mode = setup_token()

    # 4. Config file.
    console.print("\n[bold]4. Configuration[/]")
    ensure_config(auth_mode=auth_mode)

    # 5. Optional: onboard a repo.
    console.print("\n[bold]5. Repo onboarding[/]")
    repo_path = offer_onboard()
    if repo_path:
        # Run onboard inline — reuse the existing nh onboard logic.
        # Catch SystemExit so a failing onboard doesn't kill init.
        console.print(f"  Running [bold]nh onboard {repo_path}[/] …")
        try:
            ctx = click.Context(onboard, info_name="nh onboard")
            ctx.invoke(onboard, repo=repo_path, confirm=False, agent=False)
        except SystemExit:
            console.print(
                "  [yellow]onboarding did not complete — you can re-run:[/]\n"
                f"    [bold]nh onboard {repo_path}[/]"
            )
        else:
            # Auto-confirm if the test command was proven — don't force a
            # separate `--confirm` step for an already-verified profile.
            try:
                ctx2 = click.Context(onboard, info_name="nh onboard")
                ctx2.invoke(onboard, repo=repo_path, confirm=True, agent=False)
            except SystemExit:
                console.print(
                    f"\n  To confirm the profile:\n"
                    f"    [bold]nh onboard {repo_path} --confirm[/]"
                )

    # 6. Summary.
    console.print()
    from ..config import CONFIG_PATH
    print_summary(
        token_ready=token_ready,
        config_path=CONFIG_PATH,
        repo_path=repo_path,
    )


_KNOWN_TIERS = ("trivial", "simple", "standard", "complex")


def format_tier_summary(
    tier: str,
    signals: list,
    *,
    predicted: bool,
    moa_min_signals: int = 2,
    moa_enabled: bool = False,
    moa_proposers: int = 3,
) -> str:
    """Compact human-readable resourcing summary for a task's complexity tier.

    Pure and Store/LLM-free — every rule here mirrors a live gate so the
    diagnostic can't silently drift from what actually ran:
      - MoA planning fan-out: gated FIRST by ``llm.moa_planning.enabled``
        (orchestrator.py's `_generate_plan`, `if moa_cfg.get("enabled", False):`
        — a global kill switch that short-circuits everything else), and only
        then by tier/signal count (`tier == "complex" or len(signals) >=
        min_signals`). Reporting "applied" while ``enabled=False`` would be a
        lie about the one gate an operator can turn off outright.
      - Extended thinking: ``core.complexity.is_complex`` (`tier == "complex"`).
      - Complex-tier angle review passes: `Reviewer._tier_wants_angles`
        (`tier == "complex"`), naming the two angles (security, tests).
    """
    label = "predicted (task has not run)" if predicted else "recorded"
    fired = ", ".join(signals) if signals else "none"
    tier_display = tier if tier in _KNOWN_TIERS else f"{tier} (unrecognized tier)"

    is_complex_tier = tier == "complex"
    gate_would_fire = is_complex_tier or len(signals) >= moa_min_signals
    if not moa_enabled:
        moa_line = ("MoA planning fan-out: not applied "
                    "(disabled globally: llm.moa_planning.enabled=False)")
        moa_mark = "·"
    elif gate_would_fire:
        moa_line = f"MoA planning fan-out: applied ({moa_proposers} Opus proposers)"
        moa_mark = "✓"
    else:
        moa_line = f"MoA planning fan-out: not applied (signals {len(signals)}/{moa_min_signals})"
        moa_mark = "·"

    if is_complex_tier:
        thinking_mark, thinking_line = "✓", "extended thinking: on"
        angles_mark = "✓"
        angles_line = "complex-tier angle review passes: applied (security, tests)"
    else:
        thinking_mark, thinking_line = "·", "extended thinking: off"
        angles_mark = "·"
        angles_line = "complex-tier angle review passes: not applied"

    return "\n".join([
        f"tier: {tier_display} ({label})",
        f"signals: {fired}",
        "resourcing:",
        f"  {moa_mark} {moa_line}",
        f"  {thinking_mark} {thinking_line}",
        f"  {angles_mark} {angles_line}",
    ])


@cli.group()
def task() -> None:
    """Manage tasks."""


@task.command("add")
@click.argument("source", required=False)
@click.option("--title", default=None, help="Freeform task title (instead of a URL).")
@click.option("--repo", required=True, type=click.Path(exists=True), help="Target repo path.")
@click.option("--description", default=None, help="Longer description.")
@click.option("--criteria", multiple=True, help="Acceptance criterion (repeatable).")
@click.option("--external-id", default=None, help="External id, e.g. PROJ-123.")
@click.option("--kind", default=None,
              help="Override the task type (feature|bugfix|ci_fix|traceability|test_gap).")
@click.option("--linked-repo", multiple=True, type=click.Path(exists=True),
              help="Additional repo path for multi-repo tasks (repeatable).")
@click.option("--run/--no-run", default=True, help="Run immediately (default) or just stage.")
@click.option("-v", "--verbose", is_flag=True, help="Show full tool-call log (default: compact progress).")
@click.option("--grill/--no-grill", default=True,
              help="Run the intake grill to refine the spec (default: on; --no-grill to skip).")
@click.option("--backend", default=None, type=click.Choice(["claude"]),
              help="Override the worker backend for this task (default: from config; "
                   "single in-process Claude Agent SDK backend).")
def task_add(source, title, repo, description, criteria, external_id, kind, linked_repo, run, verbose, grill, backend):
    """Add a task — from a GitHub/GitLab URL or id, or a freeform --title.

    Examples:
      nh task add https://code.example.com/org/repo/issues/12 --repo ~/repo
      nh task add PROJ-42 --repo ~/repo
      nh task add --title "Fix X" --repo ~/repo
    """
    config, _ = _bootstrap()

    async def _go():
        async with Store(config.db_path) as store:
            if source:
                ref = parse_source(source)
                console.print(f"[blue]ingesting[/] {ref.kind}: {ref.ref}")
                try:
                    t = ingest_from_url(source, config.data)
                except Exception as exc:  # noqa: BLE001
                    console.print(f"[red]intake failed:[/] {exc}")
                    sys.exit(1)
                t.repo_path = str(Path(repo).resolve())
                t.acceptance_criteria += list(criteria)
                if grill:
                    t = await _run_cli_grill(config, t)
            elif title:
                t = Task.new(title, repo_path=str(Path(repo).resolve()),
                             description=description, external_id=external_id)
                t.acceptance_criteria = list(criteria)
                if grill:
                    t = await _run_cli_grill(config, t)
            else:
                console.print("[red]provide a SOURCE url/id or --title[/]")
                sys.exit(1)
            # WS-E: attach linked repos for multi-repo tasks.
            if linked_repo:
                t.linked_repos = [str(Path(r).resolve()) for r in linked_repo]
                # D19: fail at intake, not silently mid-attempt. A linked repo the
                # agent cannot stage is a repo it can never commit to — and the
                # planner will have written a plan that names its files.
                from ..core.multi_repo import validate_linked_repos
                errors = validate_linked_repos(t)
                if errors:
                    # validate_linked_repos checks the primary repo too, so the
                    # label stays generic — the message names the offending path.
                    for err in errors:
                        console.print(f"[red]multi-repo intake:[/] {err}")
                    sys.exit(1)
            # WS-A: tag the task with its type so the right pipeline drives it.
            verdict = classify_kind(t, override=kind)
            t.kind = verdict.kind.value
            if backend:
                t.config["backend"] = backend
            await store.create_task(t)
            console.print(f"[green]created task[/] [bold]{t.id[:8]}[/] — {t.title}")
            console.print(f"  [magenta]kind:[/] {t.kind}  [dim]({verdict.reason})[/]")
            if backend:
                console.print(f"  [cyan]backend:[/] {backend}")
            if t.linked_repos:
                console.print(f"  [cyan]multi-repo:[/] {len(t.linked_repos) + 1} repos")
            if t.acceptance_criteria:
                console.print(f"  {len(t.acceptance_criteria)} acceptance criteria")
            # Warn only if the profile won't actually drive the task under the
            # active policy — a proven profile with profile.auto_confirm_proven
            # on IS usable even without a human confirm click, so warning then
            # would be a lie (it drove ca23ce68 to a clean PR while this warned).
            from ..profile import ProjectProfile
            prof = await store.get_profile(t.repo_path) or ProjectProfile.load(t.repo_path)
            auto = bool(config.data.get("profile", {}).get("auto_confirm_proven", False))
            if not prof or not prof.usable_under_policy(auto_confirm_proven=auto):
                console.print(
                    "[yellow]⚠ repo profile not usable[/] — test command will be "
                    "auto-detected (may be wrong). Run:\n"
                    f"  [bold]nh onboard {t.repo_path} --confirm[/]"
                )
            if not run:
                console.print(f"staged. run it with:  [bold]nh watch {t.id[:8]}[/]")
                return
            if _server_owns_worker(config):
                # A new task is PENDING, which the server's scheduler claims.
                console.print(
                    "[cyan]the running server picked it up[/] — "
                    f"watch it with: [bold]nh watch {t.id[:8]}[/]"
                )
                return
            if verbose:
                sink = render_event
            else:
                sink = CompactProgress(t.id)
            if verbose:
                console.rule(f"[bold]running {t.id[:8]}")
            async with EventPersister(store, t.id) as persister:
                orch = _build_orchestrator(
                    config, store, event_sink=_persisting(persister, t.id, sink), task=t)
                outcome = await orch.run_task(t)
            if not verbose:
                sink.stop()
            console.rule(f"[bold]{outcome.status.value}")
            if outcome.pr_url:
                console.print(f"[bold green]PR:[/] {outcome.pr_url}")
            console.print(outcome.detail)

    asyncio.run(_go())


@task.command("context")
@click.argument("task_id")
def task_context(task_id):
    """Gather and show context for a staged task (no implementation run)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                return
            gatherer = ContextGatherer(build_default_sources(store, config.data))
            ctx = await gatherer.gather(t)
            t.context = {**(t.context or {}), "gathered": ctx.to_dict()}
            await store.update_task(t)
            for c in ctx.chunks:
                console.print(f"[cyan]\\[{c.source}][/] {c.title}  [dim]{c.ref}[/]")
            if ctx.errors:
                for src, err in ctx.errors.items():
                    console.print(f"[yellow]! {src}: {err}[/]")
            comp = ctx.completeness
            verdict = "[green]complete[/]" if comp and comp.ok else "[yellow]incomplete[/]"
            console.print(f"\ncompleteness: {verdict}")
            if comp:
                console.print(f"  present: {comp.present}")
                if comp.missing:
                    console.print(f"  [yellow]missing: {comp.missing}[/]")

    asyncio.run(_go())


@task.command("tier")
@click.argument("task_id")
def task_tier(task_id):
    """Show a task's complexity tier and the resourcing it bought (read-only)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            ctx = t.context or {}
            # `or {}`: config.yaml is hand-edited and a bare `llm:`/`moa_planning:`
            # with its body commented out deep-merges to None, not to a dict.
            moa_cfg = (config.get("llm") or {}).get("moa_planning") or {}
            if ctx.get("complexity_tier"):
                tier = ctx["complexity_tier"]
                signals = list(ctx.get("complexity_signals") or [])
                predicted = False
            else:
                from ..core.complexity import compute_tier
                tier, signals = compute_tier(t, moa_cfg)
                predicted = True
            console.print(f"[bold]{t.id}[/]  {t.title}")
            console.print(format_tier_summary(
                tier, signals,
                predicted=predicted,
                moa_min_signals=int(moa_cfg.get("min_signals", 2)),
                moa_enabled=bool(moa_cfg.get("enabled", False)),
                moa_proposers=int(moa_cfg.get("proposers", 3)),
            ))

    asyncio.run(_go())


@task.command("config")
@click.argument("task_id")
@click.argument("assignments", nargs=-1, required=True)
def task_config(task_id, assignments):
    """Set human-only per-task overrides: nh task config TASK_ID KEY=VALUE ...

    Human-only by construction — this CLI is the operator's tool, the agent
    never calls it. Accepts exactly the keys the orchestrator reads as
    per-task overrides (size limits, lifetime caps, the per-attempt token
    cap). An existing numeric cap is never lowered — see `apply_action`.
    """
    from ..blockers import ActionError, apply_action

    config, _ = _bootstrap(require_auth=False)

    settings: dict[str, str] = {}
    for assignment in assignments:
        if "=" not in assignment:
            console.print(f"[red]malformed assignment (want KEY=VALUE):[/] {assignment}")
            sys.exit(1)
        key, _, value = assignment.partition("=")
        settings[key.strip()] = value.strip()

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            try:
                applied = apply_action(t, {"set_task_config": settings})
            except ActionError as exc:
                console.print(f"[red]{exc}[/]")
                sys.exit(1)
            await store.update_task_columns(t)
            console.print(f"[green]applied[/] {applied}")

    asyncio.run(_go())


REPO_CONFIG_KEYS = frozenset({"default_attempt_tokens", "default_lifetime_tokens"})


@cli.group("repo")
def repo_group() -> None:
    """Manage per-repo profile settings."""


@repo_group.command("config")
@click.argument("repo_path", type=click.Path(exists=True))
@click.argument("assignments", nargs=-1)
def repo_config(repo_path, assignments):
    """Set or inspect human-only repo profile defaults (SCRUM-26).

    nh repo config REPO_PATH                 — inspect current defaults
    nh repo config REPO_PATH KEY=VALUE ...   — set defaults

    Human-only by construction, like `nh task config` — this is the
    operator's calibration knob for a repo's default per-task token budgets.
    They are copied into task.config at task creation whenever the task has
    no explicit override; an explicit `nh task config` value always wins.
    Accepts exactly default_attempt_tokens / default_lifetime_tokens.
    """
    from ..profile import ProjectProfile

    config, _ = _bootstrap(require_auth=False)
    repo = str(Path(repo_path).expanduser().resolve())

    resolved: dict[str, int] = {}
    for assignment in assignments:
        if "=" not in assignment:
            console.print(f"[red]malformed assignment (want KEY=VALUE):[/] {assignment}")
            sys.exit(1)
        key, _, raw = assignment.partition("=")
        key = key.strip()
        if key not in REPO_CONFIG_KEYS:
            console.print(
                f"[red]{key!r} is not settable on a repo profile "
                f"(allowed: {', '.join(sorted(REPO_CONFIG_KEYS))})[/]"
            )
            sys.exit(1)
        try:
            value = int(raw.strip())
        except (TypeError, ValueError):
            console.print(f"[red]{key} must be an integer, got {raw!r}[/]")
            sys.exit(1)
        if value <= 0:
            console.print(f"[red]{key} must be positive, got {value}[/]")
            sys.exit(1)
        resolved[key] = value

    async def _go():
        async with Store(config.db_path) as store:
            profile = await store.get_profile(repo) or ProjectProfile(repo_path=repo)
            if not resolved:
                console.print(f"default_attempt_tokens={profile.default_attempt_tokens or 0}")
                console.print(f"default_lifetime_tokens={profile.default_lifetime_tokens or 0}")
                return
            for key, value in resolved.items():
                setattr(profile, key, value)
            await store.upsert_profile(profile)
            console.print(
                "[green]applied[/] " + ", ".join(f"{k}={resolved[k]}" for k in sorted(resolved))
            )

    asyncio.run(_go())


@task.command("list")
def task_list():
    """List all tasks as a board."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            tasks = await store.list_tasks()
            table = Table(title="no_human tasks")
            table.add_column("id", style="bold")
            table.add_column("kind", style="magenta")
            table.add_column("status")
            table.add_column("att", justify="right", style="dim")
            table.add_column("turns", justify="right", style="dim")
            table.add_column("title")
            table.add_column("repo", style="cyan")
            table.add_column("PR", style="green")
            for t in tasks:
                attempts = await store.list_attempts(t.id)
                att_n = str(len(attempts)) if attempts else "—"
                last_turns = "—"
                pr_url = ""
                for a in reversed(attempts):
                    if a.get("turns_used") and last_turns == "—":
                        last_turns = str(a["turns_used"])
                    if a.get("pr_url") and not pr_url:
                        pr_url = a["pr_url"]
                repo_name = t.repo_path.rstrip("/").rsplit("/", 1)[-1] if t.repo_path else ""
                status_str = t.status.value
                status_colors = {
                    "done": "green", "failed": "red", "escalated": "bold red",
                    "awaiting_approval": "yellow", "implementing": "blue",
                }
                color = status_colors.get(status_str, "")
                styled_status = f"[{color}]{status_str}[/]" if color else status_str
                table.add_row(
                    t.id[:8], t.kind, styled_status, att_n, last_turns,
                    t.title[:50], repo_name[:20],
                    "✓" if pr_url else "",
                )
            console.print(table)

    asyncio.run(_go())


@task.command("show")
@click.argument("task_id")
def task_show(task_id):
    """Show a task's requirements, attempts, and evidence."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                return
            console.print(f"[bold]{t.id}[/]  [blue]{t.status.value}[/]  [magenta]{t.kind}[/]")
            console.print(f"title: {t.title}")
            if t.description:
                console.print(f"description: {t.description}")
            if t.acceptance_criteria:
                console.print("acceptance criteria:")
                for c in t.acceptance_criteria:
                    console.print(f"  - {c}")
            console.print(f"repo: {t.repo_path}")
            if t.blocker:
                console.print(f"[red]blocker:[/] {t.blocker}")
            attempts = await store.list_attempts(t.id)
            for a in attempts:
                console.print(
                    f"  attempt {a['attempt_number']}: {a['status']} "
                    f"branch={a['branch_name']} pr={a['pr_url']} "
                    f"turns={a['turns_used']} tests={a['test_results']}"
                )

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Task lifecycle: pause / resume / cancel / retry                              #
# --------------------------------------------------------------------------- #

_PARKED = {TaskStatus.BLOCKED, TaskStatus.AWAITING_INPUT,
           TaskStatus.PAUSED_QUOTA, TaskStatus.ESCALATED}
_ACTIVE_STATES = {TaskStatus.CONTEXT, TaskStatus.PLANNING, TaskStatus.IMPLEMENTING,
                  TaskStatus.REVIEWING, TaskStatus.TESTING}


@task.command("pause")
@click.argument("task_id")
@click.option("--reason", default="user paused via CLI", help="Reason for pausing.")
def task_pause(task_id, reason):
    """Pause a running task. A running attempt stops at its next tool call."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            if t.status in _PARKED or t.status in {TaskStatus.DONE, TaskStatus.FAILED}:
                console.print(
                    f"[yellow]task is {t.status.value}[/] — cannot pause "
                    f"(only active tasks can be paused)"
                )
                return

            # Always raise the flag: it is the only signal a running orchestrator
            # observes, and the only write that cannot race it (single-writer
            # control column, never `context`).
            await store.request_cancel(t.id, reason)

            if _server_owns_worker(config):
                # The server owns this task's status. It will checkpoint the
                # work as [WIP-BLOCKED] and park the task itself; writing the
                # status from here would race the attempt that is still running.
                console.print(
                    f"[yellow]pause requested[/] {t.id[:8]} — the running attempt "
                    f"will checkpoint and stop within a few seconds.\n"
                    f"Watch it: [bold]nh logs {t.id[:8]}[/]"
                )
                return

            # No server: nothing is running, so this process is the only writer.
            t.blocker = {"category": "USER_PAUSED", "question": reason,
                         "root_cause_hypothesis": reason}
            await store.update_task(t)
            await store.set_status(t, TaskStatus.BLOCKED)
            await store.clear_cancel_request(t.id)
            console.print(f"[yellow]paused[/] {t.id[:8]} — resume with: "
                          f"[bold]nh task resume {t.id[:8]}[/]")

    asyncio.run(_go())


@task.command("resume")
@click.argument("task_id")
def task_resume(task_id):
    """Resume a paused/blocked task (sets it to IMPLEMENTING)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            if t.status not in _PARKED:
                console.print(
                    f"[yellow]task is {t.status.value}[/] — only parked tasks "
                    f"(blocked/awaiting_input/paused_quota/escalated) can be resumed"
                )
                return
            # Continue from the checkpoint the blocker recorded, exactly as
            # `nh reply` does. Without this the next attempt branches from a
            # STALE `resume_from` (or from base) and silently throws away the
            # work the parked attempt had already committed. Read it before
            # clearing the blocker, which is what holds the sha.
            from ..blockers import resume_checkpoint
            checkpoint = resume_checkpoint(t.blocker)
            if checkpoint:
                t.context = await store.merge_context(
                    t.id, {"resume_from": checkpoint})

            t.blocker = None
            t.wake_check_at = None
            await store.update_task_columns(t)
            # "Run again" withdraws any pending stop, or the next attempt would
            # honour it immediately and park the task straight back.
            await store.clear_cancel_request(t.id)
            await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
            resumed_at = f" from {checkpoint['sha'][:8]}" if checkpoint else ""
            console.print(f"[green]resumed[/] {t.id[:8]}{resumed_at} → implementing")

    asyncio.run(_go())


@task.command("restore-approval")
@click.argument("task_id")
@click.option("--reason", default="spurious escalation reversed",
              help="Recorded in the repair event.")
def task_restore_approval(task_id, reason):
    """Return a spuriously-escalated task to awaiting_approval.

    Hard-scoped repair, not a generic override: only an ESCALATED task that
    already opened a PR (pr_open event + pr_watch in context) qualifies —
    the shape of the 2026-07-10 incident where the product's own results
    comment resumed a merge-ready task into the budget gate. The repair is
    recorded as a state_repaired event carrying the displaced blocker.
    """
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            if t.status != TaskStatus.ESCALATED:
                console.print(f"[yellow]task is {t.status.value!r}, not "
                              "escalated — nothing to restore[/]")
                sys.exit(1)
            if not (t.context or {}).get("pr_watch"):
                console.print("[yellow]task has no open PR (pr_watch) — "
                              "restore-approval only repairs parked-PR tasks[/]")
                sys.exit(1)
            events = await store.list_events(t.id)
            if not any(e.get("kind") == "pr_open" for e in events):
                console.print("[yellow]no pr_open event on record — refusing[/]")
                sys.exit(1)
            import time as _time
            await store.save_events(t.id, [{
                "source": "human", "kind": "state_repaired",
                "text": f"escalated → awaiting_approval: {reason}; "
                        f"displaced blocker: {str(t.blocker)[:400]}",
                "ts": _time.time(),
            }])
            t.blocker = None
            t.wake_check_at = None
            await store.update_task(t)
            await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
            console.print(f"[green]{t.id[:8]} → awaiting_approval[/] "
                          f"(repair recorded)")

    asyncio.run(_go())


@task.command("cancel")
@click.argument("task_id")
@click.option("--reason", default="cancelled by user", help="Reason for cancelling.")
def task_cancel(task_id, reason):
    """Cancel a task (sets it to FAILED with reason)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            if t.status in {TaskStatus.DONE, TaskStatus.FAILED}:
                console.print(f"[yellow]task is already {t.status.value}[/]")
                return

            # Raise the stop flag first: it is the signal a running attempt sees.
            await store.request_cancel(t.id, reason)

            if _server_owns_worker(config) and t.status in _ACTIVE_STATES:
                # The attempt is mid-flight and the server owns the status. It
                # checkpoints the work and parks the task; forcing FAILED from
                # here would race it and throw the work away.
                console.print(
                    f"[yellow]cancel requested[/] {t.id[:8]} — the running attempt "
                    f"will checkpoint and stop within a few seconds, then park as "
                    f"blocked.\nMark it dead with [bold]nh task cancel {t.id[:8]}[/] "
                    f"again once it has stopped."
                )
                return

            t.context = await store.merge_context(
                t.id, {"cancel_reason": reason})
            await store.clear_cancel_request(t.id)
            await store.set_status(t, TaskStatus.FAILED, validate=False)
            console.print(f"[red]cancelled[/] {t.id[:8]} — reason: {reason}")

    asyncio.run(_go())


@task.command("retry")
@click.argument("task_id")
def task_retry(task_id):
    """Retry a failed task (resets to PENDING for a fresh run)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            if t.status != TaskStatus.FAILED:
                console.print(
                    f"[yellow]task is {t.status.value}[/] — only failed tasks "
                    f"can be retried. Use [bold]nh task resume[/] for parked tasks."
                )
                return
            t.blocker = None
            t.wake_check_at = None
            # None deletes the key (RFC 7396) — clears cancel_reason atomically.
            t.context = await store.merge_context(
                t.id, {"cancel_reason": None, "retried_at": _now_iso()})
            await store.update_task_columns(t)
            await store.clear_cancel_request(t.id)
            await store.set_status(t, TaskStatus.PENDING, validate=False)
            console.print(f"[green]retried[/] {t.id[:8]} → pending (will run on next dispatch)")

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Config management                                                            #
# --------------------------------------------------------------------------- #

@cli.command("config")
@click.argument("action", type=click.Choice(["show", "edit", "path"]))
@click.option("--key", default=None, help="Show a specific config key (dot-separated).")
def config_cmd(action, key):
    """Show, edit, or locate the config file.

    \b
      nh config show                # pretty-print full config
      nh config show --key git      # show just the git section
      nh config edit                # open in $EDITOR
      nh config path                # print the config file path
    """
    import yaml as _yaml
    from ..config import CONFIG_PATH as _cfg_path

    if action == "path":
        console.print(str(_cfg_path))
        return

    if action == "edit":
        editor = os.environ.get("EDITOR", "vi")
        import subprocess as _sp
        _sp.run([editor, str(_cfg_path)])
        return

    # action == "show"
    if not _cfg_path.exists():
        console.print(f"[yellow]no config file at {_cfg_path}[/]\n"
                       "Run [bold]nh init[/] to create one.")
        return
    data = _yaml.safe_load(_cfg_path.read_text()) or {}
    if key:
        parts = key.split(".")
        node = data
        for p in parts:
            if isinstance(node, dict) and p in node:
                node = node[p]
            else:
                console.print(f"[red]key not found:[/] {key}")
                return
        data = {key: node}
    console.print_json(data=data)


# --------------------------------------------------------------------------- #
# Auth profiles — which subscription pays                                      #
# --------------------------------------------------------------------------- #


@cli.group("auth")
def auth_group():
    """Which Claude subscription pays for a run.

    Tokens live only in ~/.no_human/.env (chmod 600). These commands print
    profile names and whether a token is present — never a token value.
    """


@auth_group.command("status")
def auth_status():
    """Show the active auth profile and which profiles have a token."""
    from ..config import (
        DEFAULT_AUTH_PROFILE,
        available_auth_profiles,
        profile_token_var,
    )

    # Deliberately not _bootstrap(): a diagnostic must still print when the
    # active profile's token is missing, which is exactly when it is needed.
    config = load_config()
    active = (config.get("llm") or {}).get("auth_profile") or DEFAULT_AUTH_PROFILE
    var = profile_token_var(active)
    available = available_auth_profiles()

    table = Table(show_header=False, box=None)
    table.add_row("active profile", f"[bold]{active}[/]")
    table.add_row("token variable", var)
    table.add_row(
        "token present", "[green]yes[/]" if active in available else "[red]no[/]"
    )
    table.add_row("profiles with a token", ", ".join(available) or "[dim]none[/]")
    console.print(table)

    if active not in available:
        console.print(
            f"\n[bold red]The active profile has no token.[/] Add [bold]{var}[/] "
            f"to ~/.no_human/.env, or switch:  [bold]nh auth use <profile>[/]"
        )
    if _server_owns_worker(config):
        console.print(
            "\n[dim]A server is running; it bills the profile it started with.[/]"
        )


@auth_group.command("use")
@click.argument("profile")
def auth_use(profile):
    """Pin the auth profile that future runs bill. Requires a server restart.

    \b
      nh auth use personal      # bills CLAUDE_CODE_OAUTH_TOKEN_PERSONAL
      nh auth use default       # bills the unsuffixed CLAUDE_CODE_OAUTH_TOKEN
    """
    from ..config import available_auth_profiles, profile_token_var, set_auth_profile

    normalized = profile.strip().lower()
    available = available_auth_profiles()
    if normalized not in available:
        console.print(
            f"[bold red]no token for profile[/] '{normalized}'. Expected "
            f"[bold]{profile_token_var(normalized)}[/] in ~/.no_human/.env.\n"
            f"Profiles with a token: {', '.join(available) or 'none'}"
        )
        sys.exit(2)

    try:
        set_auth_profile(normalized)
    except AuthError as exc:
        console.print(f"[bold red]auth error:[/] {exc}")
        sys.exit(2)

    console.print(f"[green]✓[/] auth profile set to [bold]{normalized}[/]")
    if _server_owns_worker(load_config()):
        console.print(
            "[yellow]The running server still bills its startup profile.[/] "
            "Restart it (`nh stop && nh start`) for this to take effect — a "
            "live task is never re-billed mid-run."
        )


# --------------------------------------------------------------------------- #
# Rules management (Phase G)                                                   #
# --------------------------------------------------------------------------- #


@cli.group("rules")
def rules_group():
    """Manage the confirmed rule set (anti-patterns + constraints)."""


@rules_group.command("list")
def rules_list():
    """List all confirmed rules."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            from ..learning import TYPE_RULE, TYPE_ANTI_PATTERN
            items = await store.list_memories(confirmed=True, mem_type=TYPE_RULE)
            items += await store.list_memories(confirmed=True, mem_type=TYPE_ANTI_PATTERN)
            if not items:
                console.print("[dim]no confirmed rules yet[/]\n"
                              "Add one: [bold]nh rules add --title '...' --content '...'[/]")
                return
            table = Table(title="Confirmed rules")
            table.add_column("id", style="dim", no_wrap=True)
            table.add_column("type")
            table.add_column("title")
            table.add_column("tags", style="dim")
            for m in items:
                import json as _json
                tags = ", ".join(_json.loads(m.get("tags") or "[]"))
                table.add_row(m["id"][:8], m["type"], m["title"][:60], tags[:40])
            console.print(table)

    asyncio.run(_go())


@rules_group.command("add")
@click.option("--title", required=True, help="Short rule title.")
@click.option("--content", required=True, help="Rule content / description.")
@click.option("--tag", multiple=True, help="Tags (can be repeated).")
@click.option("--project", default=None, help="Repo path this rule applies to.")
def rules_add(title, content, tag, project):
    """Add a confirmed rule directly (skips the learning queue)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            from ..learning import TYPE_RULE
            mem_id = await store.add_memory(
                mem_type=TYPE_RULE, title=title, content=content,
                tags=list(tag), project=project,
                source="manual", confirmed=True,
            )
            if mem_id:
                console.print(f"[green]added[/] rule {mem_id[:8]}: {title}")
            else:
                console.print("[yellow]duplicate — a rule with the same content exists[/]")

    asyncio.run(_go())


@rules_group.command("remove")
@click.argument("rule_id")
def rules_remove(rule_id):
    """Remove a rule by ID prefix."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            m = await store.find_memory(rule_id)
            if not m:
                console.print(f"[red]no rule matching[/] {rule_id}")
                sys.exit(1)
            await store.delete_memory(m["id"])
            console.print(f"[red]removed[/] {m['id'][:8]}: {m['title']}")

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Playbooks (1.4) — reusable procedures injected when a task matches           #
# --------------------------------------------------------------------------- #

@cli.group("playbook")
def playbook_group():
    """Manage operator playbooks (Procedure / Postconditions / Forbidden /
    Required). A playbook is injected into the coder prompt only when one of its
    trigger keywords appears in the task text."""


@playbook_group.command("list")
def playbook_list():
    """List all playbooks."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            items = await store.list_playbooks()
            if not items:
                console.print("[dim]no playbooks yet[/]\nAdd one: [bold]nh "
                              "playbook add --title '...' --trigger stripe "
                              "--procedure '...' --postcondition '...'[/]")
                return
            import json as _json
            table = Table(title="Playbooks")
            table.add_column("id", style="dim", no_wrap=True)
            table.add_column("title")
            table.add_column("triggers", style="dim")
            table.add_column("project", style="dim")
            for p in items:
                trg = ", ".join(_json.loads(p.get("trigger_keywords") or "[]"))
                table.add_row(p["id"][:8], p["title"][:50], trg[:40],
                              (p.get("project") or "global")[:30])
            console.print(table)

    asyncio.run(_go())


@playbook_group.command("add")
@click.option("--title", required=True, help="Short playbook title.")
@click.option("--trigger", "trigger", multiple=True,
              help="Keyword that triggers this playbook (repeatable). No "
                   "trigger = never auto-injected.")
@click.option("--procedure", default="", help="Step-by-step procedure.")
@click.option("--postcondition", "postcondition", multiple=True,
              help="A condition that must be TRUE when done (repeatable).")
@click.option("--forbidden", "forbidden", multiple=True,
              help="A forbidden action / hard stop (repeatable).")
@click.option("--require", "require", multiple=True,
              help="Something required from the operator up front (repeatable).")
@click.option("--project", default=None, help="Repo path to scope to (else global).")
def playbook_add(title, trigger, procedure, postcondition, forbidden, require, project):
    """Add an operator playbook."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            pb_id = await store.add_playbook(
                title=title, trigger_keywords=list(trigger), procedure=procedure,
                postconditions=list(postcondition), forbidden=list(forbidden),
                required_from_user=list(require), project=project,
            )
            console.print(f"[green]added[/] playbook {pb_id[:8]}: {title}")
            if not trigger:
                console.print("[yellow]note:[/] no --trigger given, so this "
                              "playbook will never auto-inject. Add one to activate it.")

    asyncio.run(_go())


@playbook_group.command("remove")
@click.argument("playbook_id")
def playbook_remove(playbook_id):
    """Remove a playbook by ID prefix."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            if await store.delete_playbook(playbook_id):
                console.print(f"[red]removed[/] playbook {playbook_id}")
            else:
                console.print(f"[red]no playbook matching[/] {playbook_id}")
                sys.exit(1)

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Stacked-PR ordered merge (2.2) — operator-invoked; the agent never merges     #
# --------------------------------------------------------------------------- #

@cli.group("merge-stack")
def merge_stack_group():
    """Merge a chain of DEPENDENT PRs in the correct order. Record edges with
    `link`, see the order with `plan`, execute with `run`. The agent never
    merges: it opens the PRs and stops. YOU run this."""


@merge_stack_group.command("link")
@click.argument("child_pr")
@click.argument("parent_pr")
@click.option("--project", default=None, help="Repo path scope.")
def merge_stack_link(child_pr, parent_pr, project):
    """Record that CHILD_PR must merge AFTER PARENT_PR."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            await store.add_pr_edge(child_pr=child_pr, parent_pr=parent_pr,
                                    project=project)
            console.print(f"[green]linked[/] {child_pr}\n  ⤷ merges after {parent_pr}")

    asyncio.run(_go())


@merge_stack_group.command("plan")
@click.option("--project", default=None, help="Repo path scope.")
def merge_stack_plan(project):
    """Show the safe merge order and which PRs are ready right now."""
    from ..vcs.merge_order import MergeCycle, merge_order, ready_to_merge
    from ..vcs.pr_watcher import default_pr_merged
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            edges = await store.list_pr_edges(project=project)
            if not edges:
                console.print("[dim]no PR edges — link some with "
                              "[bold]nh merge-stack link <child> <parent>[/][/]")
                return
            try:
                order = merge_order(edges)
            except MergeCycle as exc:
                console.print(f"[red]cannot order:[/] {exc}")
                sys.exit(1)
            merged = set()
            for pr in order:
                if await default_pr_merged(pr):
                    merged.add(pr)
            ready = set(ready_to_merge(edges, merged))
            console.rule("[bold]merge order")
            for i, pr in enumerate(order, 1):
                if pr in merged:
                    tag = "[green]merged[/]"
                elif pr in ready:
                    tag = "[bold yellow]READY[/]"
                else:
                    tag = "[dim]blocked (parent not merged)[/]"
                console.print(f"  {i}. {pr}  {tag}")

    asyncio.run(_go())


@merge_stack_group.command("run")
@click.option("--project", default=None, help="Repo path scope.")
@click.option("--squash", is_flag=True, help="Squash-merge (else a merge commit).")
@click.confirmation_option(prompt="Merge the READY PRs in order now?")
def merge_stack_run(project, squash):
    """Merge the currently-ready PRs (parents merged) in topological order via
    `gh`. Stops at the first PR that isn't cleanly mergeable (e.g. needs a
    rebase) and reports it. Operator action — never run by the agent."""
    import subprocess
    from ..vcs.merge_order import MergeCycle, merge_order, ready_to_merge
    from ..vcs.pr_watcher import default_pr_merged, parse_pr_url
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            edges = await store.list_pr_edges(project=project)
            if not edges:
                console.print("[dim]no PR edges to merge[/]")
                return
            try:
                order = merge_order(edges)
            except MergeCycle as exc:
                console.print(f"[red]cannot order:[/] {exc}")
                sys.exit(1)
            merged = {pr for pr in order if await default_pr_merged(pr)}
            method = "--squash" if squash else "--merge"
            for pr in order:  # topo order → a parent is always attempted first
                if pr in merged:
                    continue
                if pr not in set(ready_to_merge(edges, merged)):
                    console.print(f"[dim]stop:[/] {pr} is blocked (parent not merged)")
                    break
                parsed = parse_pr_url(pr)
                if not parsed:
                    console.print(f"[red]skip[/] unparseable PR: {pr}")
                    break
                console.print(f"[bold]merging[/] {pr} …")
                proc = subprocess.run(["gh", "pr", "merge", pr, method], capture_output=True, text=True)
                if proc.returncode != 0:
                    console.print(f"[red]merge failed[/] (needs a rebase or CI is "
                                  f"red): {proc.stderr.strip()[:200]}")
                    console.print("  resolve it, then re-run `nh merge-stack run`.")
                    break
                merged.add(pr)
                await store.delete_pr_edges_for(pr)
                console.print(f"  [green]merged[/] {pr}")

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Skills management (Phase G)                                                  #
# --------------------------------------------------------------------------- #


@cli.group("skills")
def skills_group():
    """Manage the confirmed skill set (reusable approaches)."""


@skills_group.command("list")
def skills_list():
    """List all confirmed skills."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            from ..learning import TYPE_SKILL, TYPE_FACT
            items = await store.list_memories(confirmed=True, mem_type=TYPE_SKILL)
            items += await store.list_memories(confirmed=True, mem_type=TYPE_FACT)
            if not items:
                console.print("[dim]no confirmed skills yet[/]\n"
                              "Add one: [bold]nh skills add --title '...' --content '...'[/]")
                return
            table = Table(title="Confirmed skills")
            table.add_column("id", style="dim", no_wrap=True)
            table.add_column("type")
            table.add_column("title")
            table.add_column("tags", style="dim")
            for m in items:
                import json as _json
                tags = ", ".join(_json.loads(m.get("tags") or "[]"))
                table.add_row(m["id"][:8], m["type"], m["title"][:60], tags[:40])
            console.print(table)

    asyncio.run(_go())


@skills_group.command("add")
@click.option("--title", required=True, help="Short skill title.")
@click.option("--content", required=True, help="Skill content / how-to.")
@click.option("--tag", multiple=True, help="Tags (can be repeated).")
@click.option("--project", default=None, help="Repo path this skill applies to.")
def skills_add(title, content, tag, project):
    """Add a confirmed skill directly (skips the learning queue)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            from ..learning import TYPE_SKILL
            mem_id = await store.add_memory(
                mem_type=TYPE_SKILL, title=title, content=content,
                tags=list(tag), project=project,
                source="manual", confirmed=True,
            )
            if mem_id:
                console.print(f"[green]added[/] skill {mem_id[:8]}: {title}")
            else:
                console.print("[yellow]duplicate — a skill with the same content exists[/]")

    asyncio.run(_go())


@skills_group.command("remove")
@click.argument("skill_id")
def skills_remove(skill_id):
    """Remove a skill by ID prefix."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            m = await store.find_memory(skill_id)
            if not m:
                console.print(f"[red]no skill matching[/] {skill_id}")
                sys.exit(1)
            await store.delete_memory(m["id"])
            console.print(f"[red]removed[/] {m['id'][:8]}: {m['title']}")

    asyncio.run(_go())


@skills_group.command("propose")
@click.option("--title", required=True, help="Short skill title.")
@click.option("--content", required=True, help="Skill content / how-to.")
@click.option("--tag", multiple=True, help="Tags (can be repeated).")
@click.option("--project", default=None, help="Repo path this skill applies to.")
def skills_propose(title, content, tag, project):
    """Propose a skill discovered mid-task — for the agent to call via Bash.

    Queued exactly like `nh learnings`' post-task proposals (source=proposed,
    confirmed=False): never auto-trusted, never delivered to any task until a
    human runs `nh learnings --confirm`. This only widens WHO can propose
    (mid-task, not just post-task) — it does not weaken the confirm gate.
    """
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            from ..learning import TYPE_SKILL
            mem_id = await store.add_memory(
                mem_type=TYPE_SKILL, title=title, content=content,
                tags=list(tag), project=project,
                source="proposed", confirmed=False,
            )
            if mem_id:
                console.print(
                    f"[yellow]proposed[/] skill {mem_id[:8]}: {title}\n"
                    f"Queued for human review — confirm with "
                    f"[bold]nh learnings --confirm {mem_id[:8]}[/]"
                )
            else:
                console.print("[yellow]duplicate — a matching proposal already exists[/]")

    asyncio.run(_go())


@cli.command("watch")
@click.argument("task_id")
def watch(task_id):
    """Run a staged task in the live Textual TUI (Claude-Code-like view)."""
    config, _ = _bootstrap()
    from .tui import run_watch  # lazy import: Textual is heavy
    run_watch(config, task_id)


@cli.command("onboard")
@click.argument("repo", type=click.Path(exists=True))
@click.option("--confirm", is_flag=True,
              help="Confirm the proven profile (the one-click human gate).")
@click.option("--agent", is_flag=True,
              help="Use the agentic read-only recon deriver (for nonstandard repos).")
def onboard(repo, confirm, agent):
    """Derive a repo's install/test/lint commands from its OWN declarations and
    PROVE them by running each, then propose a ProjectProfile for your confirm.

    A profile drives a task only once you confirm it AND its test command was
    proven to run:
      nh onboard ~/repo            # derive + prove + propose
      nh onboard ~/repo --confirm  # confirm the proven profile
    """
    # Auth (subscription) is only needed for the agentic deriver, which runs the
    # backend; the deterministic deriver + subprocess proving need no token.
    config, _ = _bootstrap(require_auth=agent)
    repo_path = str(Path(repo).resolve())
    from ..onboard import AgentDeriver, DeclarationDeriver, OnboardEngine
    from ..profile import ProjectProfile

    async def _go():
        async with Store(config.db_path) as store:
            if confirm:
                prof = await store.get_profile(repo_path) or ProjectProfile.load(repo_path)
                if not prof:
                    console.print("[red]no profile to confirm[/] — run "
                                  f"[bold]nh onboard {repo}[/] first")
                    sys.exit(1)
                if not prof.proven.get("test_cmd"):
                    console.print(
                        "[red]cannot confirm:[/] the test command is not proven "
                        f"(test_cmd={prof.test_cmd!r}). Re-run onboarding until it "
                        "runs clean — trust requires proof."
                    )
                    sys.exit(1)
                prof.confirmed = True
                prof.save()
                await store.upsert_profile(prof)
                console.print(f"[bold green]confirmed[/] profile for {repo_path}")
                console.print(f"  usable: {prof.is_usable}  test: [bold]{prof.test_cmd}[/]")
                return

            if agent:
                # Recon must not mutate the repo: a read-only backend so its
                # PreToolUse guard blocks all write tools.
                recon_backend = ClaudeBackend(
                    model=config.primary_model,
                    forbidden_paths=config["safety"]["forbidden_paths"],
                    never_push_to=config["git"]["never_push_to"],
                    readonly=True,
                )
                deriver = AgentDeriver(recon_backend)
            else:
                deriver = DeclarationDeriver()
            console.rule(f"[bold]onboarding {repo_path}")
            console.print(f"[blue]deriving[/] commands from the repo's declarations"
                          f"{' (agentic recon)' if agent else ''} …")
            github_hosts = config["git"].get("github_hosts", ["github.com"])
            result = await OnboardEngine(deriver, github_hosts=github_hosts).onboard(repo_path)
            prof = result.profile

            console.print(f"\n[bold]ecosystem:[/] {prof.ecosystem or '[dim]unknown[/]'}"
                          f"   [dim]derived from: {', '.join(prof.derived_from) or '—'}[/]")
            if prof.vcs_host:
                console.print(f"[bold]vcs:[/] {prof.vcs_host}  [dim]{prof.vcs_remote}[/]")
            console.print("[bold]proving (running each candidate):[/]")
            for p in result.proofs:
                icon = "[green]✓[/]" if p.ok else "[red]✗[/]"
                console.print(f"  {icon} {p.summary}")
            console.print("\n[bold]proposed profile:[/]")
            for label, val in (("install", prof.install_cmd), ("test", prof.test_cmd),
                               ("lint", prof.lint_cmd)):
                proven = prof.proven.get(f"{label}_cmd")
                tag = "[green](proven)[/]" if proven else "[dim](unproven)[/]" if val else ""
                console.print(f"  {label}: {val or '[dim]—[/]'} {tag}")
            if prof.ci:
                console.print(f"  ci: {prof.ci}")
            if prof.human_gated_steps:
                console.print(f"  human-gated: {prof.human_gated_steps}")

            # Credential preflight (WS-F): show which .env keys this repo needs
            # and which are still missing — never the values.
            if prof.required_credentials:
                from ..config import credential_status
                status = credential_status(prof.required_credentials)
                console.print("\n[bold]required credentials[/] (~/.no_human/.env):")
                missing = []
                for key in prof.required_credentials:
                    ok = status.get(key)
                    icon = "[green]✓[/]" if ok else "[red]✗ missing[/]"
                    console.print(f"  {icon} {key}")
                    if not ok:
                        missing.append(key)
                if missing:
                    console.print(f"  [yellow]set {len(missing)} missing key(s) in "
                                  "~/.no_human/.env (chmod 600) before running tasks "
                                  "that need them.[/]")

            prof.save()
            await store.upsert_profile(prof)
            if prof.proven.get("test_cmd"):
                console.print(f"\n[green]test command proven.[/] confirm to make it "
                              f"usable:\n  [bold]nh onboard {repo} --confirm[/]")
            else:
                console.print("\n[yellow]test command NOT proven[/] — profile is not "
                              "usable until it runs clean. Nothing faked; fix the repo "
                              "or its declarations and re-run.")

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Docs generation                                                              #
# --------------------------------------------------------------------------- #


@cli.group("docs")
def docs_group() -> None:
    """Manage auto-generated repo wiki docs."""


@docs_group.command("generate")
@click.argument("repo", type=click.Path(exists=True))
def docs_generate(repo):
    """Generate architecture/modules/conventions wiki for a repo.

    Writes .no_human/wiki/*.md and a pointer block in CLAUDE.md.
    Uses a bounded Agent SDK session (max 12 turns, read-only).

    \b
    Examples:
      nh docs generate ~/git/myrepo
    """
    config, _ = _bootstrap()
    repo_path = str(Path(repo).resolve())
    from ..docs_gen import WikiGenerator
    from ..profile import ProjectProfile

    async def _go():
        backend = ClaudeBackend(
            model=config.primary_model,
            forbidden_paths=config["safety"]["forbidden_paths"],
        )
        gen = WikiGenerator(backend, max_turns=12)
        console.print(f"[bold]generating wiki for[/] {repo_path} …")
        result = await gen.generate(repo_path)
        if result.error:
            console.print(f"[red]error:[/] {result.error}")
            sys.exit(1)
        for f in result.files_written:
            console.print(f"  [green]✓[/] {f}")
        console.print(f"  [green]✓[/] CLAUDE.md (wiki pointer)")
        # Persist wiki_commit to profile.
        profile = ProjectProfile.load(repo_path)
        if profile and result.commit_sha:
            profile.wiki_commit = result.commit_sha
            profile.save()
            console.print(f"  wiki_commit → {result.commit_sha[:8]}")

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Enterprise CI integration validation (M6)                                          #
# --------------------------------------------------------------------------- #


@cli.group("ci-gate")
def ci_gate_group() -> None:
    """Enterprise CI integration validation (post-PR gate)."""


@ci_gate_group.command("run")
@click.argument("task_id")
@click.option("--poll-interval", type=int, default=None,
              help="Seconds between status polls (default: ci_gate.poll_interval).")
@click.option("--namespace", default=None,
              help="Override the target namespace for this run (default: "
                   "ci_gate.namespace_template). Use when the templated "
                   "namespace is occupied by a stale prior run.")
def ci_gate_run(task_id, poll_interval, namespace):
    """Run the Enterprise CI integration validation for TASK_ID's open PR, now.

    Drives the SAME WakeWatcher rung the server uses — trigger once per PR
    head with the duplicate guards, poll to terminal, post the results
    comment on the PR, and apply the verdict (pass → still awaiting your
    merge; fail → feedback to the coder / escalation). ci_gate.enabled is
    forced ON for this invocation only — running the command is the consent.
    """
    import copy

    config, _ = _bootstrap(require_auth=False)
    cfg = copy.deepcopy(config.data)
    cfg.setdefault("ci_gate", {})["enabled"] = True
    if namespace:
        # A literal namespace formats to itself (no {pr_number} placeholder).
        cfg["ci_gate"]["namespace_template"] = namespace
    interval = poll_interval or int(cfg["ci_gate"].get("poll_interval", 30) or 30)
    from ..blockers import WakeWatcher

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            url = (t.context or {}).get("pr_watch")
            if not url:
                console.print(f"[red]task {t.id[:8]} has no pr_watch URL[/]")
                sys.exit(1)
            watcher = WakeWatcher(
                store, cfg,
                on_event=lambda k, txt: console.print(f"[blue]● {k}[/] {txt}"),
            )
            if watcher._ci_gate_gate is None:
                console.print("[red]Enterprise CI gate wiring failed[/] (see logs)")
                sys.exit(1)
            console.print(f"[bold]Enterprise CI validation[/] for {t.id[:8]} — {url}")
            while True:
                t = await store.get_task(t.id)
                outcome, action = await watcher._ci_gate_step(t, url)
                if outcome is None:
                    console.print("[red]gate step failed[/] (see logs)")
                    sys.exit(1)
                if outcome.action == "skip":
                    console.print(f"[yellow]nothing to do:[/] {outcome.reason}")
                    return
                if action == "ci_gate_passed":
                    console.print(f"[bold green]Enterprise CI integration PASSED[/] — "
                                  f"{outcome.web_url}")
                    return
                if action in ("escalated_ci_gate", "escalated_ci_gate_refused"):
                    console.print(f"[bold red]escalated:[/] {outcome.reason}")
                    sys.exit(1)
                if action == "resumed":
                    console.print(
                        "[bold red]Enterprise CI integration FAILED[/] — failure fed "
                        "back to the coder (task resumed).")
                    sys.exit(1)
                # triggered / waiting / blocked → keep going.
                await asyncio.sleep(interval)

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Blocker handling (PLAN.md Part 22)                                          #
# --------------------------------------------------------------------------- #

_PARKED_STATES = (
    TaskStatus.BLOCKED, TaskStatus.AWAITING_INPUT,
    TaskStatus.PAUSED_QUOTA, TaskStatus.ESCALATED,
)


@cli.command("blocked")
@click.option("--full/--summary", default=False, help="Show the full 6-part report.")
def blocked(full):
    """List parked/escalated tasks with the one question each needs answered."""
    config, _ = _bootstrap(require_auth=False)
    from ..blockers import Blocker, render_report

    async def _go():
        async with Store(config.db_path) as store:
            found = False
            for state in _PARKED_STATES:
                for t in await store.list_tasks(state):
                    found = True
                    b = Blocker.from_dict(t.blocker) if t.blocker else None
                    cat = b.category.value if b else "?"
                    console.print(
                        f"[bold]{t.id[:8]}[/] [yellow]{t.status.value}[/] "
                        f"[magenta]{cat}[/] — {t.title}"
                    )
                    if b and b.question:
                        console.print(f"  [cyan]Q:[/] {b.question}")
                        for i, opt in enumerate(b.options, 1):
                            hint = " [dim](applies a change)[/]" if opt.action else ""
                            console.print(f"     [{i}] {opt.label}{hint}")
                    if b and b.wake_condition:
                        console.print(f"  [dim]wake: {b.wake_condition}[/]")
                    if full and b:
                        console.print(render_report(b, task_title=t.title, task_id=t.id))
                    console.print(
                        f"  [dim]reply:[/] nh reply {t.id[:8]} \"<answer>\""
                    )
            if not found:
                console.print("[green]no blocked tasks[/]")

    asyncio.run(_go())


@cli.command("reply")
@click.argument("task_id")
@click.argument("answer", required=False)
@click.option("--choose", type=int, default=None, metavar="N",
              help="Answer with the blocker's option N (1-based), applying its action.")
@click.option("--run/--no-run", default=True, help="Resume the task now (default).")
def reply(task_id, answer, choose, run):
    """Answer a blocked task's question and resume it from its checkpoint."""
    if (answer is None) == (choose is None):
        raise click.UsageError("give an ANSWER or --choose N, not both")
    config, _ = _bootstrap(require_auth=run)
    from ..blockers import (
        ActionError,
        Blocker,
        apply_action,
        is_terminal_action,
        resume_checkpoint,
    )

    async def _go():
        nonlocal answer
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            if t.status not in _PARKED_STATES:
                console.print(
                    f"[yellow]task is {t.status.value}, not blocked[/] — nothing to resume"
                )
                return
            b = Blocker.from_dict(t.blocker) if t.blocker else None
            question = b.question if b else None

            # Picking an option is the only way an action ever runs, and it only
            # runs here, on a human's instruction.
            applied = None
            terminal = False
            if choose is not None:
                if not b or not b.options:
                    console.print("[red]this blocker offers no options[/]")
                    sys.exit(1)
                if not 1 <= choose <= len(b.options):
                    console.print(f"[red]--choose must be between 1 and {len(b.options)}[/]")
                    sys.exit(1)
                option = b.options[choose - 1]
                answer = option.label
                terminal = is_terminal_action(option.action)
                try:
                    applied = apply_action(t, option.action)
                except ActionError as exc:
                    console.print(f"[red]cannot apply that option:[/] {exc}")
                    sys.exit(1)
                if applied:
                    console.print(f"[green]applied[/] {applied}")

            await store.append_context_list(t.id, "human_replies", {
                "at": _now_iso(), "question": question, "answer": answer,
                "applied": applied,
            })

            # 2.3 (CodeRabbit learnings): if the reply states a reusable
            # preference/rule, propose it to the HUMAN-CONFIRMED learning queue
            # (confirmed=False — never auto-active) so future reviews apply it.
            if answer:
                from ..history.analyzer import mine_reply
                mined = mine_reply(answer)
                if mined:
                    category, desc = mined
                    proposed = await store.add_memory(
                        mem_type=category,
                        title=f"{desc} (from a review reply)"[:120],
                        content=answer, source="reply", confirmed=False,
                        project=t.repo_path,
                        tags=["reply", category, "user_correction"],
                        dedupe_key=f"reply:{answer[:80]}",
                    )
                    if proposed:
                        console.print(f"[dim]captured a learning from your reply "
                                      f"({category}) — confirm with `nh learnings`[/]")
            # A terminal option (SCRUM-22: "stop — keep the work parked as-is")
            # means exactly that: record the answer, apply nothing else, and
            # LEAVE the task in its parked state. Resuming here is what
            # silently inverted the human's explicit stop.
            if terminal:
                await store.update_task_columns(t)
                console.print(f"[yellow]kept parked[/] {t.id[:8]} — "
                              "work preserved as-is; nothing will resume it")
                return
            # Continue from the [WIP-BLOCKED] checkpoint instead of re-doing the
            # work from base. The blocker promised "Resume with: nh reply …".
            checkpoint = resume_checkpoint(t.blocker)
            if checkpoint:
                await store.merge_context(t.id, {"resume_from": checkpoint})
            t.context = await store.merge_context(t.id, {})
            t.wake_check_at = None
            await store.update_task_columns(t)
            # Answering a blocker withdraws any pending stop (a task paused by
            # `nh task pause` is resumed by answering it).
            await store.clear_cancel_request(t.id)
            # Resume into the working loop from the [WIP-BLOCKED] checkpoint.
            await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
            console.print(f"[green]resumed[/] {t.id[:8]} with your answer")
            if not run:
                console.print(f"run it with:  [bold]nh watch {t.id[:8]}[/]")
                return
            if _server_owns_worker(config):
                # The task is IMPLEMENTING, which the server's scheduler claims.
                # Running it here too would put two orchestrators on one checkout.
                console.print(
                    "[cyan]the running server picked it up[/] — "
                    f"watch it with: [bold]nh watch {t.id[:8]}[/]"
                )
                return
            console.rule(f"[bold]resuming {t.id[:8]}")
            async with EventPersister(store, t.id) as persister:
                orch = _build_orchestrator(
                    config, store,
                    event_sink=_persisting(persister, t.id, render_event), task=t)
                outcome = await orch.run_task(t)
            console.rule(f"[bold]{outcome.status.value}")
            if outcome.pr_url:
                console.print(f"[bold green]PR:[/] {outcome.pr_url}")
            console.print(outcome.detail)

    asyncio.run(_go())


@cli.command("wake")
@click.option("--loop", is_flag=True, help="Poll continuously at wake_poll_interval.")
def wake(loop):
    """Run the wake-condition watcher: resume parked tasks whose condition fired,
    escalate tasks parked past max_park_duration (Part 22.7).

    Time-based (`after:`, `quota_refreshed`) and timeout conditions resolve out of
    the box. `pr_merged:` / `ci_green_on:` resolve via gh/glab when available.
    Run once (cron-friendly) or with --loop.
    """
    config, _ = _bootstrap(require_auth=False)
    from ..blockers import WakeWatcher, parse_duration
    from ..vcs.pr_watcher import (
        check_pr_comments, default_ci_log_excerpt, default_pr_checks,
        default_pr_merged, default_pr_state,
    )

    async def _tick_once(store):
        watcher = WakeWatcher(
            store, config.data,
            pr_merged=default_pr_merged, pr_comment=check_pr_comments,
            pr_state=default_pr_state, pr_checks=default_pr_checks,
            ci_log=default_ci_log_excerpt,
            on_event=lambda kind, text: console.print(f"[blue]● {kind}[/] {text}"),
        )
        actions = await watcher.tick()
        if not actions:
            console.print("[dim]no parked tasks ready[/]")
        return actions

    async def _go():
        async with Store(config.db_path) as store:
            if not loop:
                await _tick_once(store)
                return
            interval = parse_duration(
                str(config.data.get("blockers", {}).get("wake_poll_interval", "10m")))
            secs = int(interval.total_seconds()) if interval else 600
            console.print(f"[dim]watching parked tasks every {secs}s (ctrl-c to stop)[/]")
            import asyncio as _a
            while True:
                await _tick_once(store)
                await _a.sleep(secs)

    asyncio.run(_go())


async def _jira_poll_loop(poller, stop, poll_interval: int) -> None:
    """Tick the Jira poller every ``poll_interval`` seconds until ``stop`` is set.
    Mirrors Scheduler.run_forever's stop-aware wait so ctrl-c stays responsive."""
    while not stop.is_set():
        try:
            await poller.tick()
        except Exception as exc:  # noqa: BLE001 — never kill serve on a Jira hiccup
            console.print(f"[red]Jira poll error[/] {exc}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass


@cli.command("serve")
@click.option("--max-workers", type=int, default=None,
              help="Run the pool with this many workers for this invocation, "
                   "enabling worktree isolation even if concurrency.enabled is "
                   "false in config (config on disk is left untouched).")
def serve(max_workers):
    """Run the concurrent scheduler daemon (Phase 7): drain pending + resumed
    tasks into a bounded worker pool, each task in its own git worktree, running
    the wake-watcher in the same loop. Ctrl-C to stop (drains in-flight tasks).

    Requires concurrency.enabled in config, or pass --max-workers N to enable
    the pool (with worktree isolation) for this run only.
    """
    config, _ = _bootstrap()
    _assert_backend_usable()
    from ..blockers import WakeWatcher, parse_duration
    from ..core.scheduler import Scheduler, resolve_serve_pool

    conc = config.data.setdefault("concurrency", {})
    workers, enabled, error = resolve_serve_pool(config.data, cli_workers=max_workers)
    if error:
        console.print(f"[yellow]{error}[/]")
        sys.exit(1)
    if max_workers is not None:
        # An explicit flag enables the pool + worktree isolation for this
        # invocation only — the config default on disk is left untouched.
        conc["enabled"] = True
        conc["max_workers"] = workers
    interval = parse_duration(str(conc.get("poll_interval", "10s")))
    secs = int(interval.total_seconds()) if interval else 10

    async def _go():
        async with Store(config.db_path) as store:
            from ..vcs.pr_watcher import (
        check_pr_comments, default_ci_log_excerpt, default_pr_checks,
        default_pr_merged, default_pr_state,
    )
            watcher = WakeWatcher(
                store, config.data,
                pr_merged=default_pr_merged, pr_comment=check_pr_comments,
            pr_state=default_pr_state, pr_checks=default_pr_checks,
            ci_log=default_ci_log_excerpt,
                on_event=lambda k, t: console.print(f"[blue]● {k}[/] {t}"))
            # PR-E: periodic re-analysis job (EVOLUTION_PLAN Phase 9).
            reanalysis = None
            ra_cfg = config.data.get("reanalysis", {})
            if ra_cfg.get("enabled", True):
                from ..core.scheduler import ReanalysisJob
                reanalysis = ReanalysisJob(
                    store,
                    interval_seconds=float(ra_cfg.get("interval_seconds", 86400)),
                    days=int(ra_cfg.get("days", 30)),
                    max_proposals_per_run=int(ra_cfg.get("max_proposals", 20)),
                )
            # M-A: background repo-wiki refresh (docs_gen). Opt-in via
            # docs.auto_refresh — off by default so serve incurs no unattended
            # backend cost. Uses a read-only recon backend (write tools blocked).
            wiki_refresh = None
            docs_cfg = config.data.get("docs", {})
            if docs_cfg.get("auto_refresh", False):
                from ..core.scheduler import WikiRefreshJob
                recon_backend = ClaudeBackend(
                    model=config.primary_model,
                    forbidden_paths=config["safety"]["forbidden_paths"],
                    never_push_to=config["git"]["never_push_to"],
                    readonly=True,
                )
                wiki_refresh = WikiRefreshJob(
                    store, recon_backend,
                    interval_seconds=float(docs_cfg.get("refresh_interval_seconds", 3600)),
                    max_turns=int(docs_cfg.get("max_turns", 12)),
                )
                console.print("[green]wiki auto-refresh[/] enabled "
                              f"(every {docs_cfg.get('refresh_interval_seconds', 3600)}s)")
            sched = Scheduler(
                store, lambda task=None: _build_orchestrator(config, store, event_sink=render_event, task=task),
                max_workers=workers, wake_watcher=watcher,
                on_event=lambda k, t: console.print(f"[magenta]▸ {k}[/] {t}"),
                reanalysis_job=reanalysis,
                wiki_refresh_job=wiki_refresh,
                config=config.data)
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            import signal
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop.set)
                except NotImplementedError:  # pragma: no cover — non-unix
                    pass
            console.print(f"[green]serving[/] pool={workers} poll={secs}s "
                          "(ctrl-c to stop)")

            # Jira intake: poll the operator's JQL into tasks (+ opt-in status
            # write-back) on its own cadence, in the same loop. Opt-in via
            # integrations.jira.enabled.
            coros = [sched.run_forever(stop=stop, poll_interval=secs)]
            jira_cfg = (config.data.get("integrations") or {}).get("jira") or {}
            if jira_cfg.get("enabled"):
                from ..config import load_env_var
                from ..intake.jira import JiraAdapter
                from ..intake.jira_poll import JiraPoller
                load_env_var("JIRA_API_TOKEN")  # from ~/.no_human/.env into the process env
                jira_secs = max(60, int((parse_duration(str(jira_cfg.get("poll_interval", "5m")))
                                         or parse_duration("5m")).total_seconds()))
                poller = JiraPoller(
                    JiraAdapter(config.data), store, config=config.data,
                    on_event=lambda k, t: console.print(f"[cyan]◆ {k}[/] {t}"))
                console.print(f"[green]Jira intake[/] project={jira_cfg.get('project_key') or '?'} "
                              f"poll={jira_secs}s")
                coros.append(_jira_poll_loop(poller, stop, jira_secs))

            await asyncio.gather(*coros)
            console.print("[dim]drained; stopped[/]")

    asyncio.run(_go())


@cli.command("status")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit lane bucket counts as a JSON object to stdout.")
def status(as_json):
    """Show task counts by lane: queued (pending), running (in-flight stages),
    parked, and terminal. A quick portfolio read across all projects."""
    config, _ = _bootstrap(require_auth=False)
    needs_you = {TaskStatus.AWAITING_APPROVAL, TaskStatus.AWAITING_INPUT,
                  TaskStatus.ESCALATED}
    working = {TaskStatus.PENDING, TaskStatus.CONTEXT, TaskStatus.PLANNING,
               TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING, TaskStatus.TESTING}
    waiting = {TaskStatus.PAUSED_QUOTA}

    async def _go():
        async with Store(config.db_path) as store:
            tasks = await store.list_tasks()
            buckets = {"needs you": 0, "working": 0, "waiting": 0,
                       "failed": 0, "done": 0}
            for t in tasks:
                if t.status == TaskStatus.BLOCKED:
                    # Match the board: a blocked task auto-resolves (→ waiting)
                    # only with a wake condition; without one a human must act.
                    wake = (t.blocker or {}).get("wake_condition")
                    buckets["waiting" if wake else "needs you"] += 1
                elif t.status in needs_you:
                    buckets["needs you"] += 1
                elif t.status in working:
                    buckets["working"] += 1
                elif t.status in waiting:
                    buckets["waiting"] += 1
                elif t.status == TaskStatus.FAILED:
                    buckets["failed"] += 1
                elif t.status == TaskStatus.DONE:
                    buckets["done"] += 1
            if as_json:
                click.echo(json.dumps(buckets))
                return
            mw = config.data.get("concurrency", {}).get("max_workers", 1)
            console.print(
                f"[yellow]needs you[/] {buckets['needs you']}  "
                f"[bold]working[/] {buckets['working']}/{mw}  "
                f"[blue]waiting[/] {buckets['waiting']}  "
                f"[red]failed[/] {buckets['failed']}  "
                f"[green]done[/] {buckets['done']}")

    asyncio.run(_go())


@cli.command("autonomy")
@click.option("--days", default=None, type=int,
              help="Only include tasks created in the last N days.")
def autonomy(days):
    """Autonomy telemetry (megaplan P0): how often a human is pulled in
    mid-flight vs. tasks reaching a reviewable PR. The North Star is a
    touchpoint rate near zero — the only human steps are starting the site
    and reviewing/merging the final PR."""
    config, _ = _bootstrap(require_auth=False)
    from ..core.autonomy import compute_autonomy_metrics

    def _pct(x: float | None) -> str:
        return f"{x:.0%}" if x is not None else "n/a"

    async def _go():
        async with Store(config.db_path) as store:
            rep = await compute_autonomy_metrics(store, days=days)
            window = f"last {days}d" if days else "all time"
            console.rule(f"[bold]autonomy — {window}")
            if rep.settled_tasks == 0:
                console.print("[dim]no settled tasks yet[/]")
                return
            console.print(
                f"[green]PR-reached[/] {rep.pr_reached}/{rep.settled_tasks} "
                f"({_pct(rep.pr_reached_rate)})   "
                f"[yellow]mid-flight touchpoints[/] {rep.touchpoint_tasks}/"
                f"{rep.settled_tasks} ({_pct(rep.touchpoint_rate)})")
            if rep.turn_exhaustion_empty:
                console.print(
                    f"[red]turn-exhaustion empty-diff attempts[/] "
                    f"{rep.turn_exhaustion_empty}")
            if rep.by_status:
                table = Table(title="tasks by status")
                table.add_column("status")
                table.add_column("count", justify="right")
                for status, n in sorted(rep.by_status.items(),
                                        key=lambda kv: -kv[1]):
                    table.add_row(status, str(n))
                console.print(table)
            if rep.blocker_categories:
                table = Table(title="blocker categories (pull a human in)")
                table.add_column("category")
                table.add_column("count", justify="right")
                for cat, n in sorted(rep.blocker_categories.items(),
                                     key=lambda kv: -kv[1]):
                    table.add_row(cat, str(n))
                console.print(table)

    asyncio.run(_go())


@cli.command("recall")
@click.argument("query")
@click.option("--limit", default=8, help="Max matches to show.")
def recall(query, limit):
    """Search past tasks, attempts, memories, and ingested history for prior
    work similar to QUERY — so the agent (via Bash: `nh recall <query>`) or a
    human can find how something like this was solved before.

    Plain keyword substring matching over what's already stored — agentic
    grep, not RAG (no embeddings, no new dependency, no index to keep fresh).
    """
    config, _ = _bootstrap(require_auth=False)
    terms = [t.lower() for t in query.split() if t]

    def _hit(*texts: str | None) -> bool:
        hay = " ".join(t for t in texts if t).lower()
        return bool(hay) and any(t in hay for t in terms)

    async def _go():
        async with Store(config.db_path) as store:
            rows: list[tuple[str, str, str]] = []  # (kind, id, summary)

            for task in await store.list_tasks():
                if not _hit(task.title, task.description):
                    continue
                attempts = await store.list_attempts(task.id)
                last = attempts[-1] if attempts else {}
                outcome = last.get("failure_reason") or task.status.value
                pr = next((a.get("pr_url") for a in reversed(attempts) if a.get("pr_url")), None)
                summary = f"{task.title}  ({task.status.value}: {outcome})"
                if pr:
                    summary += f"  {pr}"
                rows.append(("task", task.id[:8], escape(summary)))

            for mem in await store.list_memories():
                if not _hit(mem.get("title"), mem.get("content")):
                    continue
                rows.append(("memory", mem["id"][:8],
                            escape(f"({mem.get('type')}) {mem.get('content', '')[:100]}")))

            for h in await store.list_history_cache():
                if not _hit(h.get("title"), h.get("findings_json")):
                    continue
                rows.append(("history", h["cascade_id"][:8], escape(h.get("title") or "(untitled)")))

            if not rows:
                console.print(f"[dim]no matches for {query!r}[/]")
                return
            table = Table(title=f"recall: {query!r} ({len(rows)} match(es))")
            table.add_column("kind")
            table.add_column("id")
            table.add_column("summary")
            for kind, rid, summary in rows[:limit]:
                table.add_row(kind, rid, summary)
            console.print(table)
            if len(rows) > limit:
                console.print(f"[dim]…and {len(rows) - limit} more (raise --limit)[/]")

    asyncio.run(_go())


# The FOUR token groups an attempt records — coder, review, plan, utility —
# each with (used, cache_read, cache_creation). `Store.list_attempts` is
# SELECT *, so every one is already in hand.
_TOKEN_GROUPS = ("", "review_", "plan_", "utility_")
_TOKEN_KINDS = ("tokens_used", "cache_read_tokens", "cache_creation_tokens")


def _attempt_tokens(a: dict) -> "tuple[int | None, int | None]":
    """``(spend, burn)`` for one attempt row, or ``(None, None)`` when the
    CODER tokens are unknown.

    Both numbers are gated on ``tokens_used`` even though ``burn`` could in
    principle be computed without it: the coder columns are written at one
    point and the review columns by a separate later update, so a row can have
    a known burn and an unknown spend. Reporting a burn that silently excludes
    the coder would be a partial number presented as a total — the exact
    defect this function exists to prevent — so an unknown coder makes both
    unknown. Empirically moot (all 13 NULL rows have every other bucket at 0),
    and it under-claims rather than over-claims.

    spend — what the budget guard ENFORCES: ``tokens_used +
            cache_read_tokens`` on the CODER session only (orchestrator's
            ``spent = ...``; the lifetime ledger uses the same sum). It is the
            number an abort message quotes, so a line printed beside that
            message has to match it.
    burn  — every token the attempt actually consumed: all four groups, all
            three buckets. This is what ``web/src/cost.js`` ``taskBurn`` sums,
            so the CLI and the board cannot disagree.

    Keeping them separate is the point. ``tokens_used`` alone is NON-CACHE
    CODER tokens, which under-reported a live runaway by ~5500x (an attempt
    aborted at 4,054,229 displayed as 731). But the coder's own total is not
    the whole story either: on that same attempt the plan and utility sessions
    added 740,643 tokens — 15% of the tokens and 34% of the dollars — so
    presenting it as the total is the same defect one tier up. cost.js's own
    header records this repo shipping that mistake twice.
    """
    if a.get("tokens_used") is None:
        return None, None
    spend = int(a["tokens_used"]) + int(a.get("cache_read_tokens") or 0)
    burn = sum(int(a.get(f"{g}{k}") or 0)
               for g in _TOKEN_GROUPS for k in _TOKEN_KINDS)
    return spend, burn


@cli.command("agents")
@click.option("--all", "show_all", is_flag=True,
              help="Include recently completed agents (last 24h), not just active.")
def agents(show_all):
    """Show active agent sessions — tasks currently being worked by the agent."""
    config, _ = _bootstrap(require_auth=False)
    active_statuses = {
        TaskStatus.IMPLEMENTING, TaskStatus.PLANNING, TaskStatus.CONTEXT,
        TaskStatus.REVIEWING, TaskStatus.TESTING,
    }

    async def _go():
        async with Store(config.db_path) as store:
            tasks = await store.list_tasks()
            active = [t for t in tasks if t.status in active_statuses]
            recent: list[Task] = []
            if show_all:
                from datetime import datetime, timezone, timedelta
                cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                for t in tasks:
                    if t.status not in active_statuses and t.updated_at:
                        try:
                            updated = t.updated_at
                            if isinstance(updated, str):
                                updated = datetime.fromisoformat(updated)
                            if updated.tzinfo is None:
                                updated = updated.replace(tzinfo=timezone.utc)
                            if updated > cutoff:
                                recent.append(t)
                        except (ValueError, AttributeError):
                            pass

            if not active and not recent:
                console.print("[dim]No active or recent agent sessions.[/]")
                return

            table = Table(title="Agent Sessions")
            table.add_column("id", style="bold")
            table.add_column("status")
            table.add_column("kind", style="magenta")
            table.add_column("att", justify="right", style="dim")
            table.add_column("turns", justify="right", style="dim")
            table.add_column("burn", justify="right", style="dim")
            table.add_column("title")
            table.add_column("repo", style="cyan")

            for t in active + recent:
                attempts = await store.list_attempts(t.id)
                att_n = str(len(attempts))
                last_turns = "—"
                last_tokens = "—"
                for a in reversed(attempts):
                    if a.get("turns_used") and last_turns == "—":
                        last_turns = str(a["turns_used"])
                    # BURN, not `tokens_used`. This column carried the same
                    # 5500x under-report as `nh logs` did — and this is the
                    # table an operator watches a runaway on, so it is the
                    # worst place to show the smallest number.
                    if last_tokens == "—":
                        _, _b = _attempt_tokens(a)
                        if _b:
                            last_tokens = f"{_b:,}"
                repo_name = t.repo_path.rstrip("/").rsplit("/", 1)[-1] if t.repo_path else ""
                status_str = t.status.value
                status_colors = {
                    "implementing": "bold green",
                    "planning": "blue",
                    "context": "blue",
                    "reviewing": "yellow",
                    "testing": "yellow",
                    "done": "dim green",
                    "failed": "dim red",
                    "escalated": "dim red",
                    "awaiting_approval": "dim yellow",
                }
                color = status_colors.get(status_str, "dim")
                styled = f"[{color}]{status_str}[/]" if color else status_str
                table.add_row(
                    t.id[:8], styled, t.kind, att_n, last_turns, last_tokens,
                    t.title[:50], repo_name[:20],
                )
            console.print(table)

    asyncio.run(_go())


@cli.command("unblock")
@click.argument("task_id")
@click.option("--fail", is_flag=True, help="Abandon the task (mark failed) instead of resuming.")
def unblock(task_id, fail):
    """Manually clear a block: resume to implementing, or --fail to abandon."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            target = TaskStatus.FAILED if fail else TaskStatus.IMPLEMENTING
            t.wake_check_at = None
            await store.update_task(t)
            await store.set_status(t, target, validate=False)
            console.print(f"[green]{t.id[:8]} -> {target.value}[/]")

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Human-action verbs (PLAN.md Part 6)                                         #
# --------------------------------------------------------------------------- #

@cli.command("approve")
@click.argument("task_id")
def approve(task_id):
    """Record your approval — YOU merge the PR (agent never merges)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            if t.status != TaskStatus.AWAITING_APPROVAL:
                console.print(
                    f"[yellow]task is {t.status.value!r}, not awaiting_approval — cannot approve[/]"
                )
                sys.exit(1)
            t.context = await store.merge_context(
                t.id, {"approved_at": _now_iso()})
            attempts = await store.list_attempts(t.id)
            pr_url = next(
                (a["pr_url"] for a in reversed(attempts) if a.get("pr_url")), None
            )
            # An already-satisfied claim has no PR to merge — approval IS the
            # human confirmation its terminal promised, so it completes here
            # (the agent still never merges anything; there is nothing to).
            # Guarded on pr_url: the report key persists in context, and after
            # a send-back a LATER attempt may ship a real PR — that approval
            # must stay a merge instruction, never a false DONE (PR #101
            # round-2 review).
            if (t.context or {}).get("already_satisfied_report") and not pr_url:
                await store.set_status(t, TaskStatus.DONE, validate=False)
                console.print(
                    f"[bold green]approved[/] {t.id[:8]} — already satisfied "
                    "claim confirmed; no code change was needed. Task done."
                )
                return
            console.print(f"[bold green]approved[/] {t.id[:8]} — merge the PR in your git host.")
            if pr_url:
                console.print(f"  PR: {pr_url}")
            else:
                console.print("  [dim](no PR URL recorded)[/]")

    asyncio.run(_go())


@cli.command("review-comments")
@click.argument("task_id")
@click.option("--post", "post_spec", default=None,
              help="Approve + post drafts: 'all' or 1-based numbers like 1,3,5. "
                   "Omit to just list them (nothing is posted).")
def review_comments(task_id, post_spec):
    """Show a code-review task's DRAFT comments and approve them one-by-one or all.

    A code_review NEVER posts to the PR on its own — it parks the drafted
    comments here. This is the only path that posts them, and only the ones you
    name. Without --post it just lists; the PR is untouched until you approve.
    """
    config, _ = _bootstrap(require_auth=(post_spec is not None))

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            ctx = t.context or {}
            drafts = ctx.get("draft_review_comments") or []
            pr_url = ctx.get("pr_url")
            if not drafts:
                console.print(
                    f"[yellow]{t.id[:8]} has no draft review comments[/] "
                    "(not a finished code_review, or the review found no issues)."
                )
                return
            console.print(f"[bold]{len(drafts)} draft comment(s)[/] for [cyan]{pr_url}[/]\n")
            for i, d in enumerate(drafts, 1):
                mark = "[green]✓ posted[/]" if d.get("posted") else "[dim]draft[/]"
                loc = (f"{d.get('file')}:{d.get('line')}"
                       if d.get("file") and d.get("line") else "(general)")
                sev = f" [{d['severity']}]" if d.get("severity") else ""
                console.print(f"  [bold]{i}.[/] {mark} [cyan]{loc}[/]{sev}")
                console.print(f"     {d.get('comment')}\n")
            if not post_spec:
                console.print(
                    "[dim]Approve + post with:  "
                    f"nh review-comments {t.id[:8]} --post all   (or --post 1,3)[/]"
                )
                return
            if post_spec.strip().lower() == "all":
                which = "all"
            else:
                try:
                    which = [int(x) - 1 for x in post_spec.split(",") if x.strip()]
                except ValueError:
                    console.print("[red]--post must be 'all' or numbers like 1,3,5[/]")
                    sys.exit(1)
            orch = _build_orchestrator(config, store, task=t)
            posted, remaining = await orch.post_draft_comments(t, which)
            console.print(f"[green]posted {posted}[/] comment(s); {remaining} still unposted.")
            if remaining == 0:
                console.print(f"[bold green]{t.id[:8]} done[/] — all approved comments posted.")

    asyncio.run(_go())


@cli.command("reject")
@click.argument("task_id")
@click.option("--reason", required=True, help="Feedback for the agent on the next attempt.")
def reject(task_id, reason):
    """Send a task back with feedback; agent retries on next run."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            await store.append_context_list(t.id, "send_back_feedback",
                                            {"at": _now_iso(), "message": reason})
            await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
            console.print(
                f"[yellow]sent back[/] {t.id[:8]} — run [bold]nh watch {t.id[:8]}[/] to retry."
            )

    asyncio.run(_go())


@cli.command("diff")
@click.argument("task_id")
def diff(task_id):
    """Show the git diff for the latest attempt's commit."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            if not t.repo_path:
                console.print("[yellow]no repo_path recorded for this task[/]")
                return
            attempts = await store.list_attempts(t.id)
            sha = next(
                (a["commit_sha"] for a in reversed(attempts) if a.get("commit_sha")),
                None,
            )
            if not sha:
                console.print("[dim]no commit recorded yet[/]")
                return
            import subprocess
            try:
                result = subprocess.run(
                    ["git", "diff", f"{sha}~1..{sha}", "--no-color"],
                    cwd=t.repo_path,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode == 0:
                    console.print(result.stdout or "[dim](empty diff)[/]")
                else:
                    console.print(
                        f"[yellow]git diff failed:[/] {result.stderr.strip()}\n"
                        f"[dim]commit: {sha}  branch: "
                        f"{next((a['branch_name'] for a in reversed(attempts) if a.get('branch_name')), '?')}[/]"
                    )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                console.print(f"[red]git error:[/] {exc}")

    asyncio.run(_go())


@cli.command("review")
@click.argument("target")
@click.option("--repo", default=".",
              help="Local clone to fetch the PR diff from (for a PR/MR URL).")
def review(target, repo):
    """Review a PR, or show a task's review checklist.

    TARGET is a PR/MR URL — queues a standalone code_review task that fetches the
    diff, runs the fresh-context adversarial reviewer, and posts cited findings —
    OR a task id, which shows that task's latest review checklist.
    """
    import re as _re
    is_pr_url = bool(_re.match(r"https?://\S+/(?:pull|merge_requests)/\d+", target))
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            if is_pr_url:
                from ..core.task import Task
                t = Task.new(f"Review {target}",
                             repo_path=str(Path(repo).resolve()),
                             description=target, kind="code_review")
                await store.create_task(t)
                console.print(f"[green]queued code review[/] [bold]{t.id[:8]}[/] "
                              f"— {target}")
                console.print("  the worker fetches the diff, runs the adversarial "
                              "reviewer, and posts cited findings on the PR.")
                console.print(f"  see the result: [bold]nh review {t.id[:8]}[/]")
                return
            t = await store.find_task(target)
            if not t:
                console.print(f"[red]no task matching[/] {target}")
                sys.exit(1)
            attempts = await store.list_attempts(t.id)
            attempt = next(
                (a for a in reversed(attempts) if a.get("review_checklist")), None
            )
            if not attempt:
                console.print("[dim]no review checklist yet[/]")
                return
            import json as _json
            raw = attempt["review_checklist"]
            checklist = _json.loads(raw) if isinstance(raw, str) else raw
            passed_overall = checklist.get("passed", False)
            verdict = "[green]PASSED[/]" if passed_overall else "[red]FAILED[/]"
            console.rule(f"[bold]review — attempt #{attempt['attempt_number']} — {verdict}")
            for item in checklist.get("items") or []:
                icon = "[green]✓[/]" if item.get("passed") else "[red]✗[/]"
                console.print(f"  {icon} {item.get('label', '')}")
                if item.get("evidence"):
                    console.print(f"    [dim]{item['evidence']}[/]")

    asyncio.run(_go())


@cli.command("investigate")
@click.argument("question", required=False)
@click.option("--repo", default=".", help="Repo to investigate.")
@click.option("--show", "show_id", default=None,
              help="Print the report of a completed investigation instead.")
def investigate(question, repo, show_id):
    """Start a read-only investigation (root-cause / analysis) that produces a
    cited report — no PR, no test gate — for the questions the implement→PR loop
    can't converge on. Or --show a completed one's report.
    """
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            if show_id:
                t = await store.find_task(show_id)
                if not t:
                    console.print(f"[red]no task matching[/] {show_id}")
                    sys.exit(1)
                findings = (t.context or {}).get("findings")
                if not findings:
                    console.print("[dim]no report yet — investigation not complete[/]")
                    return
                console.rule(f"[bold]investigation report — {t.title[:60]}")
                console.print(findings)
                return
            if not question:
                console.print("[red]provide a question to investigate, or "
                              "--show <task_id>[/]")
                sys.exit(1)
            from ..core.task import Task
            t = Task.new(question, repo_path=str(Path(repo).resolve()),
                         kind="investigation")
            await store.create_task(t)
            console.print(f"[green]investigating[/] [bold]{t.id[:8]}[/] — {question}")
            console.print("  read-only; produces a cited report (no PR, no test gate).")
            console.print(f"  read it when done: [bold]nh investigate --show {t.id[:8]}[/]")

    asyncio.run(_go())


@cli.command("logs")
@click.argument("task_id")
def logs(task_id):
    """Show the attempt log for a task (turns, tokens, result, failure reason)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                console.print(f"[red]no task matching[/] {task_id}")
                sys.exit(1)
            console.print(
                f"[bold]{t.id[:8]}[/] [blue]{t.status.value}[/] — {t.title}"
            )
            attempts = await store.list_attempts(t.id)
            if not attempts:
                console.print("[dim]no attempts yet[/]")
                return
            for a in attempts:
                import json as _json
                tr_raw = a.get("test_results")
                tr = (_json.loads(tr_raw) if isinstance(tr_raw, str) else tr_raw) or {}
                status_color = "green" if a.get("status") == "succeeded" else "red"
                # SPEND, not just non-cache tokens. `tokens_used` holds
                # non-cache only, while the budget guard enforces
                # `tokens_used + cache_read_tokens` — and cache reads are the
                # overwhelming majority of real burn. Printing the former
                # alone under-reported a live runaway by ~5500x: an attempt
                # that was aborted for spending 4,054,229 tokens displayed as
                # `tokens=731`, so the one attempt that needed attention
                # looked like the cheapest thing that ever ran.
                # TWO numbers, because they answer different questions and
                # collapsing them lies about one of them. See _attempt_tokens.
                _spend, _burn = _attempt_tokens(a)
                _fmt = lambda v: f"{v:,}" if v is not None else "?"
                _plain = a.get("tokens_used")
                console.print(
                    f"\n  [bold]attempt #{a['attempt_number']}[/] "
                    f"[{status_color}]{a.get('status', '?')}[/]  "
                    # `.get(k, '?')` defaults only on a MISSING key, never on a
                    # NULL value — and an aborted attempt records turns as
                    # NULL, so this printed the literal "turns=None".
                    f"turns={a.get('turns_used') if a.get('turns_used') is not None else '?'}  "
                    # "tok", because every other cost surface here prints
                    # dollars and a bare number reads as money.
                    f"spend={_fmt(_spend)} tok  burn={_fmt(_burn)} tok\n"
                    f"    [dim]coder: non-cache {_fmt(_plain)} · cache-read "
                    f"{a.get('cache_read_tokens') or 0:,} · cache-creation "
                    f"{a.get('cache_creation_tokens') or 0:,} "
                    f"(not counted by the cap)[/]"
                )
                if a.get("branch_name"):
                    console.print(f"    branch: {a['branch_name']}")
                if a.get("pr_url"):
                    console.print(f"    PR:     {a['pr_url']}")
                if a.get("review_passed") is not None:
                    rv = "[green]pass[/]" if a["review_passed"] else "[red]fail[/]"
                    console.print(f"    review: {rv}")
                if tr:
                    passed = tr.get("passed", 0)
                    failed = tr.get("failed", 0)
                    console.print(f"    tests:  {passed} passed / {failed} failed")
                if a.get("failure_reason"):
                    console.print(f"    [red]reason: {a['failure_reason']}[/]")

    asyncio.run(_go())


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Learning queue (PLAN.md 4.5)                                                #
# --------------------------------------------------------------------------- #

@cli.command("learnings-curate")
@click.option("--apply", "apply_llm", is_flag=True,
              help="Also apply the LLM-proposed archives/consolidations "
                   "(deterministic dedupe always applies).")
def learnings_curate(apply_llm):
    """Tidy the pending learning queue (D2 #3): archive duplicates, propose
    consolidations. Never deletes; confirmed memories are never touched; the
    human confirm gate stands."""
    config, _ = _bootstrap()
    from ..learning.curator import curate

    async def _llm(prompt: str) -> str:
        from ..agent.claude_backend import ClaudeBackend
        backend = ClaudeBackend(model=config.utility_model, readonly=True)
        result = await backend.run(prompt, cwd=Path("."), max_turns=1,
                                   effort="low")
        return result.final_text or ""

    async def _go():
        async with Store(config.db_path) as store:
            report = await curate(store, llm_call=_llm, apply=apply_llm)
            console.print(
                f"[green]dedupe:[/] {report.duplicates_archived} archived · "
                f"[cyan]llm proposals:[/] {len(report.llm_archive_proposed)} "
                f"archive, {len(report.llm_consolidate_proposed)} consolidate"
                f"{' — APPLIED' if report.llm_applied else ' (dry: rerun with --apply)'}")
            for a in report.llm_archive_proposed[:15]:
                console.print(f"  [dim]archive {a.get('id')}: {a.get('reason','')[:80]}[/]")
            for c in report.llm_consolidate_proposed[:10]:
                console.print(f"  [dim]merge {c.get('ids')}: {c.get('title','')[:70]}[/]")

    asyncio.run(_go())


@cli.command("learnings")
@click.option("--confirm", "confirm_id", default=None, help="Confirm a proposal by id.")
@click.option("--reject", "reject_id", default=None, help="Reject/delete a proposal by id.")
@click.option("--active", is_flag=True, help="Show the confirmed active rule set instead.")
def learnings(confirm_id, reject_id, active):
    """Review the human-confirmed learning queue; confirm or reject proposals.

    Nothing enters the active rule set without your one-click confirm.
    """
    config, _ = _bootstrap(require_auth=False)
    from ..learning import LearningQueue

    async def _go():
        async with Store(config.db_path) as store:
            q = LearningQueue(store)
            if confirm_id:
                mem = await store.find_memory(confirm_id)
                if not mem:
                    console.print(f"[red]no proposal matching[/] {confirm_id}")
                    return
                await q.confirm(mem["id"])
                console.print(f"[green]confirmed[/] {mem['id'][:8]} — now active")
                return
            if reject_id:
                mem = await store.find_memory(reject_id)
                if not mem:
                    console.print(f"[red]no proposal matching[/] {reject_id}")
                    return
                await q.reject(mem["id"])
                console.print(f"[yellow]rejected[/] {mem['id'][:8]}")
                return
            rows = await (q.active() if active else q.pending())
            if not rows:
                console.print("[green]no active rules[/]" if active
                              else "[green]no pending proposals[/]")
                return
            label = "active rule set" if active else "pending proposals (one-click confirm)"
            console.rule(f"[bold]{label}")
            for m in rows:
                console.print(
                    f"[bold]{m['id'][:8]}[/] [magenta]{m['type']}[/] {m['title']}"
                )
                for line in (m["content"] or "").splitlines():
                    if line.strip():
                        console.print(f"  [dim]{line}[/]")
                if not active:
                    console.print(
                        f"  confirm: nh learnings --confirm {m['id'][:8]}   "
                        f"reject: nh learnings --reject {m['id'][:8]}"
                    )

    asyncio.run(_go())


@cli.command("history")
@click.option("--days", default=30, help="How many days back to extract.")
@click.option("--output", "-o", default=None,
              help="Directory to write markdown transcripts to.")
@click.option("--analyze", is_flag=True,
              help="Analyze transcripts for user corrections and propose learnings.")
@click.option("--json-out", is_flag=True,
              help="Print transcripts as JSON to stdout (for piping).")
@click.option("--roots", multiple=True, type=click.Path(path_type=Path),
              help="Claude Code projects roots to read (repeatable). "
                   "Default: ~/.claude/projects AND ~/.claude-personal/projects.")
def history(days, output, analyze, json_out, roots):
    """Extract conversation history from EVERY source.

    Combines Claude Code sessions read from disk (both the enterprise and
    personal config dirs — always available) with Windsurf transcripts  # term-ok: real IDE names
    from a running IDE (best-effort; skipped with a note when no IDE runs).

    \b
    Examples:
      nh history                        # list conversations, all sources
      nh history -o ./transcripts       # write markdown files
      nh history --analyze              # propose learnings from corrections
      nh history --days 7 --json-out    # JSON to stdout
      nh history --roots ~/.claude-personal/projects   # one root only
    """
    from ..history.claude_code import extract_claude_code_transcripts
    from ..history.extractor import (
        IDENotRunningError,
        extract_transcripts,
        write_transcripts,
    )

    # In --json-out mode stdout must be pure JSON (pipeable to jq); status
    # lines go to stderr instead.
    status_console = Console(stderr=True) if json_out else console

    transcripts = []
    try:
        transcripts += extract_transcripts(days=days)
    except (IDENotRunningError, ImportError) as exc:
        status_console.print(f"[dim]windsurf: skipped ({exc})[/]")  # term-ok: real IDE names

    try:
        transcripts += extract_claude_code_transcripts(
            days=days, roots=list(roots) or None)
    except Exception as exc:  # noqa: BLE001 — one bad root must not kill the run
        status_console.print(f"[red]claude code extract failed: {exc}[/]")

    if not transcripts:
        status_console.print(
            f"[yellow]no conversations found in the last {days} days[/]")
        if json_out:
            print("[]")
        return

    status_console.print(
        f"[green]extracted {len(transcripts)} conversations[/] "
        f"({sum(len(t.messages) for t in transcripts)} total messages)")

    if json_out:
        import json as _json
        from dataclasses import asdict
        print(_json.dumps([asdict(t) for t in transcripts], indent=2,
                          ensure_ascii=False))
        return

    if output:
        index_path = write_transcripts(transcripts, output)
        console.print(f"[bold]transcripts written to:[/] {output}")
        console.print(f"[bold]index:[/] {index_path}")
    else:
        table = Table(title=f"Conversations (last {days} days)")
        table.add_column("#", style="dim", width=4)
        table.add_column("Date", width=12)
        table.add_column("Source", width=14)
        table.add_column("Title")
        table.add_column("Msgs", justify="right", width=6)
        for i, t in enumerate(transcripts, 1):
            table.add_row(str(i), t.created[:10],
                          getattr(t, "source", "") or "windsurf", t.title,  # term-ok: internal source tag names the real IDE
                          str(len(t.messages)))
        console.print(table)

    if analyze:
        from ..history.analyzer import analyze_all
        findings = analyze_all(transcripts)
        if not findings:
            console.print("[green]no correction patterns found[/]")
            return

        console.print(f"\n[bold]{len(findings)} user corrections detected[/]")

        config, _ = _bootstrap(require_auth=False)
        from ..learning import LearningQueue

        async def _propose():
            async with Store(config.db_path) as store:
                q = LearningQueue(store)
                proposed = 0
                for f in findings:
                    mid = await store.add_memory(
                        mem_type=f.category,
                        title=f.title,
                        content=f.content,
                        tags=f.tags,
                        project=f.source_transcript,
                        source="history",
                        confirmed=False,
                        dedupe_key=f"history:{f.category}:{f.title}",
                    )
                    if mid:
                        proposed += 1
                        console.print(
                            f"  [magenta]{f.category}[/] {f.title[:60]}"
                        )
                console.print(
                    f"\n[green]{proposed} proposals queued[/] — "
                    "review with: nh learnings"
                )

        asyncio.run(_propose())


def _acquire_pid_lock() -> bool:
    """Write a PID lock file. Returns True if we got the lock, False if another
    instance is already running."""
    from ..config import NO_HUMAN_HOME, ensure_private_dir
    lock_path = NO_HUMAN_HOME / "nh.pid"
    ensure_private_dir(lock_path.parent)

    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text().strip())
            # Check if the old process is still alive.
            os.kill(old_pid, 0)
            return False  # process alive → another instance running
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass  # stale lock — old process is dead

    lock_path.write_text(str(os.getpid()))
    return True


def _release_pid_lock() -> None:
    from ..config import NO_HUMAN_HOME
    lock_path = NO_HUMAN_HOME / "nh.pid"
    try:
        if lock_path.exists():
            pid = int(lock_path.read_text().strip())
            if pid == os.getpid():
                lock_path.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


@cli.command("doctor")
def doctor():
    """Liveness check: which guarded mechanisms have actually ever fired.

    The system's worst bugs were silences, not crashes — TESTING dead for its
    entire life, a watcher that persisted nothing. This enumerates every
    mechanism's lifetime firings and flags the known silent-death patterns.
    """
    from datetime import datetime

    from ..doctor import diagnose

    config, _ = _bootstrap(require_auth=False)

    from ..agent.backend_check import check_backend

    async def _go():
        async with Store(config.db_path) as store:
            d = await diagnose(store)

        # Live readiness (not history): can the coding backend actually run a
        # task right now? A missing `claude` CLI makes the board load green
        # while every task fails at launch — a contradiction, so it flips
        # `healthy` and the exit code below.
        backend = check_backend(
            profile=(config.get("llm") or {}).get("auth_profile"),
            auth_mode=(config.get("llm") or {}).get("auth_mode", "subscription"))
        cli = backend.cli_path or "not found"
        colour = "green" if backend.ready else "red"
        console.print(f"[bold]coding backend[/] — claude CLI: "
                      f"[{colour}]{cli}[/]")
        for reason in backend.reasons:
            d.contradictions.append(f"CODING BACKEND UNUSABLE: {reason}")

        console.print("[bold]mechanism liveness[/] (lifetime firings)")
        for m in d.mechanisms:
            when = (datetime.fromtimestamp(m["last_ts"]).strftime("%Y-%m-%d %H:%M")
                    if m["last_ts"] else "never")
            colour = "green" if m["count"] else "yellow"
            line = f"  [{colour}]{m['name']:<18}[/] {m['count']:>6}  last: {when}"
            if m["hint"]:
                line += f"  [dim]{m['hint']}[/]"
            console.print(line)

        if d.contradictions:
            console.print("\n[bold red]contradictions[/] — evidence of activity "
                          "without evidence of the mechanism:")
            for c in d.contradictions:
                console.print(f"  [red]✗[/] {c}")
        if d.evidence_gaps:
            console.print("\n[bold yellow]evidence gaps[/] — statuses not backed "
                          "by events:")
            for g in d.evidence_gaps:
                console.print(f"  [yellow]![/] {g}")
        if d.advisories:
            console.print("\n[bold cyan]advisories[/] — prunable leftovers "
                          "(do not affect health):")
            for a in d.advisories:
                console.print(f"  [cyan]•[/] {a}")
        if d.healthy:
            console.print("\n[green]no contradictions, no evidence gaps[/]")

    asyncio.run(_go())


@cli.command("start")
@click.option("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
@click.option("--port", default=None, type=int, help="Bind port (default from config).")
@click.option("--workers", default=None, type=int,
              help="Max concurrent tasks (default 1 = serial mode).")
@click.option("--no-open", is_flag=True, help="Don't open browser.")
def start(host, port, workers, no_open):
    """Start no_human: web board + task worker. The only command you need.

    \b
    This single command starts:
      • The web board (FastAPI + React UI)
      • A task worker that picks up and runs new tasks
      • The wake watcher (auto-resumes blocked tasks)

    \b
    No configuration needed beyond `nh init`. Tasks created from the
    board are automatically picked up and run by the embedded worker.

    \b
    Examples:
      nh start                     # board + 1 serial worker
      nh start --workers 3         # board + 3 concurrent workers
      nh start --no-open           # don't open browser
    """
    config, _ = _bootstrap()
    _assert_backend_usable()

    if not _acquire_pid_lock():
        console.print(
            "[red]another no_human instance is already running[/]\n"
            "Kill it first, or remove the stale lock:\n"
            "  [bold]rm ~/.no_human/nh.pid[/]"
        )
        sys.exit(1)

    port = port or config.data.get("server", {}).get("port", 8420)

    # Determine worker concurrency: explicit flag > config > 1 (serial), then
    # clamped to 1 unless concurrency.enabled — without it there is no worktree
    # isolation and parallel tasks would share one checkout.
    from ..core.scheduler import resolve_max_workers
    conc = config.data.get("concurrency", {})
    max_workers, worker_warning = resolve_max_workers(config.data, override=workers)
    if worker_warning:
        console.print(f"[yellow]⚠ {worker_warning}[/]")
    poll_interval = 10
    try:
        from ..blockers import parse_duration
        raw = str(conc.get("poll_interval", "10s"))
        interval = parse_duration(raw)
        if interval:
            poll_interval = int(interval.total_seconds())
    except Exception:  # noqa: BLE001
        pass

    url = f"http://{host}:{port}"
    mode = f"{max_workers} worker(s)" + (" · serial" if max_workers == 1 else " · concurrent")
    console.print(f"[bold green]no_human[/]  {url}  ·  {mode}")
    console.print("[dim]ctrl-c to stop[/]")

    if not no_open:
        import webbrowser
        webbrowser.open(url)

    import uvicorn
    from ..api.app import app as _app

    # CLI overrides for worker concurrency (lifespan reads these). Always set,
    # and always the *resolved* value, so the pool the server builds is exactly
    # the one we just announced — the two used to be computed independently.
    _app.state._worker_opts = {
        "max_workers": max_workers,
        "poll_interval": poll_interval,
    }

    try:
        uvicorn.run(_app, host=host, port=port, log_level="warning")
    finally:
        _release_pid_lock()


@cli.command("dashboard")
@click.option("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
@click.option("--port", default=None, type=int, help="Bind port (default from config).")
@click.option("--no-open", is_flag=True, help="Don't open browser.")
def dashboard(host, port, no_open):
    """Alias for `nh start`. Starts board + worker."""
    # Forward to start() so there's only one code path.
    ctx = click.get_current_context()
    ctx.invoke(start, host=host, port=port, workers=None, no_open=no_open)


# --------------------------------------------------------------------------- #
# Evaluation harness (PLAN.md Part 21)                                        #
# --------------------------------------------------------------------------- #

@cli.command("eval")
@click.option("--prev", "prev_path", default=None, type=click.Path(),
              help="Previous scorecard JSON to diff against (CI gate).")
@click.option("--out", "out_path", default=None, type=click.Path(),
              help="Write the scorecard JSON here.")
@click.option("--gate", is_flag=True, help="Exit non-zero if the CI gate fails.")
def eval_cmd(prev_path, out_path, gate):
    """Replay the golden task set and emit a scorecard (Part 21).

    Runs on subscription auth via the real backend; a deliberately-impossible
    golden task must be escalated, never faked.
    """
    config, _ = _bootstrap()
    from ..agent.claude_backend import ClaudeBackend
    from ..eval import Scorecard, render_scorecard, run_eval
    from ..eval.judge import IntentJudge
    from ..review.reviewer import AdversarialReviewer

    def backend_factory(_golden):
        return ClaudeBackend(
            model=config.primary_model,
            forbidden_paths=config["safety"]["forbidden_paths"],
            never_push_to=config["git"]["never_push_to"],
        )

    async def _go():
        previous = Scorecard.load(Path(prev_path)) if prev_path else None
        run = await run_eval(
            config.data,
            backend_factory=backend_factory,
            reviewer=AdversarialReviewer(model=config.review_model),
            judge=IntentJudge(model=config.review_model),
            previous=previous,
            now=_now_iso(),
            on_event=lambda e: console.print(
                f"[dim]· {e.get('kind')}: {e.get('task', '')}"
                f"{' ✓' if e.get('correct') else ''}[/]"),
        )
        console.print(render_scorecard(run.scorecard, previous))
        if out_path:
            run.scorecard.save(Path(out_path))
            console.print(f"[dim]scorecard → {out_path}[/]")
        if not run.gate.passed:
            console.print("[bold red]CI gate FAILED:[/]")
            for r in run.gate.reasons:
                console.print(f"  ⛔ {escape(str(r))}")
            if gate:
                sys.exit(1)
        else:
            console.print("[bold green]CI gate passed[/]")

    asyncio.run(_go())


@cli.group("bench")
def bench():
    """North-star benchmark: replay the operator's REAL historical tasks.

    build  → specs from conversation history (no-cheat: initial request only);
    run    → replay through the real pipeline in push-proof sandboxes,
             recording <label>-<stamp>.json — publishing nothing;
    publish→ promote one results file to the baseline + the committed report;
    report → re-render docs/NORTH_STAR_BENCH.md from the published baseline.
    """


def _slug(label: str) -> str:
    """Filename-safe form of a run label. A label reaches this from the command
    line and becomes a path, so anything that could traverse or collide is
    flattened rather than trusted."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (label or "run").strip()).strip("-.")
    return slug or "run"


def _spec_set_key(specs) -> str:
    """Short stable digest of which specs a run covers.

    Two runs with different spec sets must never share a checkpoint, whatever
    they are labelled: an unlabelled `--limit 1` probe and an unlabelled
    `--full` run both slug to "run", and the probe's clean completion deleted
    the full run's checkpoint. Keying on the spec set is what separates them.
    """
    import hashlib
    ids = ",".join(sorted(s.id for s in specs))
    return hashlib.sha256(ids.encode()).hexdigest()[:8]


@bench.command("build")
@click.option("--days", default=400, help="History horizon.")
@click.option("--roots", multiple=True, type=click.Path(path_type=Path),
              help="Claude Code projects roots (default: both config dirs).")
@click.option("--out", "out_dir", default=None, type=click.Path(path_type=Path),
              help="Spec output dir (default: eval/northstar_tasks/generated — "
                   "GITIGNORED; raw specs hold verbatim conversation content).")
def bench_build(days, roots, out_dir):
    """Build benchmark specs from ALL conversation sources."""
    from ..eval.bench_task import GENERATED_DIR, build_bench_tasks
    from ..history.claude_code import extract_claude_code_transcripts
    from ..history.extractor import IDENotRunningError, extract_transcripts

    transcripts = []
    try:
        transcripts += extract_transcripts(days=days)
    except (IDENotRunningError, ImportError) as exc:
        console.print(f"[dim]windsurf: skipped ({exc})[/]")  # term-ok: real IDE names
    # min_user_msgs=1: a one-shot request is a real task for the bench corpus.
    transcripts += extract_claude_code_transcripts(
        days=days, limit=10_000, roots=list(roots) or None, min_user_msgs=1)

    target = Path(out_dir) if out_dir else GENERATED_DIR
    written = build_bench_tasks(transcripts, out_dir=target)
    runnable = sum(1 for _ in written)  # count; runnable split shown by report
    console.print(f"[green]{runnable} specs written[/] → {target}")
    console.print("[dim]curate the core subset by copying reviewed specs up "
                  "into eval/northstar_tasks/ and setting subset: core[/]")


@bench.command("run")
@click.option("--full", is_flag=True,
              help="Run the FULL corpus (default: subset core only).")
@click.option("--limit", default=0, help="Cap the number of tasks (0 = no cap).")
@click.option("--gate", is_flag=True,
              help="Exit non-zero on regression, OR when the run did not measure "
                   "enough of the corpus (too much skipped/dead, a filtered or "
                   "capped slice, or narrower than the baseline). NOTE: the "
                   "available-spec count is read from the canonical corpus "
                   "regardless of --specs-dir, so a deliberately small scratch "
                   "corpus cannot pass --gate. That is intended: a filtered "
                   "slice must not be able to stand for the corpus.")
@click.option("--prev", "prev_path", default=None, type=click.Path(),
              help="Previous latest.json (default: eval/results/northstar/latest.json).")
@click.option("--label", default="", help="Label for this run (e.g. the change).")
@click.option("--resume", is_flag=True,
              help="Skip specs already scored in the checkpoint (progress.json) "
                   "— continue a run that died on quota saturation.")
@click.option("--specs-dir", default=None, type=click.Path(path_type=Path),
              help="Read specs from here too (default: eval/northstar_tasks + generated/ when --full).")
@click.option("--parallel", default=1, type=click.IntRange(1, 16),
              help="Run up to N specs concurrently (each spec is already "
                   "sandbox-isolated, so this is a pure wall-clock win). "
                   "Values above ~4 risk saturating the shared subscription "
                   "quota mid-run; default 1 = today's serial behavior.")
@click.option("--quick", is_flag=True,
              help="Stratified iteration tier: ONE representative per coverage "
                   "cell (project × runnable × expect-escalation × size), "
                   "picked deterministically — the corpus's whole variety at a "
                   "fraction of the wall clock. Iteration signal only; a quick "
                   "card cannot publish as the baseline.")
def bench_run(full, limit, gate, prev_path, label, specs_dir, resume, parallel,
              quick):
    """Replay bench specs through the REAL pipeline; score vs the originals."""
    import tempfile

    if quick and full:
        raise click.UsageError(
            "--quick and --full are mutually exclusive: --quick is a "
            "stratified slice of the core subset; --full is the whole corpus.")

    from ..agent.claude_backend import ClaudeBackend
    from ..eval.bench_task import GENERATED_DIR, NORTHSTAR_DIR, load_bench_tasks
    from ..eval.judge import GoalJudge
    from ..eval.northstar import NorthStarRunner
    from ..eval.northstar_card import (
        RESULTS_DIR,
        NorthStarCard,
        northstar_gate,
        publish_refusals,
    )
    from ..review.reviewer import AdversarialReviewer

    try:
        if specs_dir:
            specs = load_bench_tasks(Path(specs_dir))
        else:
            specs = load_bench_tasks(NORTHSTAR_DIR, subset=None if full else "core")
            if full:
                specs += load_bench_tasks(GENERATED_DIR)
    except ValueError as exc:
        # A malformed repo map raises with a precise, human-readable message.
        # Unhandled it exited 1 with a traceback and NO console output, so the
        # one thing that would tell the operator what to fix never rendered.
        console.print(f"[red]{escape(str(exc))}[/]")
        sys.exit(1)
    seen: set[str] = set()
    specs = [s for s in specs if not (s.id in seen or seen.add(s.id))]
    # How much corpus EXISTS, measured before --limit and independently of
    # --specs-dir. Coverage is a ratio over what a run LOADED, so filtering to
    # the specs that still resolve reads as perfect coverage; comparing loaded
    # against available is the only check that survives that.
    corpus_available = len(load_bench_tasks(NORTHSTAR_DIR, subset="core"))
    if quick:
        from ..eval.bench_task import select_quick_subset
        loaded = len(specs)
        specs = select_quick_subset(specs)
        console.print(
            f"[yellow]quick tier[/]: {len(specs)}/{loaded} specs — one per "
            "coverage cell; iteration signal only, not publishable as the "
            "baseline")
    if limit:
        specs = specs[:limit]
    # Pre-flight the corpus BEFORE spending a night on it. The repo map is
    # gitignored, so it is absent in every git worktree — without this, a run
    # launched from one silently fails to resolve most specs and reads as a
    # model regression rather than an operational accident.
    from ..eval.bench_task import (
        REPO_MAP_PATH,
        check_repo_map,
        redact_local_path,
        spec_project_name,
    )
    unresolved = check_repo_map(specs)
    if unresolved:
        console.print(
            f"[yellow]{len(unresolved)}/{len(specs)} spec repo(s) will not "
            f"resolve on this machine[/]")
        for line in unresolved[:10]:
            console.print(f"  ⚠ {escape(line)}")
        if len(unresolved) > 10:
            console.print(f"  [dim]… and {len(unresolved) - 10} more[/]")
        if not REPO_MAP_PATH.exists():
            console.print(
                f"[dim]no repo map at {REPO_MAP_PATH} — see "
                f"eval/repo_map.example.yaml[/]")
    if not specs:
        console.print("[yellow]no specs found — run `nh bench build` and curate "
                      "a core subset first[/]")
        sys.exit(1)

    config, _ = _bootstrap()

    def backend_factory(_spec):
        return ClaudeBackend(
            model=config.primary_model,
            forbidden_paths=config["safety"]["forbidden_paths"],
            never_push_to=config["git"]["never_push_to"],
        )

    async def _go():
        def make_runner() -> NorthStarRunner:
            # A fresh runner (with its own reviewer/judge) per spec, mirroring
            # the serve pool's fresh-orchestrator-per-task pattern, so
            # concurrent specs never share mutable review state.
            return NorthStarRunner(
                config.data,
                backend_factory=backend_factory,
                reviewer=AdversarialReviewer(model=config.review_model),
                goal_judge=GoalJudge(model=config.review_model),
                event_sink=lambda e: None,
            )
        import json as _json
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        # Checkpoint keyed by label AND spec set. A shared progress.json meant a
        # one-spec probe and a 141-spec run collided: the probe "completed
        # cleanly" and its unlink() deleted the long run's only resumable state.
        # The label alone does not fix that — both default to "" — so the spec
        # set is what actually separates a probe from the corpus.
        ckpt = RESULTS_DIR / f"progress-{_slug(label)}-{_spec_set_key(specs)}.json"
        legacy_ckpt = RESULTS_DIR / "progress.json"
        if resume and not ckpt.exists() and legacy_ckpt.exists():
            # A run started before per-label checkpoints must stay resumable —
            # but ONLY if that checkpoint is this run's. Adopting it whenever it
            # existed re-created the original incident on the resume path: a
            # one-spec probe adopted a 56-spec checkpoint, filtered every spec
            # out as foreign, and then unlinked it on "clean completion". The
            # foreign-spec filter below proves the code can already tell; it
            # must refuse the file rather than inherit and delete it.
            legacy = NorthStarCard.load(legacy_ckpt)
            legacy_ids = {s.task_id for s in legacy.scores} if legacy else set()
            # Ownership, by the identity the checkpoint actually records. A
            # subset test is not ownership: a run whose spec set CONTAINS the
            # legacy specs passes it, which let `--full --resume` swallow the
            # 10 specs (8 of them dead) of an unrelated core run. An unlabelled
            # checkpoint is not identifiable at all, so it is declined rather
            # than guessed at — losing a one-time migration beats adopting
            # someone else's scores.
            owned = bool(legacy_ids) and bool(legacy.label) \
                and legacy.label == label and legacy_ids <= {s.id for s in specs}
            if owned:
                # COPY, never consume. The keyed file is this run's to rewrite
                # and unlink; `progress.json` is left in place forever, so no
                # code path can delete a checkpoint it did not create — which
                # is the whole incident, twice over.
                import shutil
                shutil.copy2(legacy_ckpt, ckpt)
            elif legacy_ids:
                console.print(
                    f"[yellow]not resuming from {legacy_ckpt.name}: it holds "
                    f"{len(legacy_ids)} spec(s) from run "
                    f"'{legacy.label or 'unlabelled'}' — left untouched[/]")
        scores = []
        done_ids: set = set()
        if resume and ckpt.exists():
            # Reload the partial run so a quota death doesn't waste completed
            # tasks (the expanded run died at 3/14 on "Stream closed"). Only
            # carry forward specs that belong to THIS run's set — a checkpoint
            # from a run with different --full/--limit/--specs-dir must not
            # bleed foreign specs into latest.json (the gate baseline).
            prev_card = NorthStarCard.load(ckpt)
            if prev_card is not None:
                spec_ids = {s.id for s in specs}
                scores = [sc for sc in prev_card.scores
                          if sc.task_id in spec_ids]
                done_ids = {sc.task_id for sc in scores}
                foreign = [sc for sc in prev_card.scores
                           if sc.task_id not in spec_ids]
                if foreign:
                    console.print(
                        f"[yellow]resume: ignoring {len(foreign)} checkpointed "
                        "spec(s) not in this run — the spec set changed; check "
                        "your --full/--limit/--specs-dir flags[/]")
                console.print(f"[green]resuming — {len(done_ids)} spec(s) "
                              "already scored[/]")
        base_tmp = Path(tempfile.mkdtemp(prefix="nh-bench-"))
        # Bounded pool. Each spec already runs in its own sandbox clone +
        # workdir, so --parallel is a pure wall-clock lever; at the default of
        # 1 the semaphore serializes in submission order — exactly the old
        # serial loop. The checkpoint lock keeps per-completion saves atomic.
        pool = asyncio.Semaphore(parallel)
        ckpt_lock = asyncio.Lock()

        async def _run_spec(spec):
            async with pool:
                console.print(
                    f"[dim]· {escape(spec.id)} {escape(spec.title[:60])}[/]")
                wd = base_tmp / spec.id
                wd.mkdir(parents=True, exist_ok=True)
                try:
                    score = await make_runner().run_one(spec, workdir=wd)
                except Exception as exc:  # noqa: BLE001 — one task's hard crash
                    # (e.g. the SDK CLI dying on quota saturation: "Stream
                    # closed") must not lose the run's partial results. Recorded
                    # honestly as crashed, never as satisfied.
                    from ..eval.northstar import BenchScore
                    # Redact BEFORE truncating. A CalledProcessError stringifies
                    # its whole argv, so an unresolvable repo puts the real
                    # local path into a note that is rendered into the TRACKED
                    # report — and whether an org name survives the 80-char cell
                    # depends only on how long the operator's home directory is.
                    # Computed once and used for both the record and the
                    # console line.
                    crash_note = redact_local_path(str(exc), spec)
                    orig = spec.original or {}
                    toks = orig.get("tokens", {}) or {}
                    score = BenchScore(
                        task_id=spec.id, title=spec.title,
                        outcome_status="crashed", goal_satisfied=False,
                        escalated_honestly=False, mergeable=None,
                        nh_tokens=0, nh_cache_tokens=0,
                        nh_cache_creation_tokens=0, nh_turns=0,
                        nh_wall_clock_s=0.0,
                        orig_tokens=int(toks.get("input_tokens", 0))
                        + int(toks.get("output_tokens", 0)),
                        orig_cache_tokens=int(toks.get("cache_read_input_tokens", 0)),
                        orig_cache_creation_tokens=int(
                            toks.get("cache_creation_input_tokens", 0)),
                        orig_wall_clock_s=float(orig.get("wall_clock_s", 0.0)),
                        orig_corrections=int(orig.get("corrections", 0)),
                        subset=spec.subset,
                        project=spec_project_name(spec),
                        notes=f"runner crashed: {crash_note[:300]}",
                    )
                    console.print(f"  [red]💥 {escape(spec.id)} crashed[/] "
                                  f"({escape(crash_note[:80])})")
                else:
                    mark = {True: "✅", False: "❌", None: "⏭"}[score.goal_satisfied]
                    ratio = (f"cost×{score.cost_ratio:.2f}"
                             if score.cost_ratio is not None else "cost n/a")
                    console.print(
                        f"  {mark} {escape(spec.id)} {score.outcome_status} ({ratio})")
                # Checkpoint after EVERY completion so a mid-run death (quota
                # "Stream closed") never wastes the completed tasks — resume
                # with --resume.
                async with ckpt_lock:
                    scores.append(score)
                    NorthStarCard(scores=scores, created_at=_now_iso(),
                                  corpus_available=corpus_available,
                                  label=label).save(ckpt)

        await asyncio.gather(
            *(_run_spec(s) for s in specs if s.id not in done_ids))

        card = NorthStarCard(scores=scores, created_at=_now_iso(), label=label,
                             corpus_available=corpus_available)
        prev_file = Path(prev_path) if prev_path else RESULTS_DIR / "latest.json"
        previous = NorthStarCard.load(prev_file)
        result = northstar_gate(card, previous)

        # A run RECORDS; it does not publish. Writing latest.json and the
        # committed report as a side effect of finishing is what let a saturated
        # run and a one-spec probe each overwrite the baseline. The result file
        # is immutable and named for its run, so nothing can collide with it.
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = _now_iso().replace(":", "").replace("-", "")[:15]
        out = RESULTS_DIR / f"{_slug(label)}-{stamp}.json"
        # Second-resolution stamps collide for runs started in the same second.
        # A results file is the only record a run leaves, so it never overwrites
        # another one.
        n = 2
        while out.exists():
            out = RESULTS_DIR / f"{_slug(label)}-{stamp}-{n}.json"
            n += 1
        card.save(out)
        ckpt.unlink(missing_ok=True)   # completed cleanly — no partial to resume
        agg = card.as_dict()["aggregate"]
        console.print(
            f"[bold]success {agg['success_rate']:.0%}[/] · "
            f"median cost ratio {agg['median_cost_ratio']} · "
            f"corrections avoided {agg['corrections_avoided']} → "
            f"{out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")
        refusals = publish_refusals(card, previous)
        if refusals:
            console.print("[yellow]not publishable as the baseline:[/]")
            for r in refusals:
                console.print(f"  ⚠ {r}")
        else:
            console.print(f"[dim]publish it with:  nh bench publish {out.name}[/]")
        if not result.passed:
            console.print("[bold red]north-star gate FAILED:[/]")
            for r in result.reasons:
                console.print(f"  ⛔ {r}")
            if gate:
                sys.exit(1)
        else:
            console.print(f"[bold green]north-star gate: {result.reasons[0]}[/]")

    asyncio.run(_go())


def _render_report_or_refuse(card) -> str:
    """Render the report, or REFUSE (exit 1) if it carries a banned term.

    Returns the markdown rather than writing it, so the CALLER decides the write
    order — a guard that runs after the baseline was already saved is not a
    guard, it is a partial publish.

    docs/NORTH_STAR_BENCH.md is TRACKED and its per-task `notes` column is
    judge-authored free text quoting real repo contents, so a publish can drop
    an internal codename into a clean file. That is not hypothetical: a v13
    publish put three of them there (an internal system name glued inside
    `<name>-pipeline`, another codename, and a competitor product) while the
    author's own manual scan of a shorter list reported clean.

    The test-suite guard cannot catch this — it is xfail(strict=False) while a
    vendor-neutral sweep is in progress, so it passes either way. A write path
    into a tracked file has to enforce its own precondition.
    """
    from ..eval.northstar_card import REPORT_MD, render_northstar_md
    from ..eval.vendor_terms import find_banned_terms

    md = render_northstar_md(card)
    found = find_banned_terms(md)
    # The report states "labels and repo paths are pseudonymised". The guard
    # checked vendor terms only, so a home path published happily underneath a
    # sentence asserting it had been removed — a self-certifying honesty claim
    # in the one artifact the project cites as proof.
    if str(Path.home()) in md or "/Users/" in md:
        found = found + ["<a home path>"]
    if found:
        # Redacted locators: enough to find the offending note, without
        # printing the term into a console log that may itself be pasted.
        # Redacted term shapes (first letter + length) so a console log that
        # gets pasted does not carry the term, PLUS the task_ids of the rows
        # that trip it — the shape alone cannot be grepped for.
        where = ", ".join(f"{t[0]}*({len(t)})" for t in found)
        # Look in every field that reaches the render, not just notes: a hit in
        # a project label or the card label refused with a count and no locator.
        def _dirty(text: str) -> bool:
            return bool(find_banned_terms(text)) or "/Users/" in text
        rows = [s.task_id for s in card.scores
                if _dirty(s.notes or "") or _dirty(s.project or "")]
        if _dirty(card.label or ""):
            rows.append(f"(run label {card.label!r})")
        console.print(
            f"[bold red]refusing to publish:[/] the rendered report would "
            f"contain {len(found)} disallowed string(s) [{where}]")
        if rows:
            console.print(f"[dim]offending row(s): {', '.join(rows[:8])}[/]")
        console.print(
            "[dim]the per-task notes are judge-authored free text quoting real "
            "repo contents. Edit the results JSON's `notes` for those rows and "
            "re-publish; --force does NOT override this guard.[/]")
        sys.exit(1)
    return md


@bench.command("publish")
@click.argument("results_file")
@click.option("--force", is_flag=True,
              help="Publish despite the refusals, recording them in the report.")
def bench_publish(results_file: str, force: bool):
    """Promote a results file to the baseline + docs/NORTH_STAR_BENCH.md.

    Publishing is an ACT, not a side effect of finishing a run. A saturated run
    and a one-spec probe have each overwritten the committed report; both were
    "clean completions" as far as the runner could tell.
    """
    from ..eval.northstar_card import (
        REPORT_MD, RESULTS_DIR, NorthStarCard, publish_refusals,
        published_file, render_northstar_md,
    )
    path = Path(results_file)
    if not path.exists():
        path = RESULTS_DIR / results_file
    card = NorthStarCard.load(path)
    if card is None:
        console.print(f"[red]not a readable results file: {path}[/]")
        sys.exit(1)

    previous = NorthStarCard.load(RESULTS_DIR / "latest.json")
    refusals = publish_refusals(card, previous)
    if refusals and not force:
        console.print(f"[bold red]refusing to publish {path.name}:[/]")
        for r in refusals:
            console.print(f"  ⛔ {r}")
        console.print(
            "\n[dim]If you have judged this run publishable anyway, re-run with "
            "--force; the reasons are recorded in the report.[/]")
        sys.exit(1)

    card.override_reasons = refusals if force else []
    # Render and CHECK before either write. The guard used to run after
    # latest.json was already saved, so a refusal left a partial publish: the
    # gate would then compare every later run against a baseline whose report
    # was rejected, and `nh bench report` — which renders from latest.json —
    # would hard-fail forever, leaving no command able to regenerate the
    # tracked file. The sibling refusal path asserts exactly this invariant.
    md = _render_report_or_refuse(card)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    card.save(RESULTS_DIR / "latest.json")
    # SCRUM-25: only a publish that needed no override is a clean baseline —
    # keep it distinct from `latest.json` so a later `--force`d publish (a
    # probe, a saturated run) cannot erase the last trustworthy measurement.
    if not refusals:
        card.save(published_file())
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(md)
    agg = card.as_dict()["aggregate"]
    if refusals:
        console.print("[bold yellow]published WITH --force over:[/]")
        for r in refusals:
            console.print(f"  ⚠ {r}")
    console.print(
        f"[green]published[/] {card.label or path.name} — "
        f"success {agg['success_rate']:.0%} · "
        f"median cost ratio {agg['median_cost_ratio']} · "
        f"{agg['total_nh_tokens']:,} tokens → docs/NORTH_STAR_BENCH.md")


def _load_reviewer_recall_runner():
    """Load eval/reviewer_recall/runner.py by file path.

    That tree sits outside ``src/no_human`` on purpose (SCRUM-29 — "single
    surface: eval/ CLI python only"), so it is loaded dynamically rather than
    imported as a package. This function and the ``--reviewer-recall`` flag
    below are the ONLY things in this file allowed to reference it —
    tests/test_reviewer_recall_guard.py pins that nothing else does.
    """
    import importlib.util

    repo_root = Path(__file__).resolve().parents[3]
    runner_path = repo_root / "eval" / "reviewer_recall" / "runner.py"
    if not runner_path.exists():
        console.print(f"[red]reviewer-recall runner not found at {runner_path}[/]")
        sys.exit(1)
    spec = importlib.util.spec_from_file_location(
        "nh_eval_reviewer_recall_runner", runner_path)
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: the runner's dataclasses resolve their (string,
    # via __future__ annotations) field types through
    # sys.modules[cls.__module__] — unregistered, py3.12 dies with
    # "'NoneType' object has no attribute '__dict__'".
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, repo_root


@bench.command("report")
@click.option("--reviewer-recall", is_flag=True,
             help="Score the fresh-context reviewer against the seeded-defect "
                  "corpus instead (SCRUM-29, docs/REVIEWER_RECALL_METHOD.md).")
def bench_report(reviewer_recall: bool):
    """Re-render docs/NORTH_STAR_BENCH.md from the latest saved results."""
    if reviewer_recall:
        config, _ = _bootstrap()
        module, repo_root = _load_reviewer_recall_runner()
        # markup=False: the per-class breakdown is bracketed ("[logic 2/2, …]")
        # and rich would otherwise swallow it as a style tag.
        console.print(module.run_and_report(repo_root, model=config.review_model),
                      markup=False)
        return

    from ..eval.northstar_card import (
        REPORT_MD, RESULTS_DIR, NorthStarCard, render_northstar_md)

    card = NorthStarCard.load(RESULTS_DIR / "latest.json")
    if card is None:
        console.print("[yellow]no results yet — run `nh bench run` first[/]")
        sys.exit(1)
    REPORT_MD.write_text(_render_report_or_refuse(card))
    console.print(f"[green]report rendered[/] → {REPORT_MD}")


@cli.command("shadow")
@click.argument("title")
@click.option("--repo", required=True, type=click.Path(exists=True), help="Target repo.")
@click.option("--criteria", multiple=True, help="Acceptance criterion (repeatable).")
def shadow_cmd(title, repo, criteria):
    """Shadow-run a task end-to-end in a sandbox clone WITHOUT pushing (21.3)."""
    config, _ = _bootstrap()
    from ..agent.claude_backend import ClaudeBackend
    from ..eval import run_shadow
    from ..review.reviewer import AdversarialReviewer

    backend = ClaudeBackend(
        model=config.primary_model,
        forbidden_paths=config["safety"]["forbidden_paths"],
        never_push_to=config["git"]["never_push_to"],
    )

    async def _go():
        result = await run_shadow(
            config.data, repo_path=str(Path(repo).resolve()), task_title=title,
            backend=backend, acceptance_criteria=list(criteria),
            reviewer=AdversarialReviewer(model=config.review_model),
            on_event=render_event,
        )
        console.rule(f"[bold]shadow: {result.outcome_status}")
        console.print(f"[dim]{result.notes}[/]")
        console.print(result.draft_diff[:8000] or "(no diff produced)")

    asyncio.run(_go())


@cli.command("test")
@click.argument("mode", default="fast", type=click.Choice(["fast", "full", "slow"]))
@click.option("-v", "--verbose", is_flag=True, help="Show full pytest output.")
def test_cmd(mode, verbose):
    """Run the test suite locally (zero LLM tokens).

    Modes:

      fast  — 711 tests, ~28s (skip slow integration tests)
      full  — 721 tests, ~3min (all tests, parallel)
      slow  — 10 tests, ~3min (eval replay + integration only)

    This runs pytest directly as a subprocess — no agent turns, no token cost.
    Use this instead of running 'uv run pytest' inside an AI session.
    """
    import subprocess as _sp

    project_root = Path(__file__).resolve().parents[3]
    script = project_root / "scripts" / "run_tests.sh"

    if script.exists():
        cmd = [str(script), mode]
    else:
        # Fallback if script missing.
        marker_args = {
            "fast": ["-m", "not slow"],
            "slow": ["-m", "slow"],
            "full": [],
        }[mode]
        cmd = ["uv", "run", "pytest", "-q", "--tb=short", "-n", "auto"] + marker_args

    console.print(f"[bold blue]nh test {mode}[/] — running (no LLM tokens spent)")
    if not verbose:
        cmd_str = " ".join(cmd)
        console.print(f"[dim]  {cmd_str}[/]")

    result = _sp.run(cmd, cwd=project_root)

    if result.returncode == 0:
        console.print(f"\n[bold green]✓ All tests passed[/] (mode={mode})")
    else:
        console.print(f"\n[bold red]✗ Tests failed[/] (exit {result.returncode})")
        sys.exit(result.returncode)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
