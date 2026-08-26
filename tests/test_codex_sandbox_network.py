"""Proof that the codex CODER's workspace-write sandbox can be granted network
access, and that the naive fix the ticket's author already tried does not
work.

THE BUG: `codex exec`'s `workspace-write` sandbox (the one `_command` has
always emitted for a non-readonly coder session) has no network access at
all — every git fetch/push, `gh` call, and pip install a codex-backed ticket
needs fails the same way. THE REMEDY: pairing an explicit
`sandbox_mode="workspace-write"` override with
`sandbox_workspace_write.network_access=true` restores it. THE TRAP the
ticket's own author already fell into: passing `network_access=true` alone,
with no `sandbox_mode` override, does NOT work — `network_access` lives
under the `sandbox_workspace_write` POLICY TABLE and is inert unless that
policy is the one actually in force. Every test below reproduces one of
these claims with a real measurement rather than assuming it.

SAFETY — read this before adding a test here. Every test that touches the
real installed `codex` binary uses ONLY the `codex sandbox` debug subcommand
(`codex sandbox -- <cmd>`), NEVER `codex exec`. `codex sandbox` runs a
command inside the CLI's real sandbox construction — traced against the
open-source codex-rs sources (see the comment in `codex_backend.py`'s
`_command`: both `cli/src/debug_sandbox.rs` and the real tool-executor in
`core/src/exec.rs` build their sandboxed argv through the identical
`SandboxManager::transform` machinery) — but it never contacts the model and
never opens a billable session.

`codex exec`, in contrast, DOES start a real turn once its config validates
— even under `--strict-config`, and even with a no-op prompt like `true` —
because `--strict-config` only intercepts genuinely UNKNOWN fields; once
every field resolves, the CLI proceeds exactly as it would for a real ticket.
This was discovered the hard way while developing this fix: a `--strict-
config` probe intended only to check whether a config key's NAME was
recognized in fact ran a full turn (~4,694 tokens) against the live ChatGPT
session on this development machine, because every field involved happened
to be valid. That is why this file answers every "does the CLI recognize or
enforce this key" question BEHAVIOURALLY, through `codex sandbox` plus a
real `git ls-remote`, and never through `codex exec` in any form, strict-
config or otherwise. If a future test needs to add one, don't — find another
behavioural probe instead.

Every test is skipped outright when `codex` is not on PATH: these exercise
the REAL installed CLI (codex-cli 0.149.0 at the time this was written), not
a fixture of it, unlike test_codex_backend.py's hermetic suite. A schema or
default-behaviour drift in a future CLI version should surface here as a
failure, not pass silently forever.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from no_human.agent import codex_backend as cx

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None, reason="codex CLI is not installed on PATH"
)

#: A small, stable, public, read-only target. `git ls-remote` never mutates
#: anything on either end — it is the safe stand-in this whole file uses for
#: "a codex-backed ticket reaches the network", including for the "push"
#: half of the acceptance criterion (see the E2E test below for why).
_PUBLIC_REPO = "https://github.com/octocat/Hello-World.git"
#: octocat/Hello-World's HEAD sha — stable since 2011, used to confirm a
#: successful `ls-remote` returned the REAL answer, not merely exit 0.
_KNOWN_SHA_PREFIX = "7fd1a60b"


def _run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str]:
    """Run `cmd` and return (returncode, combined output) directly off the
    subprocess — never through a pipe such as `| head`, which would report
    the PIPE consumer's exit code instead of the command's own."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout + proc.stderr


def _codex_sandbox(*config_overrides: str, argv: list[str]) -> tuple[int, str]:
    """Run `argv` under `codex sandbox` with the given `-c key=value`
    overrides layered in. Never calls the model — see the module docstring."""
    cli = shutil.which("codex")
    assert cli is not None  # pytestmark already skipped the module otherwise
    cmd = [cli, "sandbox"]
    for kv in config_overrides:
        cmd += ["--config", kv]
    cmd += ["--", *argv]
    return _run(cmd)


# --------------------------------------------------------------------------- #
# AC1: reproduce the defect with a before/after control                       #
# --------------------------------------------------------------------------- #

def test_an_unsandboxed_control_reaches_the_network():
    """The control: no sandbox at all. If THIS fails, the target repo or this
    machine's network is the problem, not codex's sandbox — every other test
    in this file depends on this one passing first."""
    rc, out = _run(["git", "ls-remote", _PUBLIC_REPO, "HEAD"])
    assert rc == 0, out
    assert _KNOWN_SHA_PREFIX in out


def test_the_bare_workspace_write_sandbox_has_no_network():
    """THE bug, measured directly. `git ls-remote` is used rather than
    `curl`: it is the literal first step of the git fetch/push (and `gh`)
    calls a codex-backed ticket actually needs, not just an analogous probe."""
    rc, out = _codex_sandbox(
        'sandbox_mode="workspace-write"',
        argv=["git", "ls-remote", _PUBLIC_REPO, "HEAD"],
    )
    assert rc == 128, out
    assert "Could not resolve host" in out


def test_the_fix_restores_network_access_under_the_real_sandbox():
    """THE remedy, measured the same way: adding
    `sandbox_workspace_write.network_access=true` alongside the SAME
    `workspace-write` mode turns the identical command from rc=128 back to
    rc=0 with the real SHA returned."""
    rc, out = _codex_sandbox(
        'sandbox_mode="workspace-write"',
        "sandbox_workspace_write.network_access=true",
        argv=["git", "ls-remote", _PUBLIC_REPO, "HEAD"],
    )
    assert rc == 0, out
    assert _KNOWN_SHA_PREFIX in out


def test_the_naive_fix_the_ticket_already_tried_still_fails():
    """The ticket author reported that `network_access=true` ALONE did not
    work. Reproduced here rather than trusted: `network_access` is a field of
    the `sandbox_workspace_write` policy table and is inert unless that
    policy is the one actually in force — passing it with no `sandbox_mode`
    override leaves whichever mode the CLI defaults to in force instead, and
    that default is ALSO network-blocked on codex-cli 0.149.0 (asserted
    below rather than assumed, so a future CLI version that changed its
    default would fail this test loudly instead of leaving it a stale
    assumption)."""
    rc_default, out_default = _codex_sandbox(
        argv=["git", "ls-remote", _PUBLIC_REPO, "HEAD"]
    )
    assert rc_default == 128, (
        "the CLI's own default (no `sandbox_mode` override at all) now has "
        f"network access — re-examine this test's premise: {out_default}"
    )

    rc_naive, out_naive = _codex_sandbox(
        "sandbox_workspace_write.network_access=true",
        argv=["git", "ls-remote", _PUBLIC_REPO, "HEAD"],
    )
    assert rc_naive == 128, (
        "the naive fix (network_access=true with no explicit sandbox_mode) "
        "now WORKS on this CLI version — `_command` may no longer need to "
        f"pair it with an explicit `sandbox_mode` override: {out_naive}"
    )
    assert "Could not resolve host" in out_naive


# --------------------------------------------------------------------------- #
# AC2: no narrower grant is available on this CLI version                     #
# --------------------------------------------------------------------------- #

def test_allowed_domains_does_not_narrow_access_on_this_cli_version():
    """`sandbox_workspace_write.allowed_domains` reads like the narrower grant
    this all-or-nothing flag should eventually be replaced with — but on
    codex-cli 0.149.0 it does nothing: restricting to a domain that is NOT
    github.com still permits full access to github.com, identically to not
    setting it at all. Proven behaviourally, never via `--strict-config`
    against `codex exec` (see the module docstring for why), so a future CLI
    version that actually implements narrowing turns this red instead of
    leaving a stale "no narrower option exists" comment uncontradicted."""
    rc, out = _codex_sandbox(
        'sandbox_mode="workspace-write"',
        "sandbox_workspace_write.network_access=true",
        'sandbox_workspace_write.allowed_domains=["example.com"]',
        argv=["git", "ls-remote", _PUBLIC_REPO, "HEAD"],
    )
    assert rc == 0, (
        "allowed_domains now appears to narrow access on this CLI version — "
        f"the 'no narrower grant exists' claim in _command needs updating: {out}"
    )
    assert _KNOWN_SHA_PREFIX in out


def test_network_proxy_is_not_honoured_on_this_cli_version():
    """Same shape, a different candidate narrowing knob: an unreachable,
    bogus proxy should break access if `network_proxy` were a real routing
    setting on this CLI version. It is not — access still succeeds,
    unchanged."""
    rc, out = _codex_sandbox(
        'sandbox_mode="workspace-write"',
        "sandbox_workspace_write.network_access=true",
        'network_proxy="http://127.0.0.1:1"',
        argv=["git", "ls-remote", _PUBLIC_REPO, "HEAD"],
    )
    assert rc == 0, (
        "network_proxy now appears to be honoured on this CLI version — "
        f"re-examine the 'no narrower option exists' claim: {out}"
    )
    assert _KNOWN_SHA_PREFIX in out


# --------------------------------------------------------------------------- #
# AC4: end-to-end — a real codex-backed argv reaches the network             #
# --------------------------------------------------------------------------- #

def _sandbox_overrides_from_backend_argv(built_cmd: list[str]) -> list[str]:
    """Translate the sandbox-relevant slice of a REAL `_command()` argv
    (built for `codex exec`) into the `-c key=value` overrides `codex
    sandbox` needs to reconstruct the same sandbox. `codex sandbox` has no
    `--sandbox <mode>` flag of its own (only `exec` does) — the same mode is
    set via `-c sandbox_mode="<mode>"` instead; every `--config`/`-c` pair
    `_command` emits (network_access included) carries over unchanged."""
    overrides = []
    mode = None
    i = 0
    while i < len(built_cmd):
        tok = built_cmd[i]
        if tok == "--sandbox":
            mode = built_cmd[i + 1]
            i += 2
            continue
        if tok in ("--config", "-c"):
            overrides.append(built_cmd[i + 1])
            i += 2
            continue
        i += 1
    if mode is not None:
        overrides.insert(0, f'sandbox_mode="{mode}"')
    return overrides


def test_e2e_a_real_codex_backed_task_reaches_the_network(tmp_path: Path):
    """AC4, the closest safe proxy available to "a codex-backed task doing
    git fetch/push no longer escalates MISSING_ACCESS" without ever invoking
    a real `codex exec` turn (see the module docstring for why that path is
    off-limits here, permanently). `git fetch` and `git push` both fail at
    the exact same point a blocked sandbox breaks anything — DNS resolution
    and connection, before either's protocol-specific exchange even starts —
    so `git ls-remote`, which forces that identical resolve-and-connect step,
    reproduces the actual failure mode for BOTH, not just a lesser stand-in
    for one of them. (A literal push was deliberately not exercised: it would
    need write access to some remote, which this test has no safe way to
    provision — the DNS/connect failure this ticket is about happens before
    a push's write step is ever reached, so nothing about "push" specifically
    is left unproven by testing ls-remote instead.)

    This drives the REAL argv `CodexBackend()._command()` builds — not a
    hand-written flag list — through `codex sandbox`, so a future change to
    `_command` that silently stopped emitting the grant would fail this test
    even if every other test in test_codex_backend.py stayed green."""
    cli = shutil.which("codex")
    be = cx.CodexBackend(env={"OPENAI_API_KEY": "not-a-real-key"}, cli_path=cli)
    built_cmd = be._command(tmp_path, effort=None, resume=None)
    assert "--config" in built_cmd
    assert "sandbox_workspace_write.network_access=true" in built_cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in built_cmd
    assert "danger-full-access" not in built_cmd

    overrides = _sandbox_overrides_from_backend_argv(built_cmd)
    rc, out = _codex_sandbox(
        *overrides, argv=["git", "ls-remote", _PUBLIC_REPO, "HEAD"]
    )
    assert rc == 0, out
    assert _KNOWN_SHA_PREFIX in out
    assert "Could not resolve host" not in out


def test_e2e_without_the_fix_the_same_real_argv_would_have_failed(tmp_path: Path):
    """The ablation half of the E2E proof, inline: the identical real argv
    with ONLY `network_access` turned off (the pre-fix constructor default
    would have been indistinguishable from this) reproduces the ticket's
    reported failure through the exact same translation path as the test
    above — confirming the E2E pass above is actually attributable to the
    fix, not to something incidental about the translation or the target
    repo."""
    cli = shutil.which("codex")
    be = cx.CodexBackend(
        env={"OPENAI_API_KEY": "not-a-real-key"}, cli_path=cli, network_access=False
    )
    built_cmd = be._command(tmp_path, effort=None, resume=None)
    assert "sandbox_workspace_write.network_access=true" not in built_cmd

    overrides = _sandbox_overrides_from_backend_argv(built_cmd)
    rc, out = _codex_sandbox(
        *overrides, argv=["git", "ls-remote", _PUBLIC_REPO, "HEAD"]
    )
    assert rc == 128, out
    assert "Could not resolve host" in out


# --------------------------------------------------------------------------- #
# The operator's opt-out must MEAN what it says.                              #
#                                                                              #
# `bool(cfg.get("codex_network_access", True))` had two defects, each of which #
# made the running configuration the OPPOSITE of the written one. Both are     #
# pinned here because a capability control that silently ignores the operator  #
# is worse than one that is missing.                                          #
# --------------------------------------------------------------------------- #

import pytest as _pytest  # noqa: E402

from no_human.agent.backend import _codex_network_access  # noqa: E402


@_pytest.mark.parametrize(
    "raw,expected,why",
    [
        ({}, True, "absent -> the documented default"),
        ({"codex_network_access": None}, True,
         "null -> the default, matching `codex_model: null` and "
         "`codex_cli_path: null`; bool(None) used to turn it OFF"),
        ({"codex_network_access": True}, True, "real bool honoured"),
        ({"codex_network_access": False}, False, "real bool honoured"),
        ({"codex_network_access": "false"}, False,
         "QUOTED false: bool('false') is True, so the opt-out used to be "
         "silently ignored and the sandbox kept its network"),
        ({"codex_network_access": "no"}, False, "yaml-ish falsey spelling"),
        ({"codex_network_access": "off"}, False, "yaml-ish falsey spelling"),
        ({"codex_network_access": "0"}, False, "yaml-ish falsey spelling"),
        ({"codex_network_access": "true"}, True, "quoted truthy spelling"),
        ({"codex_network_access": "YES"}, True, "case-insensitive"),
        ({"codex_network_access": " False "}, False, "surrounding whitespace"),
        ({"codex_network_access": 0}, False, "int falsey"),
        ({"codex_network_access": 1}, True, "int truthy"),
    ],
)
def test_the_network_opt_out_resolves_to_what_the_operator_wrote(
        raw, expected, why):
    assert _codex_network_access(raw) is expected, why


@_pytest.mark.parametrize(
    "bad", ["maybe", "flase", "enabled", "1.5", ["true"], {"a": 1}])
def test_an_unparseable_network_setting_refuses_rather_than_guessing(bad):
    """Silently resolving a typo to EITHER direction is how an operator ends up
    with a sandbox posture they did not choose. A loud error at startup is
    cheaper than finding out from a coder's network trace."""
    with _pytest.raises(ValueError, match="codex_network_access"):
        _codex_network_access({"codex_network_access": bad})
