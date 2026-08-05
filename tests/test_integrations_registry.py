"""Integrations registry: status list + health checks over the config.

The registry is a STATUS layer — jira/circleci are first-class config sections;
github/gitlab/jenkins are read-only views over ``ci.*`` and slack over
``notifications.*``. Secrets are never surfaced in a status detail.
"""

import asyncio
import logging
import os
import socket
import subprocess
from pathlib import Path

import pytest

from no_human import integrations as reg
from no_human.integrations import (
    IntegrationStatus,
    list_integrations,
    list_integrations_with_ambient,
)
# NB: the product fn is `integrations.test_integration`; referenced as
# `reg.test_integration` so pytest doesn't collect it as a test case.

_CONFIGURED_GITHUB_CFG = {
    "integrations": {}, "notifications": {},
    "ci": {"enabled": True, "backend": "github_actions", "project": "o/r", "job": ""},
}
_CONFIGURED_GITLAB_CFG = {
    "integrations": {}, "notifications": {},
    "ci": {"enabled": True, "backend": "gitlab", "project": "ns/r", "job": ""},
}
_UNCONFIGURED_CFG = {"integrations": {}, "ci": {}, "notifications": {}}


@pytest.mark.parametrize("name,configured_cfg", [
    ("github", _CONFIGURED_GITHUB_CFG),
    ("gitlab", _CONFIGURED_GITLAB_CFG),
])
def test_ambient_stays_configured_when_stored_config_present(mock_ambient_probes, name, configured_cfg):
    # Even if the CLI is also ambiently authenticated, a stored/configured
    # integration must never be downgraded/relabelled to "ambient".
    mock_ambient_probes._AMBIENT_PROBES[name] = lambda: True
    st = {s.name: s for s in list_integrations_with_ambient(configured_cfg, cache={})}
    assert st[name].configured is True
    assert st[name].status == "configured"


@pytest.mark.parametrize("name", ["github", "gitlab"])
def test_ambient_reported_when_unconfigured_and_cli_authenticated(mock_ambient_probes, name):
    mock_ambient_probes._AMBIENT_PROBES[name] = lambda: True
    st = {s.name: s for s in list_integrations_with_ambient(_UNCONFIGURED_CFG, cache={})}
    assert st[name].configured is False
    assert st[name].status == "ambient"


@pytest.mark.parametrize("name", ["github", "gitlab"])
def test_unconfigured_stays_unconfigured_when_cli_not_authenticated(mock_ambient_probes, name):
    mock_ambient_probes._AMBIENT_PROBES[name] = lambda: False
    st = {s.name: s for s in list_integrations_with_ambient(_UNCONFIGURED_CFG, cache={})}
    assert st[name].configured is False
    assert st[name].status == "unconfigured"


def test_ambient_available_caches_within_ttl_without_reprobing(monkeypatch):
    calls = []

    def fake_probe():
        calls.append(1)
        return True

    monkeypatch.setitem(reg._AMBIENT_PROBES, "github", fake_probe)
    cache = {}
    assert reg.ambient_available("github", cache=cache, now=1000.0) is True
    assert reg.ambient_available("github", cache=cache, now=1030.0) is True  # within 60s
    assert len(calls) == 1  # not re-probed
    assert reg.ambient_available("github", cache=cache, now=1070.0) is True  # past TTL (>60s)
    assert len(calls) == 2


def test_ambient_cache_never_stores_a_credential_only_bool_and_timestamp(monkeypatch):
    monkeypatch.setitem(reg._AMBIENT_PROBES, "github", lambda: True)
    cache = {}
    reg.ambient_available("github", cache=cache, now=5.0)
    ts, result = cache["github"]
    assert isinstance(ts, float)
    assert isinstance(result, bool)


# --------------------------------------------------------------------------- #
# "No ambient probe performs network I/O" — the class-wide property.           #
#                                                                              #
# An ambient probe fires when an integration is UNCONFIGURED. That is the one  #
# moment a privacy-conscious reader is entitled to assume nothing is sent, so  #
# it is the one moment egress is least acceptable — and `_probe_github_ambient`#
# shipped for months running `gh auth status`, which validates the stored      #
# token against the GitHub API (measured 1.7-2.0s on a dev machine, against    #
# ~10ms for a local credential read). The property below is pinned for the     #
# WHOLE CLASS rather than that one function, because the next probe will be    #
# written by somebody who never read this comment.                             #
# --------------------------------------------------------------------------- #


class _NetworkAttempted(RuntimeError):
    """Raised by ``block_network`` the moment a probe reaches for the wire."""


#: argv[0]s an ambient probe is allowed to spawn. An ALLOWLIST, not a denylist:
#: the defect being guarded against is a program nobody realised made a network
#: call, and a denylist can only ever name the programs somebody already knew
#: about. A probe that spawns anything else fails until a human justifies it.
_LOCAL_ONLY_PROGRAMS = {
    "git": "`git credential fill` consults locally configured credential "
           "helpers and returns immediately if none has anything; "
           "GIT_TERMINAL_PROMPT=0 + a no-op GIT_ASKPASS keep it from blocking",
}


@pytest.fixture
def block_network(monkeypatch):
    """Make any attempt to reach the network an immediate, deterministic error.

    Two arms, because a probe can egress two ways and each is invisible to the
    other's guard:

    * **In-process.** ``socket.socket`` / ``socket.create_connection`` /
      ``socket.getaddrinfo`` are replaced with raisers, so a Python-level
      connection (httpx, urllib, a raw socket) dies at the syscall boundary.
      This is asserted, not timed: a timing threshold is flaky, and a slow
      local keychain read and a fast HTTP round-trip are indistinguishable.
    * **In a child.** Monkeypatching *this* process's ``socket`` says nothing
      whatsoever about a subprocess — ``gh auth status`` opened its socket in a
      child, which is exactly why the bug survived every existing test. So the
      spawn is intercepted and argv[0] checked against ``_LOCAL_ONLY_PROGRAMS``;
      an allowlisted program is then run for real, so the probe under test
      executes the code path a user actually gets.

      The interception point is ``subprocess.Popen``, NOT ``subprocess.run``.
      An audit of 14 spawn vectors found five that bypass a ``run``-only guard
      — ``subprocess.Popen``, ``subprocess.call``, ``os.popen``, ``os.system``,
      ``os.spawnv`` — and one, ``asyncio.create_subprocess_exec``, that a
      ``run``-only guard blocked merely *incidentally*, via the socket arm
      firing on event-loop construction, which does not happen from an
      already-running loop. That already-running loop is the real production
      path: ``_check_view`` reaches these probes through
      ``asyncio.to_thread``. ``run``/``call``/``check_output``/``os.popen``
      and asyncio's own ``_UnixSubprocessTransport`` all funnel through
      ``Popen``, so guarding it covers them by construction rather than by
      luck. ``os.system`` and the ``os.spawn*``/``os.posix_spawn`` family do
      not funnel through anything, so they are blocked outright — no ambient
      probe has any business reaching for them.

    WHAT THIS CANNOT SEE, stated so the guard is not oversold (the same honesty
    ``tests/test_egress_disclosure.py`` states about itself): an *allowlisted*
    program that grows a network call of its own, or that spawns a GRANDCHILD
    of its own — a Git Credential Manager doing an OAuth refresh would pass
    this, and so does an arbitrary helper reached via ``GIT_CONFIG_GLOBAL``
    (demonstrated: the harness sees only ``['git']``). It bounds what our code
    asks for, not what every binary on the machine then does.

    Yields the list of argv[0]s the probe spawned, so a test can assert on it.
    """
    def _blocked(*_a, **_k):
        raise _NetworkAttempted("a probe attempted network I/O in this process")

    real_socket = socket.socket

    def _blocked_socket(family=socket.AF_INET, *args, **kwargs):
        # AF_UNIX is local IPC and cannot reach the network; asyncio builds its
        # event-loop self-pipe out of one, so blocking it would make this
        # harness unusable from an async test while adding no safety at all.
        if family == getattr(socket, "AF_UNIX", object()):
            return real_socket(family, *args, **kwargs)
        raise _NetworkAttempted("a probe attempted network I/O in this process")

    monkeypatch.setattr(socket, "socket", _blocked_socket)
    for attr in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, attr, _blocked)

    def _blocked_spawn(*_a, **_k):
        raise _NetworkAttempted(
            "an ambient probe reached for a raw os-level spawn primitive; "
            "these bypass the argv allowlist entirely and no probe needs one"
        )

    # Not funnelled through Popen — blocked outright. getattr-guarded because
    # the os.spawn*/posix_spawn set is not identical across platforms.
    for attr in ("system", "spawnv", "spawnve", "spawnvp", "spawnvpe",
                 "posix_spawn", "posix_spawnp", "execv", "execvp"):
        if hasattr(os, attr):
            monkeypatch.setattr(os, attr, _blocked_spawn)

    real_popen = subprocess.Popen
    spawned: list[str] = []

    def _guarded_popen(args, *rest, **kwargs):
        argv0 = args[0] if isinstance(args, (list, tuple)) and args else args
        spawned.append(argv0)
        if argv0 not in _LOCAL_ONLY_PROGRAMS:
            raise _NetworkAttempted(
                f"an ambient probe spawned {argv0!r}, which is not in "
                "_LOCAL_ONLY_PROGRAMS. An ambient probe must establish that a "
                "credential is PRESENT without validating it. If this program "
                "really is local, add it there with the evidence."
            )
        return real_popen(args, *rest, **kwargs)

    monkeypatch.setattr(reg.subprocess, "Popen", _guarded_popen)
    return spawned


def test_block_network_catches_an_in_process_connection(block_network):
    """Control, arm 1. A green below must mean 'nothing connected', never 'the
    harness is inert' — so show it catching a known positive first."""
    with pytest.raises(_NetworkAttempted):
        socket.create_connection(("example.invalid", 443))


def test_block_network_catches_the_pre_fix_github_probe(block_network):
    """Control, arm 2 — and the red-green proof kept in the suite.

    This is ``_probe_github_ambient`` EXACTLY as it shipped before this fix.
    `gh auth status` is not a local check: it puts the operator's GitHub token
    on the wire to validate it, from a probe whose whole purpose is to run when
    GitHub is unconfigured. If this stops raising, the harness has gone blind
    and every assertion below it is worthless."""
    def _pre_fix_probe_github_ambient() -> bool:
        proc = reg._run_probe(["gh", "auth", "status"])
        return proc is not None and proc.returncode == 0

    with pytest.raises(_NetworkAttempted, match="'gh'"):
        _pre_fix_probe_github_ambient()
    assert block_network == ["gh"]


@pytest.mark.parametrize("spawn", [
    pytest.param(lambda: subprocess.run(["gh", "auth", "status"]), id="subprocess.run"),
    pytest.param(lambda: subprocess.Popen(["gh", "auth", "status"]), id="subprocess.Popen"),
    pytest.param(lambda: subprocess.call(["gh", "auth", "status"]), id="subprocess.call"),
    pytest.param(lambda: subprocess.check_output(["gh", "auth", "status"]),
                 id="subprocess.check_output"),
    pytest.param(lambda: os.popen("gh auth status"), id="os.popen"),
])
def test_block_network_catches_every_popen_backed_spawn(block_network, spawn):
    """Control, arm 2 — the escape matrix. A ``subprocess.run``-only guard let
    four of these five straight through; all five funnel through ``Popen``, so
    guarding that one point closes them by construction. Each is asserted
    individually rather than assumed, because "it funnels through Popen" is a
    claim about CPython internals and this repo has been wrong before about a
    mechanism it only reasoned about. `os.popen` takes a shell STRING, so the
    reported argv[0] is the whole command — still refused, since only exact
    allowlist members pass."""
    with pytest.raises(_NetworkAttempted, match="gh"):
        spawn()


@pytest.mark.parametrize("name,call", [
    ("os.system", lambda: os.system("gh auth status")),
    ("os.spawnv", lambda: os.spawnv(os.P_WAIT, "/bin/sh", ["sh", "-c", "gh auth status"])),
])
def test_block_network_catches_raw_os_spawn_primitives(block_network, name, call):
    """These funnel through nothing — no ``Popen``, no allowlist — so they are
    refused outright rather than argv-checked."""
    with pytest.raises(_NetworkAttempted):
        call()


async def test_block_network_catches_an_asyncio_subprocess_from_a_running_loop(block_network):
    """The vector the previous guard blocked only by ACCIDENT. A
    ``subprocess.run``-only guard never saw ``asyncio.create_subprocess_exec``;
    what stopped it was the socket arm firing while the event loop was being
    constructed — which does not happen when a loop is ALREADY running. That is
    the production shape: ``_check_view`` reaches the probes from a live loop
    via ``asyncio.to_thread``. This test runs inside a running loop precisely so
    the incidental block cannot be what passes it."""
    assert asyncio.get_running_loop().is_running()
    with pytest.raises(_NetworkAttempted, match="'gh'"):
        await asyncio.create_subprocess_exec("gh", "auth", "status")


def test_block_network_lets_an_allowlisted_program_through(block_network):
    """The other half of a discriminating guard: it must not simply refuse
    everything, or a green anywhere above would mean nothing."""
    assert reg._run_probe(["git", "--version"]) is not None
    assert block_network == ["git"]


def test_ambient_probe_inventory_is_not_empty():
    """The corpus guard. An empty ``_AMBIENT_PROBES`` would make the
    parametrised property below vacuous rather than failing — empty is not
    zero. Re-derived 2026-08-02: exactly github and gitlab have an ambient
    path; jira/linear/jenkins/circleci/slack/teams have no 'already
    authenticated CLI' concept."""
    assert set(reg._AMBIENT_PROBES) >= {"github", "gitlab"}


@pytest.mark.parametrize("name", sorted(reg._AMBIENT_PROBES))
def test_no_ambient_probe_performs_network_io(block_network, name):
    """THE property, pinned over ``_AMBIENT_PROBES`` so a third probe is
    covered the day it is added and not the day somebody remembers.

    Detecting whether a credential is PRESENT never requires validating it —
    validity is settled later, visibly, at the point of use."""
    result = reg._AMBIENT_PROBES[name]()
    assert type(result) is bool


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess — only the two fields the
    probes actually read."""
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_run_probe_returns_process_on_success(monkeypatch):
    monkeypatch.setattr(reg.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=0))
    proc = reg._run_probe(["true"])
    assert proc is not None
    assert proc.returncode == 0


def test_run_probe_returns_none_on_os_error(monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError("no such file or directory")
    monkeypatch.setattr(reg.subprocess, "run", _raise)
    assert reg._run_probe(["nonexistent-binary"]) is None


def test_run_probe_returns_none_on_timeout(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else k.get("cmd"), timeout=k.get("timeout", 2.0))
    monkeypatch.setattr(reg.subprocess, "run", _raise)
    assert reg._run_probe(["slow-binary"]) is None


def test_run_probe_passes_a_finite_timeout_to_subprocess_run(monkeypatch):
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(reg.subprocess, "run", _fake_run)
    reg._run_probe(["git", "credential", "fill"])
    assert isinstance(captured.get("timeout"), (int, float))
    # Pinned to the shipped default: 5.0 was the pre-SCRUM-81 value, and a
    # loose bound here let a revert back to it pass unnoticed.
    assert 0 < captured["timeout"] <= 2.0


#: A value no real token can collide with, used to assert that no detection
#: path ever lets a credential out of the probe.
_SENTINEL_TOKEN = "gho_SENTINEL_MUST_NEVER_ESCAPE_00000000"


@pytest.fixture
def gh_config(monkeypatch, tmp_path):
    """Isolate `_probe_github_ambient` from the developer's real gh install:
    no token env vars, an empty config dir, and no git credential helper with
    anything to say. Returns the `hosts.yml` path the probe will read."""
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(reg, "_git_credential_present", lambda host: False)
    cfg = tmp_path / "gh"
    cfg.mkdir()
    monkeypatch.setenv("GH_CONFIG_DIR", str(cfg))
    return cfg / "hosts.yml"


@pytest.mark.parametrize("var", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_probe_github_ambient_true_when_token_env_var_is_set(gh_config, monkeypatch, var):
    # Detection path 1 — what gh itself prefers over any stored credential,
    # and the only one visible inside a container/CI.
    monkeypatch.setenv(var, _SENTINEL_TOKEN)
    assert reg._probe_github_ambient() is True


def test_probe_github_ambient_false_when_token_env_var_is_blank(gh_config, monkeypatch):
    # `GH_TOKEN=""` is how a shell UNSETS a token for one command; it is an
    # absent credential, not a present one.
    monkeypatch.setenv("GH_TOKEN", "   ")
    assert reg._probe_github_ambient() is False


def test_probe_github_ambient_true_when_git_credential_has_one(gh_config, monkeypatch):
    # Detection path 2 — the case that matters most in practice: `gh auth
    # login` puts the token in the OS keyring, NOT in hosts.yml, and registers
    # `gh auth git-credential` as github.com's helper. Verified on the dev
    # machine: hosts.yml there has users/user/git_protocol and zero
    # `oauth_token:` lines, while `git credential fill` returns a credential
    # in 85-93 ms. Without this path the probe would never fire on a keyring
    # install — i.e. on a default macOS `gh auth login`.
    seen = []

    def _fake(host):
        seen.append(host)
        return True

    monkeypatch.setattr(reg, "_git_credential_present", _fake)
    assert reg._probe_github_ambient() is True
    assert seen == ["github.com"]


def test_probe_github_ambient_true_when_hosts_file_has_a_token(gh_config):
    # Detection path 3 — a gh login with no keyring, or where `gh auth
    # setup-git` never ran so path 2 has no helper to ask.
    gh_config.write_text(f"github.com:\n    user: someone\n    oauth_token: {_SENTINEL_TOKEN}\n")
    assert reg._probe_github_ambient() is True


def test_probe_github_ambient_true_for_a_token_nested_under_users(gh_config):
    # gh writes the token under `users:` as well as at host level; a scan that
    # only understood the top level would miss a real credential.
    gh_config.write_text(
        "github.com:\n"
        "    users:\n"
        f"        someone:\n            oauth_token: {_SENTINEL_TOKEN}\n"
        "    git_protocol: https\n"
    )
    assert reg._probe_github_ambient() is True


def test_probe_github_ambient_ignores_a_token_under_a_different_host(gh_config):
    """Path 3 must be HOST-SCOPED. A `hosts.yml` naming only an enterprise host
    is a credential for THAT host; reporting github.com as ambient off the back
    of it is the false "yes" this probe's docstring calls a lie. (`gh auth
    status` had the same any-host semantics, so this is a tightening, not a
    regression — but path 2 is host-scoped and path 3 must agree with it.)"""
    gh_config.write_text(f"ghe.corp.example:\n    oauth_token: {_SENTINEL_TOKEN}\n")
    assert reg._probe_github_ambient() is False


def test_probe_github_ambient_finds_github_when_another_host_is_listed_first(gh_config):
    """The other direction: host-scoping must not become a way to MISS a real
    github.com token that happens to be listed second."""
    gh_config.write_text(
        f"ghe.corp.example:\n    oauth_token: {_SENTINEL_TOKEN}\n"
        f"github.com:\n    oauth_token: {_SENTINEL_TOKEN}\n"
    )
    assert reg._probe_github_ambient() is True


def test_probe_github_ambient_ignores_a_commented_out_token(gh_config):
    gh_config.write_text(f"github.com:\n    # oauth_token: {_SENTINEL_TOKEN}\n")
    assert reg._probe_github_ambient() is False


def test_probe_github_ambient_false_when_hosts_file_has_only_a_username(gh_config):
    # The sibling's distinction, applied here: a host entry and a username are
    # a PREFERENCE, not proof of a credential. This is the real shape of the
    # dev machine's hosts.yml under a keyring install, so getting it wrong
    # would report "ambient" for every gh user whose token this cannot see.
    gh_config.write_text(
        "github.com:\n    users:\n        someone:\n    git_protocol: https\n    user: someone\n"
    )
    assert reg._probe_github_ambient() is False


@pytest.mark.parametrize("line", ["    oauth_token:", "    oauth_token: ", '    oauth_token: ""',
                                  "    oauth_token: ''"])
def test_probe_github_ambient_false_when_hosts_token_is_empty(gh_config, line):
    gh_config.write_text(f"github.com:\n{line}\n")
    assert reg._probe_github_ambient() is False


def test_probe_github_ambient_false_when_nothing_is_present(gh_config):
    # The negative: no env var, no git credential, no hosts.yml at all.
    assert not gh_config.exists()
    assert reg._probe_github_ambient() is False


def test_probe_github_ambient_false_when_hosts_path_is_unreadable(gh_config):
    # Fails CLOSED. A hosts.yml that is a directory raises IsADirectoryError
    # (an OSError) out of read_text; "cannot tell" must read as "not present".
    gh_config.mkdir()
    assert reg._probe_github_ambient() is False


def test_probe_github_ambient_survives_undecodable_hosts_bytes(gh_config):
    # UnicodeDecodeError is a ValueError, not an OSError — it would sail past
    # the fail-closed guard and crash GET /api/integrations if the read were
    # strict. errors="replace" is what keeps this a bool.
    gh_config.write_bytes(b"github.com:\n    user: \xff\xfe\n")
    assert reg._probe_github_ambient() is False


def test_probe_github_ambient_returns_a_bool_and_never_logs_the_token(gh_config, caplog):
    caplog.set_level(logging.DEBUG)
    gh_config.write_text(f"github.com:\n    oauth_token: {_SENTINEL_TOKEN}\n")
    result = reg._probe_github_ambient()
    assert type(result) is bool and result is True
    assert _SENTINEL_TOKEN not in caplog.text


def test_ambient_token_value_never_reaches_the_integration_status(gh_config):
    """The end-to-end escape check: a present token must change the STATUS and
    nothing else. `repr` of the whole list covers every field at once, so a
    future detail string that helpfully quotes the credential fails here."""
    gh_config.write_text(f"github.com:\n    oauth_token: {_SENTINEL_TOKEN}\n")
    statuses = list_integrations_with_ambient(_UNCONFIGURED_CFG, cache={})
    assert {s.name: s.status for s in statuses}["github"] == "ambient"
    assert _SENTINEL_TOKEN not in repr(statuses)


def test_gh_hosts_path_resolution_matches_gh(monkeypatch, tmp_path):
    """GH_CONFIG_DIR wins; else XDG_CONFIG_HOME/gh; else ~/.config/gh — the
    order gh uses. Getting this wrong is a silent false negative, which is the
    quiet failure mode of a fail-closed probe."""
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "explicit"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert reg._gh_hosts_path() == tmp_path / "explicit" / "hosts.yml"

    monkeypatch.delenv("GH_CONFIG_DIR")
    assert reg._gh_hosts_path() == tmp_path / "xdg" / "gh" / "hosts.yml"

    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setattr(reg.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert reg._gh_hosts_path() == tmp_path / "home" / ".config" / "gh" / "hosts.yml"


def test_probe_github_ambient_never_shells_out_to_a_token_printer(gh_config, monkeypatch):
    """`gh auth token` prints the credential in the clear. Neither it nor any
    other subprocess is reachable from the github probe once the env and
    hosts.yml paths are the only ones left."""
    def _forbidden(*a, **k):
        raise AssertionError(f"github probe spawned a subprocess: {a!r}")

    monkeypatch.setattr(reg.subprocess, "run", _forbidden)
    gh_config.write_text(f"github.com:\n    oauth_token: {_SENTINEL_TOKEN}\n")
    assert reg._probe_github_ambient() is True


def test_git_credential_present_scrubs_the_credential_from_its_frame(monkeypatch):
    """`proc.stdout` holds `password=<TOKEN>`, and a frame local is reachable
    from a traceback — an independent review pulled a sentinel out of exactly
    that. Nothing in `src/no_human/` renders frame locals and there is no
    realistic raise point after the binding, so this was never exploitable;
    it is fixed anyway because the alternative is a containment claim that
    overstates its mechanism."""
    fake = _FakeCompleted(returncode=0, stdout=f"password={_SENTINEL_TOKEN}\n")
    monkeypatch.setattr(reg, "_run_probe", lambda *a, **k: fake)
    assert reg._git_credential_present("github.com") is True
    assert _SENTINEL_TOKEN not in fake.stdout


def test_git_credential_present_scrubs_even_when_no_password_came_back(monkeypatch):
    # The scrub must not be conditional on the answer: a `username=`-only reply
    # still carries whatever the helper chose to print.
    fake = _FakeCompleted(returncode=0, stdout=f"username={_SENTINEL_TOKEN}\n")
    monkeypatch.setattr(reg, "_run_probe", lambda *a, **k: fake)
    assert reg._git_credential_present("github.com") is False
    assert _SENTINEL_TOKEN not in fake.stdout


def test_probe_gitlab_ambient_true_when_password_line_present(monkeypatch):
    monkeypatch.setattr(
        reg.subprocess, "run",
        lambda *a, **k: _FakeCompleted(
            returncode=0, stdout="protocol=https\nhost=gitlab.com\npassword=secret\n",
        ),
    )
    assert reg._probe_gitlab_ambient() is True


def test_probe_gitlab_ambient_false_on_nonzero_returncode(monkeypatch):
    monkeypatch.setattr(
        reg.subprocess, "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stdout="password=secret\n"),
    )
    assert reg._probe_gitlab_ambient() is False


def test_probe_gitlab_ambient_false_when_only_a_username_is_configured(monkeypatch):
    # The ticket's own bug, reversed: a bare username with no stored password
    # is a preference, not proof of an authenticated session.
    monkeypatch.setattr(
        reg.subprocess, "run",
        lambda *a, **k: _FakeCompleted(
            returncode=0, stdout="protocol=https\nhost=gitlab.com\nusername=someone\n",
        ),
    )
    assert reg._probe_gitlab_ambient() is False


def test_probe_gitlab_ambient_false_when_password_line_is_empty(monkeypatch):
    monkeypatch.setattr(
        reg.subprocess, "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout="password=\n"),
    )
    assert reg._probe_gitlab_ambient() is False


def test_probe_gitlab_ambient_false_when_probe_errors(monkeypatch):
    monkeypatch.setattr(reg, "_run_probe", lambda *a, **k: None)
    assert reg._probe_gitlab_ambient() is False


def test_ambient_unknown_provider_is_never_ambient(mock_ambient_probes):
    # jira/circleci/jenkins/slack have no ambient concept.
    st = {s.name: s for s in list_integrations_with_ambient(_UNCONFIGURED_CFG, cache={})}
    for name in ("jira", "linear", "jenkins", "circleci", "slack", "teams"):
        assert st[name].status == "unconfigured"


def test_list_all_unconfigured():
    st = list_integrations({"integrations": {}, "ci": {}, "notifications": {}})
    # Listing order is issue tracker → VCS → CI → notifications.
    assert [s.name for s in st] == ["jira", "linear", "monday", "github", "gitlab",
                                    "jenkins", "circleci", "slack", "teams"]
    assert all(isinstance(s, IntegrationStatus) for s in st)
    assert all(s.configured is False for s in st)
    assert all(s.healthy is None for s in st)          # None until test_integration runs
    kinds = {s.name: s.kind for s in st}
    assert kinds == {
        "jira": "issue_tracker", "linear": "issue_tracker",
        "monday": "issue_tracker",
        "github": "vcs", "gitlab": "vcs",
        "jenkins": "ci", "circleci": "ci",
        "slack": "notifications", "teams": "notifications",
    }


def test_configured_detection():
    cfg = {
        "integrations": {
            "jira": {"site": "acme.atlassian.net", "project_key": "PROJ", "email": "me@x.com"},
            "circleci": {"org_slug": "gh/acme", "project": "svc"},
        },
        "ci": {"enabled": True, "backend": "github_actions", "project": "o/r", "job": ""},
        "notifications": {"slack_webhook_url": "https://hooks.slack.com/x"},
    }
    st = {s.name: s for s in list_integrations(cfg)}
    assert st["jira"].configured is True
    assert st["circleci"].configured is True
    assert st["github"].configured is True      # ci.backend == github_actions + project
    assert st["gitlab"].configured is False     # backend isn't gitlab
    assert st["jenkins"].configured is False    # backend isn't jenkins
    assert st["slack"].configured is True


def test_null_sections_are_safe():
    # Config deep-merge shadowing trap: a user setting `integrations:` (or ci /
    # notifications) to null must not crash the registry.
    st = list_integrations({"integrations": None, "ci": None, "notifications": None})
    assert len(st) == 9
    assert all(s.configured is False for s in st)


def test_detail_never_contains_a_secret():
    cfg = {
        "integrations": {"jira": {"site": "s", "project_key": "P", "email": "e@x.com"}},
        "ci": {}, "notifications": {"slack_webhook_url": "https://hooks.slack.com/T/SECRETPART"},
    }
    for s in list_integrations(cfg):
        assert "SECRETPART" not in s.detail


@pytest.mark.asyncio
async def test_test_integration_jira_health_ok(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "tok-should-not-leak")
    calls = {}

    async def fake_get(url, headers=None, auth=None, timeout=None):
        calls["url"] = url
        calls["auth"] = auth
        class _R:
            status_code = 200
            def json(self):
                return {"displayName": "Dana Lee"}
        return _R()

    monkeypatch.setattr(reg, "_http_get", fake_get)
    cfg = {"integrations": {"jira": {"site": "https://acme.atlassian.net",
                                     "project_key": "P", "email": "me@x.com"}}}
    s = await reg.test_integration("jira", cfg)
    assert s.name == "jira"
    assert s.healthy is True
    assert "acme.atlassian.net" in calls["url"]
    assert calls["auth"] == ("me@x.com", "tok-should-not-leak")   # Basic auth
    assert "tok-should-not-leak" not in s.detail                  # never echoed


@pytest.mark.asyncio
async def test_test_integration_jira_health_fails_loudly(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    async def fake_get(url, headers=None, auth=None, timeout=None):
        class _R:
            status_code = 401
            def json(self):
                return {}
        return _R()

    monkeypatch.setattr(reg, "_http_get", fake_get)
    cfg = {"integrations": {"jira": {"site": "https://acme.atlassian.net",
                                     "project_key": "P", "email": "me@x.com"}}}
    s = await reg.test_integration("jira", cfg)
    assert s.healthy is False
    assert "401" in s.detail


@pytest.mark.asyncio
async def test_test_integration_unconfigured_jira_is_not_healthy(monkeypatch):
    s = await reg.test_integration("jira", {"integrations": {"jira": {}}})
    assert s.configured is False
    assert s.healthy is False
    assert "not configured" in s.detail.lower()


@pytest.mark.asyncio
async def test_test_integration_unknown_name_raises():
    with pytest.raises(ValueError, match="unknown integration"):
        await reg.test_integration("mystery", {})


# --------------------------------------------------------------------------- #
# Linear (issue tracker) + Teams (notifications)                               #
# --------------------------------------------------------------------------- #

def test_linear_and_teams_are_registered_with_the_right_kinds():
    names = [s.name for s in list_integrations({})]
    assert "linear" in names and "teams" in names
    kinds = {s.name: s.kind for s in list_integrations({})}
    assert kinds["linear"] == "issue_tracker"     # intake, like jira
    assert kinds["teams"] == "notifications"      # notify-OUT, NOT the context source
    assert reg.KIND_BY_NAME["linear"] == "issue_tracker"
    assert reg.KIND_BY_NAME["teams"] == "notifications"


def test_linear_status_needs_a_team_key():
    st = {s.name: s for s in list_integrations(
        {"integrations": {"linear": {"team_key": "ENG", "label": "Bug"}}})}
    assert st["linear"].configured is True
    assert "ENG" in st["linear"].detail and "Bug" in st["linear"].detail
    st = {s.name: s for s in list_integrations({"integrations": {"linear": {"label": "Bug"}}})}
    assert st["linear"].configured is False


def test_linear_secret_lives_in_env_not_config():
    specs = {f.name: f for f in reg.FIELD_SPECS["linear"]}
    assert specs["api_key"].secret is True
    assert specs["api_key"].env_var == "LINEAR_API_KEY"
    assert specs["api_key"].config_path is None      # never written to config.yaml
    assert specs["team_key"].config_path == "integrations.linear.team_key"


def test_teams_webhook_is_marked_secret_and_never_echoed():
    url = "https://prod-9.westus.logic.azure.com:443/workflows/x?sig=SECRETSIG"
    specs = {f.name: f for f in reg.FIELD_SPECS["teams"]}
    assert specs["webhook_url"].secret is True
    assert specs["webhook_url"].config_path == "notifications.teams_webhook_url"
    for s in list_integrations({"notifications": {"teams_webhook_url": url}}):
        assert "SECRETSIG" not in s.detail


def test_a_retired_teams_connector_url_is_reported_as_broken_not_configured():
    # Microsoft disabled Office 365 connectors in May 2026. Reporting such a
    # URL as a working channel would hide the breakage until an alert failed.
    st = {s.name: s for s in list_integrations({"notifications": {
        "teams_webhook_url": "https://outlook.office.com/webhook/a/IncomingWebhook/b/c"}})}
    assert st["teams"].healthy is False
    assert "retired" in st["teams"].detail.lower()
    assert "Power Automate" in st["teams"].detail


@pytest.mark.asyncio
async def test_test_integration_linear_health_uses_the_raw_key_header(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin-should-not-leak")
    calls = {}

    async def fake_post(url, headers=None, json=None, timeout=None):
        calls.update(url=url, headers=headers, json=json)

        class _R:
            status_code = 200

            def json(self):
                return {"data": {"viewer": {"id": "u1", "name": "Dana Lee"}}}
        return _R()

    monkeypatch.setattr(reg, "_http_post", fake_post)
    s = await reg.test_integration("linear", {"integrations": {"linear": {"team_key": "ENG"}}})
    assert s.healthy is True and "Dana Lee" in s.detail
    assert calls["url"] == "https://api.linear.app/graphql"
    # RAW key, not Bearer — Linear's documented personal-key header.
    assert calls["headers"]["Authorization"] == "lin-should-not-leak"
    assert "lin-should-not-leak" not in s.detail


@pytest.mark.asyncio
async def test_test_integration_linear_reports_a_200_with_errors_as_unhealthy(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")

    async def fake_post(url, headers=None, json=None, timeout=None):
        class _R:
            status_code = 200

            def json(self):
                return {"errors": [{"message": "nope",
                                    "extensions": {"code": "AUTHENTICATION_ERROR"}}]}
        return _R()

    monkeypatch.setattr(reg, "_http_post", fake_post)
    s = await reg.test_integration("linear", {"integrations": {"linear": {"team_key": "ENG"}}})
    assert s.healthy is False
    assert "AUTHENTICATION_ERROR" in s.detail


@pytest.mark.asyncio
async def test_test_integration_linear_names_throttling_at_http_400(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")

    async def fake_post(url, headers=None, json=None, timeout=None):
        class _R:
            status_code = 400

            def json(self):
                return {"errors": [{"message": "slow down",
                                    "extensions": {"code": "RATELIMITED"}}]}
        return _R()

    monkeypatch.setattr(reg, "_http_post", fake_post)
    s = await reg.test_integration("linear", {"integrations": {"linear": {"team_key": "ENG"}}})
    assert s.healthy is False
    assert "rate limited" in s.detail.lower()


@pytest.mark.asyncio
async def test_test_integration_linear_without_a_key_says_which_key(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    s = await reg.test_integration("linear", {"integrations": {"linear": {"team_key": "ENG"}}})
    assert s.healthy is False
    assert "LINEAR_API_KEY" in s.detail


@pytest.mark.asyncio
async def test_test_integration_teams_never_posts_a_probe_message(monkeypatch):
    # Microsoft's terms: "It's a violation of the terms of use to use Microsoft
    # Teams as a log file. Only send messages that people will read." A health
    # check must not put noise in a human's channel.
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not post"))
    monkeypatch.setattr(reg, "_http_post",
                        lambda *a, **k: pytest.fail("must not post"))
    s = await reg.test_integration("teams", {"notifications": {
        "teams_webhook_url": "https://prod-9.westus.logic.azure.com:443/workflows/x?sig=S"}})
    assert s.healthy is True
    assert "run time" in s.detail


@pytest.mark.asyncio
async def test_test_integration_teams_reports_a_retired_connector_url(monkeypatch):
    s = await reg.test_integration("teams", {"notifications": {
        "teams_webhook_url": "https://acme.webhook.office.com/webhookb2/a/IncomingWebhook/b/c"}})
    assert s.healthy is False
    assert "retired" in s.detail.lower()


@pytest.mark.asyncio
async def test_test_integration_teams_unconfigured():
    s = await reg.test_integration("teams", {"notifications": {}})
    assert s.configured is False and s.healthy is False
