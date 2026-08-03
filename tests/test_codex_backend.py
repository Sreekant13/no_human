"""The coding-backend seam, and the second backend behind it.

TWO THINGS ARE UNDER TEST HERE and they are not the same thing.

  1. THE SEAM. That ``agent/backend.py`` describes what the orchestrator
     actually needs, that the default resolves to the incumbent Claude path
     with identical arguments, and that the four Claude-pinned roles cannot be
     moved by config. This half is the acceptance criterion of the change:
     an operator who edits nothing must get exactly what they had.

  2. THE CODEX BACKEND's event translation, guard evaluation, token
     arithmetic and refusal behaviour, driven over a FAKE ``codex`` process.

WHAT THESE TESTS DO NOT ESTABLISH, stated here rather than left to be
discovered: no test in this file reaches OpenAI, and none runs the real
``codex`` binary. The JSONL fixtures below are written against the documented
``codex exec --json`` envelope; if the real CLI's schema differs, these tests
pass and the backend still fails in production. What they DO pin is everything
downstream of the parse — the guard wiring, the cached-token subtraction, the
NULL-vs-zero output rule, and the refusal paths — which is where the logic
lives. See ``_ITEM_TOOL_NAMES`` for the one table a schema change would move.

No credential is read or written anywhere in this file: the fake env carries
the literal string "not-a-real-key".
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from no_human.agent import backend as seam
from no_human.agent import codex_backend as cx
from no_human.agent.backend import (
    AgentEvent,
    AgentResult,
    BackendCapabilities,
    BackendUnavailable,
    CodingBackend,
    make_backend,
    resolve_backend_name,
)
from no_human.agent.claude_backend import ClaudeBackend

FAKE_ENV = {"OPENAI_API_KEY": "not-a-real-key", "PATH": "/usr/bin:/bin"}


# --------------------------------------------------------------------------- #
# 1. The seam                                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.real_backend
def test_the_default_backend_is_still_the_claude_agent_sdk_path():
    """THE acceptance criterion. No config, empty config, and an explicit
    "claude" all produce the incumbent class."""
    for cfg in (None, {}, {"worker": {}}, {"worker": {"backend": "claude"}}):
        be = make_backend(model="claude-sonnet-5", config=cfg)
        assert isinstance(be, ClaudeBackend), cfg
        assert be.model == "claude-sonnet-5"


@pytest.mark.real_backend
def test_the_default_backend_receives_the_same_arguments_it_always_did():
    """Not just the same CLASS — the same construction. A factory that silently
    dropped `never_push_to` would return a ClaudeBackend that pushes to main."""
    be = make_backend(
        model="claude-sonnet-5",
        config={},
        forbidden_paths=[".env", "custom/"],
        never_push_to=["main", "trunk"],
        readonly=True,
        permission_mode="acceptEdits",
        tool_result_caps={"Bash": 11},
    )
    assert be.forbidden_paths == [".env", "custom/"]
    assert be.never_push_to == ["main", "trunk"]
    assert be.readonly is True
    assert be.permission_mode == "acceptEdits"
    assert be.tool_result_caps == {"Bash": 11}


def test_only_the_coder_role_can_be_moved_off_claude():
    """The review gate and all four model tiers are pinned by constraint. The
    2026-08-01 amendment sanctioned a second CODING backend and moved neither,
    so a `worker.backend` that silently relocated the reviewer would be a
    constraint change wearing the costume of a feature."""
    cfg = {"worker": {"backend": "codex"}}
    assert resolve_backend_name(cfg, role="coder") == "codex"
    for role in seam.CLAUDE_PINNED_ROLES:
        assert resolve_backend_name(cfg, role=role) == "claude", role
    # An unrecognised role defaults to the SAFE side, not to the config value —
    # this is what makes the tuple above documentation rather than the gate.
    assert resolve_backend_name(cfg, role="some-future-role") == "claude"


@pytest.mark.real_backend
def test_a_pinned_role_gets_a_claude_backend_even_under_codex_config():
    be = make_backend(model="claude-opus-5", role="reviewer",
                      config={"worker": {"backend": "codex"}}, readonly=True)
    assert isinstance(be, ClaudeBackend)
    assert be.model == "claude-opus-5"


def test_an_unknown_backend_name_is_refused_by_name():
    with pytest.raises(BackendUnavailable) as exc:
        make_backend(model="m", config={"worker": {"backend": "gemini"}})
    assert "gemini" in str(exc.value)
    assert "claude" in str(exc.value)


@pytest.mark.real_backend
def test_both_backends_satisfy_the_protocol_the_orchestrator_is_typed_against():
    assert isinstance(ClaudeBackend(model="claude-sonnet-5"), CodingBackend)
    assert isinstance(cx.CodexBackend(), CodingBackend)


@pytest.mark.real_backend
def test_both_backends_declare_capabilities_and_they_differ_where_they_must():
    claude = ClaudeBackend(model="claude-sonnet-5").capabilities
    codex = cx.CodexBackend().capabilities
    assert isinstance(claude, BackendCapabilities)
    assert isinstance(codex, BackendCapabilities)
    # The mismatches the report names. Asserted so a future "fix" that flips one
    # of these to True has to say why, in a diff, next to the evidence.
    assert claude.blocks_tool_calls and not codex.blocks_tool_calls
    assert claude.post_tool_hooks and not codex.post_tool_hooks
    assert claude.skills and not codex.skills
    assert claude.subagents and not codex.subagents
    assert claude.incremental_usage and not codex.incremental_usage
    assert claude.cache_creation_accounting and not codex.cache_creation_accounting


def test_the_seam_types_are_the_same_objects_the_old_import_path_yields():
    """~50 sites do `from ...claude_backend import AgentEvent`. Moving the
    dataclasses to the seam must not create a second class: an `isinstance`
    against the other copy would silently start returning False."""
    from no_human.agent import claude_backend as cb

    assert cb.AgentEvent is AgentEvent
    assert cb.AgentResult is AgentResult


def test_the_codex_model_is_its_own_config_key_not_a_claude_tier(monkeypatch):
    """The Claude tier IDs are fixed by constraint. Forwarding `claude-sonnet-5` to
    OpenAI would be both meaningless and a silent tier change."""
    be = make_backend(model="claude-sonnet-5",
                      config={"worker": {"backend": "codex"},
                              "llm": {"codex_model": "gpt-5-codex-mini"}})
    assert isinstance(be, cx.CodexBackend)
    assert be.model == "gpt-5-codex-mini"
    # With no override it takes the module default, never the Claude tier.
    be2 = make_backend(model="claude-sonnet-5",
                       config={"worker": {"backend": "codex"}})
    assert be2.model == seam.DEFAULT_CODEX_MODEL
    assert not be2.model.startswith("claude")


# --------------------------------------------------------------------------- #
# 2. Codex: command construction and the legality gate                         #
# --------------------------------------------------------------------------- #

def test_the_command_forces_api_key_auth_and_never_offers_a_login(monkeypatch):
    """BYO-API-key is the ONLY sanctioned path: OpenAI prohibits using ChatGPT
    to power third-party services. `preferred_auth_method="apikey"` is what
    stops the CLI falling back to a ChatGPT login that happens to be on the
    machine, so it is pinned here rather than left to the CLI's default."""
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    cmd = cx.CodexBackend()._command(Path("/repo"), effort="high", resume=None)
    assert 'preferred_auth_method="apikey"' in cmd
    joined = " ".join(cmd)
    assert "login" not in joined
    assert "chatgpt" not in joined.lower()
    # Never unsandboxed: without a PreToolUse veto the sandbox is the only real
    # boundary left.
    assert "--dangerously-bypass-approvals-and-sandbox" not in joined
    assert "--sandbox" in cmd and "workspace-write" in cmd
    assert "--ask-for-approval" in cmd and "never" in cmd
    assert 'model_reasoning_effort="high"' in cmd


def test_a_readonly_codex_session_gets_the_sandbox_that_enforces_it(monkeypatch):
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    cmd = cx.CodexBackend(readonly=True)._command(
        Path("/repo"), effort=None, resume=None)
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"


def test_resume_continues_a_thread(monkeypatch):
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    cmd = cx.CodexBackend()._command(Path("/repo"), effort=None, resume="th_1")
    assert cmd[1:4] == ["exec", "resume", "th_1"]


def test_a_missing_openai_key_refuses_rather_than_finding_other_auth():
    with pytest.raises(cx.CodexAuthError) as exc:
        cx.CodexBackend(env={"PATH": "/bin"})._child_env()
    msg = str(exc.value)
    assert "OPENAI_API_KEY" in msg
    assert "no subscription path" in msg
    assert "config.yaml" in msg  # the key never lives there


def test_the_claude_credential_is_not_exported_into_the_codex_subprocess():
    """It could not bill anything through `codex`, but any command the agent
    runs could read it. Not exporting it is free."""
    env = cx.CodexBackend(env={
        **FAKE_ENV,
        "CLAUDE_CODE_OAUTH_TOKEN": "not-a-real-token",
        "ANTHROPIC_API_KEY": "not-a-real-key-either",
    })._child_env()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["OPENAI_API_KEY"] == "not-a-real-key"


def test_a_missing_codex_cli_is_an_absence_with_a_name(monkeypatch):
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: None)
    with pytest.raises(BackendUnavailable) as exc:
        cx.CodexBackend()._command(Path("/repo"), effort=None, resume=None)
    assert "npm install -g @openai/codex" in str(exc.value)


# --------------------------------------------------------------------------- #
# 3. Codex: usage arithmetic                                                   #
# --------------------------------------------------------------------------- #

def test_cached_input_is_subtracted_out_of_the_fresh_total():
    """OpenAI reports `input_tokens` INCLUDING `cached_input_tokens`; Anthropic
    reports it EXCLUDING cache reads, and `core.pricing` weights the two
    classes at 1.0 and 0.1. Adding the raw figure would charge every cached
    token twice and fire the budget gate early on exactly the long sessions
    where caching matters most."""
    u = cx._Usage.parse({"input_tokens": 10_000, "cached_input_tokens": 9_000,
                         "output_tokens": 500})
    assert u.cache_read_tokens == 9_000
    assert u.tokens_used == 1_000 + 500       # NOT 10_000 + 500
    assert u.output_tokens == 500


def test_a_usage_block_that_reports_nothing_is_absent_not_zero():
    assert cx._Usage.parse({"input_tokens": 0, "output_tokens": 0}) is None
    assert cx._Usage.parse(None) is None
    assert cx._Usage.parse("nonsense") is None


def test_a_cached_share_larger_than_the_input_clamps_instead_of_going_negative():
    u = cx._Usage.parse({"input_tokens": 100, "cached_input_tokens": 900,
                         "output_tokens": 7})
    assert u.tokens_used == 7  # 0 fresh, not -793


# --------------------------------------------------------------------------- #
# 4. Codex: a whole run over a fake `codex` process                            #
# --------------------------------------------------------------------------- #

def _fake_codex(lines: list[dict], *, returncode: int = 0, stderr: bytes = b""):
    """Monkeypatch target for ``asyncio.create_subprocess_exec``.

    A real subprocess is deliberately avoided: the point of these tests is the
    NORMALIZER, and shelling out would make them depend on a binary this
    machine does not have.
    """
    payload = "\n".join(json.dumps(line) for line in lines).encode() + b"\n"

    class _Stdin:
        def write(self, _data): pass
        async def drain(self): pass
        def close(self): pass

    class _Reader:
        def __init__(self, data: bytes):
            self._lines = data.splitlines(keepends=True)
        async def readline(self) -> bytes:
            return self._lines.pop(0) if self._lines else b""
        async def read(self) -> bytes:
            return b"".join(self._lines)

    class _Proc:
        def __init__(self):
            self.stdin, self.stdout = _Stdin(), _Reader(payload)
            self.stderr = _Reader(stderr)
            self.returncode = None
            self.killed = False
        def kill(self): self.killed = True
        async def wait(self):
            self.returncode = returncode
            return returncode

    proc = _Proc()

    async def _spawn(*_args, **_kwargs):
        return proc

    _spawn.proc = proc
    return _spawn


def _run(backend, monkeypatch, lines, *, max_turns=40, on_event=None, **kw):
    spawn = _fake_codex(lines, **kw)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    result = asyncio.run(backend.run(
        "do the thing", cwd=Path("/repo"), max_turns=max_turns,
        on_event=on_event))
    return result, spawn.proc


_HAPPY = [
    {"type": "thread.started", "thread_id": "th_42"},
    {"type": "item.completed", "item": {"id": "i0", "type": "reasoning",
                                        "text": "thinking about it"}},
    {"type": "item.started", "item": {"id": "i1", "type": "command_execution",
                                      "command": ["pytest", "-q"]}},
    {"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                        "aggregated_output": "5 passed",
                                        "exit_code": 0}},
    {"type": "item.started", "item": {"id": "i2", "type": "file_change",
                                      "changes": {"src/a.py": {"kind": "update"}}}},
    {"type": "item.completed", "item": {"id": "i2", "type": "file_change",
                                        "changes": {"src/a.py": {"kind": "update"}}}},
    {"type": "item.completed", "item": {"id": "i3", "type": "agent_message",
                                        "text": "done: fixed the bug"}},
    {"type": "turn.completed", "usage": {"input_tokens": 12_000,
                                         "cached_input_tokens": 11_000,
                                         "output_tokens": 900}},
]


def test_a_successful_run_produces_the_result_the_orchestrator_reads(monkeypatch):
    events: list[AgentEvent] = []
    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, _HAPPY,
                     on_event=events.append)
    assert result.is_error is False
    assert result.final_text == "done: fixed the bug"
    assert result.session_id == "th_42"
    assert result.stop_reason == "end_turn"
    assert result.tokens_used == 1_000 + 900
    assert result.cache_read_tokens == 11_000
    assert result.cache_creation_tokens == 0
    assert result.output_tokens == 900
    assert result.num_turns == 2  # one command, one file change
    kinds = [e.kind for e in events]
    assert kinds[-1] == "result"
    assert "thinking" in kinds and "text" in kinds
    assert "tool_use" in kinds and "tool_result" in kinds and "usage" in kinds


def test_tool_use_events_carry_the_vocabulary_the_orchestrator_switches_on(
        monkeypatch):
    """`_agent_sink` keys the doom-loop detector and, critically, the
    committed-file set on `tool_name in ("Write", "Edit", ...)` and
    `tool_input["file_path"]`. A backend that reported `file_change` /
    `changes` instead would commit nothing the agent wrote."""
    events: list[AgentEvent] = []
    _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, _HAPPY,
         on_event=events.append)
    uses = [e for e in events if e.kind == "tool_use"]
    assert [e.tool_name for e in uses] == ["Bash", "Write"]
    assert uses[0].tool_input == {"command": "pytest -q"}
    assert uses[1].tool_input == {"file_path": "src/a.py"}
    assert all(e.meta.get("tool_use_id") for e in uses)


def test_one_patch_touching_several_files_is_several_guard_checks(monkeypatch):
    """A per-path policy checked once per ITEM passes a patch whose SECOND hunk
    rewrites .env. One event per path is the whole reason `_tool_inputs`
    returns a list."""
    lines = [
        {"type": "item.started", "item": {
            "id": "i1", "type": "file_change",
            "changes": {"src/a.py": {}, "src/b.py": {}, "src/c.py": {}}}},
    ]
    events: list[AgentEvent] = []
    _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, lines,
         on_event=events.append)
    paths = [e.tool_input["file_path"] for e in events if e.kind == "tool_use"]
    assert sorted(paths) == ["src/a.py", "src/b.py", "src/c.py"]


def test_output_tokens_stays_null_when_no_usage_was_ever_reported(monkeypatch):
    """0 asserts "this run emitted no output"; None says "never reported". The
    distinction is load-bearing all the way to the DB column."""
    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, [
        {"type": "item.completed",
         "item": {"id": "i", "type": "agent_message", "text": "hi"}},
    ])
    assert result.output_tokens is None
    assert result.tokens_used == 0


# --------------------------------------------------------------------------- #
# 5. Codex: the guard, and what it can and cannot do                           #
# --------------------------------------------------------------------------- #

_PUSH_TO_MAIN = [
    {"type": "item.started", "item": {"id": "i1", "type": "command_execution",
                                      "command": ["git", "push", "origin", "main"]}},
    {"type": "item.completed", "item": {"id": "i9", "type": "agent_message",
                                        "text": "pushed"}},
]


def test_a_guard_violation_kills_the_session_and_fails_the_attempt(monkeypatch):
    events: list[AgentEvent] = []
    result, proc = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch,
                        _PUSH_TO_MAIN, on_event=events.append)
    assert result.is_error is True
    assert result.stop_reason == "guard"
    assert result.denials and "main" in result.denials[0]
    assert proc.killed is True
    # Nothing after the violation is processed — the "pushed" message never
    # reaches the transcript.
    assert not any(e.kind == "text" and "pushed" in e.text for e in events)


def test_the_denial_event_marks_itself_post_hoc(monkeypatch):
    """THE capability mismatch, made unmissable at the point of use. On the
    Claude path the call is DENIED before it runs; here the push has already
    happened when we see it. A reader (or a future dashboard) must not be able
    to confuse the two."""
    events: list[AgentEvent] = []
    _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, _PUSH_TO_MAIN,
         on_event=events.append)
    denied = [e for e in events if e.kind == "denied"]
    assert len(denied) == 1
    assert denied[0].meta["post_hoc"] is True


def test_a_forbidden_path_write_is_caught_by_the_same_pure_policy(monkeypatch):
    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, [
        {"type": "item.started", "item": {"id": "i1", "type": "file_change",
                                          "changes": {".env": {}}}},
    ])
    assert result.is_error and result.stop_reason == "guard"
    assert ".env" in result.denials[0]


def test_the_task_scoped_guard_lists_are_mutable_like_the_claude_backends():
    """The worker pool reuses one backend instance across tasks and the
    orchestrator rewrites both lists per task. A backend with frozen lists
    would carry one repo's protections into the next repo."""
    be = cx.CodexBackend()
    be.forbidden_paths = ["custom/"]
    be.never_push_to = ["develop"]
    assert be._guard_events("Bash", {"command": "git push origin develop"})
    assert be._guard_events("Write", {"file_path": "custom/x"})
    assert be._guard_events("Write", {"file_path": ".env"}) is None  # no longer listed


# --------------------------------------------------------------------------- #
# 6. Codex: bounds, failures, and refusals                                     #
# --------------------------------------------------------------------------- #

def test_max_turns_is_enforced_here_because_codex_does_not_enforce_it(monkeypatch):
    lines = [
        {"type": "item.started", "item": {"id": f"i{n}", "type":
                                          "command_execution", "command": ["ls"]}}
        for n in range(10)
    ]
    result, proc = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, lines,
                        max_turns=3)
    assert result.num_turns == 3
    assert result.stop_reason == "max_turns"
    assert result.is_error is True
    assert "maximum number of turns" in result.final_text
    assert proc.killed is True


def test_a_vendor_error_becomes_a_failed_attempt_not_a_crash(monkeypatch):
    """Constraint §5: the bounded loop reads the result event. A backend that
    raised would crash the daemon instead of failing one attempt."""
    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, [
        {"type": "turn.failed", "error": {"message": "insufficient_quota"}},
    ])
    assert result.is_error is True
    assert "insufficient_quota" in result.final_text


def test_a_nonzero_exit_with_no_json_still_yields_a_result_event(monkeypatch):
    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, [],
                     returncode=2, stderr=b"codex: boom")
    assert result.is_error is True
    assert "boom" in result.final_text


def test_unparseable_and_unknown_records_are_skipped_not_fatal(monkeypatch):
    """A schema drift must degrade, not read as a code failure to the bounded
    loop — which would retry three times and escalate against the wrong cause."""
    spawn = _fake_codex([{"type": "item.completed", "item": {
        "id": "i", "type": "agent_message", "text": "ok"}}])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    # Splice in a banner line and a record of a type nobody here has seen.
    spawn.proc.stdout._lines.insert(0, b"Reading prompt from stdin...\n")
    spawn.proc.stdout._lines.insert(1, b'{"type":"some.future.event","x":1}\n')
    spawn.proc.stdout._lines.insert(2, b"{not json\n")
    result = asyncio.run(cx.CodexBackend(env=FAKE_ENV).run(
        "p", cwd=Path("/repo"), max_turns=9))
    assert result.is_error is False
    assert result.final_text == "ok"


@pytest.mark.parametrize("kwargs,needle", [
    ({"supervisor_hook": object()}, "supervisor_hook"),
    ({"lint_hook": object()}, "lint_hook"),
    ({"skills": ["verify"]}, "skills"),
    ({"agents": {"researcher": object()}}, "agents"),
])
def test_an_unsupported_control_is_refused_never_silently_dropped(
        monkeypatch, kwargs, needle):
    """A supervisor that never fires is worse than no supervisor, because the
    orchestrator reports that it supervised. Refusing is the only honest
    behaviour available to a backend that cannot run the check."""
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    result = asyncio.run(cx.CodexBackend(env=FAKE_ENV).run(
        "p", cwd=Path("/repo"), max_turns=5, **kwargs))
    assert result.is_error is True
    assert result.stop_reason == "unsupported"
    assert needle in result.final_text


def test_an_exception_raised_by_on_event_propagates_out_of_run(monkeypatch):
    """The orchestrator's three abort controls — CancelRequested (nh task
    pause), BudgetAbort (the mid-attempt spend watch) and StuckAbort
    (doom-loop) — are ALL raised from inside `_agent_sink`. A backend that
    swallowed a callback exception would silently disable all three, and the
    only visible symptom would be attempts that never stop."""
    class _Abort(Exception):
        pass

    def _sink(event):
        if event.kind == "tool_use":
            raise _Abort("stop now")

    spawn = _fake_codex(_HAPPY)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    with pytest.raises(_Abort):
        asyncio.run(cx.CodexBackend(env=FAKE_ENV).run(
            "p", cwd=Path("/repo"), max_turns=40, on_event=_sink))
    # …and the subprocess is killed on the way out, or a cancelled attempt
    # leaves a live `codex` writing into the tree about to be committed.
    assert spawn.proc.killed is True


# --------------------------------------------------------------------------- #
# 7. The credential rules (config.py)                                          #
# --------------------------------------------------------------------------- #
#
# No real credential appears below. Every value is a literal placeholder.

def test_the_openai_key_is_refused_in_config_yaml_like_the_anthropic_one(
        tmp_path):
    """The mode may live in config; the key never does. A guard that named only
    the first vendor would have missed the second — which is the whole reason
    the second vendor's key is named in the same breath."""
    from no_human import config as cfgmod

    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "openai_api_key"):
        with pytest.raises(cfgmod.AuthError) as exc:
            cfgmod._reject_api_key_in_config({"llm": {key: "placeholder"}})
        assert key.upper() in str(exc.value)
        assert ".env" in str(exc.value)
    # A key-shaped VALUE is not a key-shaped NAME; this guard is about names.
    cfgmod._reject_api_key_in_config({"worker": {"backend": "codex"}})


def test_codex_mode_requires_the_key_and_says_there_is_no_subscription_path(
        tmp_path, monkeypatch):
    from no_human import config as cfgmod

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    empty = tmp_path / ".env"
    empty.write_text("")
    with pytest.raises(cfgmod.AuthError) as exc:
        cfgmod.assert_codex_api_key_mode(empty)
    msg = str(exc.value)
    assert "OPENAI_API_KEY" in msg
    assert "no subscription path" in msg
    assert "ChatGPT" in msg           # names the reason, not just the rule
    assert "config.yaml" in msg


def test_codex_mode_scrubs_the_routings_that_would_move_the_bill(
        tmp_path, monkeypatch):
    """`assert_codex_api_key_mode` is the OpenAI half of "exactly one billing
    path per vendor": the key the operator chose stays, every redirect to
    another endpoint or another account goes."""
    from no_human import config as cfgmod

    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=placeholder-not-a-real-key\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for var in cfgmod.CODEX_ALTERNATE_ROUTING_VARS:
        monkeypatch.setenv(var, "placeholder")

    report = cfgmod.assert_codex_api_key_mode(env_file)

    assert sorted(report.removed) == sorted(cfgmod.CODEX_ALTERNATE_ROUTING_VARS)
    import os
    for var in cfgmod.CODEX_ALTERNATE_ROUTING_VARS:
        assert var not in os.environ
    assert os.environ["OPENAI_API_KEY"] == "placeholder-not-a-real-key"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_the_openai_key_is_not_added_to_the_anthropic_scrub_list():
    """METERED_AUTH_VARS runs on EVERY start, including runs that use no
    OpenAI at all. Widening it would delete an operator's OPENAI_API_KEY out of
    their shell for a Claude-only run — a different program's setting, removed
    by us, silently."""
    from no_human import config as cfgmod

    assert cfgmod.CODEX_API_KEY_VAR not in cfgmod.METERED_AUTH_VARS
    assert not set(cfgmod.CODEX_ALTERNATE_ROUTING_VARS) & set(
        cfgmod.METERED_AUTH_VARS)


def test_the_default_config_still_selects_claude():
    """The acceptance criterion, at the config layer."""
    from no_human.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["worker"]["backend"] == "claude"
    assert resolve_backend_name(DEFAULT_CONFIG) == "claude"
    # And the Codex keys exist with inert defaults, so reading them never
    # depends on a key the operator has not written.
    assert DEFAULT_CONFIG["llm"]["codex_reasoning_effort"] is None
    assert DEFAULT_CONFIG["llm"]["codex_cli_path"] is None
    # The four Claude tiers are untouched by this change.
    assert DEFAULT_CONFIG["llm"]["primary_model"] == "claude-sonnet-5"
    assert DEFAULT_CONFIG["llm"]["review_model"] == "claude-opus-5"
    assert DEFAULT_CONFIG["llm"]["planner_model"] == "claude-opus-5"
    assert DEFAULT_CONFIG["llm"]["supervisor_model"] == "claude-sonnet-5"
    assert DEFAULT_CONFIG["llm"]["utility_model"] == "claude-haiku-4-5"


@pytest.mark.real_backend
def test_every_boolean_capability_is_true_for_claude():
    """THE MECHANISM by which the Claude path is unchanged, asserted by
    DISCOVERY rather than by a hand-written list.

    The orchestrator gates each optional feature on `caps.<field>`, so the
    Claude path is byte-for-byte the old behaviour precisely because every one
    of those fields is True. A future capability field added as False-for-Claude
    would silently switch a feature off for the DEFAULT backend, and an
    enumerated check could not catch it — the field it would need to name is the
    one that does not exist yet.
    """
    import dataclasses

    caps = ClaudeBackend(model="claude-sonnet-5").capabilities
    false_fields = [
        f.name for f in dataclasses.fields(caps)
        if f.type in ("bool", bool) and getattr(caps, f.name) is not True
    ]
    assert not false_fields, (
        f"the DEFAULT backend declares {false_fields} as not-True. Every "
        "orchestrator feature gate reads these, so a False here turns a "
        "feature off for every operator who changed nothing.")
    # …and the check is not vacuous: it really did look at fields.
    assert sum(1 for f in dataclasses.fields(caps)
               if f.type in ("bool", bool)) >= 8
