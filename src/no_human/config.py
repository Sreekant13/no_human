"""Configuration loading and the subscription-auth safety boundary.

The single most important job in this module is preventing the daemon from
silently billing the metered Anthropic API. The Claude Agent SDK honours
``ANTHROPIC_API_KEY`` over ``CLAUDE_CODE_OAUTH_TOKEN`` when both are present, so
a stray key would quietly bill pay-per-token instead of the subscription. On
startup we scrub every metered-auth variable from the process environment and
assert that subscription mode is active before any task can run.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import os
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Home for the user's private token + config. Never inside the repo.
log = logging.getLogger("no_human.config")

NO_HUMAN_HOME = Path.home() / ".no_human"
ENV_PATH = NO_HUMAN_HOME / ".env"
CONFIG_PATH = NO_HUMAN_HOME / "config.yaml"
DB_PATH = NO_HUMAN_HOME / "no_human.db"

# The subscription token the SDK / `claude` CLI reads.
SUBSCRIPTION_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# Auth profiles let one install hold several subscriptions' tokens side by side
# in the same chmod-600 .env: the unsuffixed SUBSCRIPTION_TOKEN_VAR is the
# "default" profile, and any other profile <p> lives in
# ``CLAUDE_CODE_OAUTH_TOKEN_<P>``. Exactly one of them is exported into
# SUBSCRIPTION_TOKEN_VAR at startup, so a task can never span two subscriptions.
DEFAULT_AUTH_PROFILE = "default"

# The profile whose token this process exported, set by :func:`load_env_token`.
# Read it through :func:`active_auth_profile` — never re-derive it from config,
# which a long-lived server may have outlived.
_ACTIVE_AUTH_PROFILE: str | None = None

# Variables that, if present, route to metered API / cloud billing instead of
# the subscription. ANTHROPIC_API_KEY is the dangerous one (wins precedence).
# The one metered var that is a SANCTIONED billing path in BYO-API-key mode
# (llm.auth_mode: "api_key"). It stays in METERED_AUTH_VARS so subscription mode
# still scrubs it; api_key mode passes it to scrub's ``keep`` and requires it.
API_KEY_VAR = "ANTHROPIC_API_KEY"

METERED_AUTH_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLOUD_ML_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AWS_BEARER_TOKEN_BEDROCK",
)


# --------------------------------------------------------------------------- #
# The SECOND coding backend's credential (OpenAI Codex).                       #
# --------------------------------------------------------------------------- #
#
# BYO-API-KEY IS THE ONLY SANCTIONED PATH, and that is a legal constraint, not a
# convenience. OpenAI's terms prohibit using ChatGPT to power third-party
# services, so no_human never routes a user's ChatGPT subscription: there is no
# browser-login flow here, no `codex login`, and the Codex CLI is invoked with
# `preferred_auth_method="apikey"` so it cannot silently fall back to a ChatGPT
# credential that happens to exist on the machine.
#
# The same discipline as the Anthropic key applies verbatim: the MODE
# (`worker.backend: codex`) may live in config.yaml, the KEY never does — it
# comes from ~/.no_human/.env, chmod 600 (see `_reject_api_key_in_config`,
# which names both vendors' keys).
CODEX_API_KEY_VAR = "OPENAI_API_KEY"

# Variables that would silently REROUTE an OpenAI call to somebody else's
# endpoint or somebody else's bill. Deliberately a SEPARATE tuple from
# METERED_AUTH_VARS rather than an extension of it: that tuple is the Anthropic
# scrub list, is asserted verbatim by a test, and is applied on EVERY run —
# including runs that use no OpenAI at all. Scrubbing these is only correct when
# Codex is the selected backend, which is exactly when `assert_codex_api_key_mode`
# runs.
#
# NOT included, deliberately: OPENAI_ORG_ID / OPENAI_PROJECT. They select which
# of the key-holder's OWN org/projects is billed, which is a legitimate choice an
# operator may have made in their shell; removing them would silently move their
# invoice. The ones below point the request somewhere else entirely.
CODEX_ALTERNATE_ROUTING_VARS = (
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_AD_TOKEN",
)


# Windows cannot express POSIX permission bits: `os.chmod` there only toggles
# FILE_ATTRIBUTE_READONLY, and the mode argument to `os.open` is ignored except
# for that same bit. So every `0o600` in this module is a SILENT NO-OP on
# Windows and the credential file inherits whatever ACL its directory carries.
# Read it through this constant rather than testing `os.name` inline, so the
# Windows branches are reachable (and therefore testable) from any platform.
_IS_WINDOWS = os.name == "nt"

_ICACLS_OK_TAIL = ("Successfully processed", "Failed processing")

# The Windows Trusted Computing Base: SYSTEM and the local Administrators group.
# These are the platform's root-equivalent — the exact analog of POSIX ``root``,
# which the ``os.chmod(path, 0o600)`` branch of this module leaves with full
# access and cannot exclude. Any local administrator can already read this file,
# take ownership of it, or run as SYSTEM to reach it regardless of its ACL, so
# treating them as forbidden is BOTH impossible on an admin-owned file — the
# common case, since the primary account on most personal Windows installs is a
# local admin, and files it creates carry EXPLICIT Administrators/SYSTEM ACEs
# that ``/inheritance:r`` does not strip — AND stricter than the POSIX contract
# this module mirrors. Accepting them realigns Windows with that contract; the
# readback STILL flags every OTHER principal (Users, Everyone, a specific
# non-owner account), which is the real protection for a non-admin user.
#
# Matched by the localized names an en-US readback emits AND by the well-known
# SIDs, since icacls prints a raw SID when it cannot resolve a name. A
# non-English Windows emits localized display names not listed here; those fall
# through to the throw, which names the surviving principal, so an incomplete
# allowlist is self-reporting on the next run rather than a silent weakening.
_WINDOWS_TCB_NAMES = frozenset({"nt authority\\system", "builtin\\administrators"})
_WINDOWS_TCB_SIDS = frozenset({"s-1-5-18", "s-1-5-32-544"})


def _is_windows_tcb_principal(grantee: str) -> bool:
    """True if *grantee* is SYSTEM or the local Administrators group."""
    g = (grantee or "").lstrip("*").strip().lower()
    return g in _WINDOWS_TCB_NAMES or g in _WINDOWS_TCB_SIDS


def _non_owner_grantees(grantees: set[str], principal: str) -> set[str]:
    """Grantees that are neither the owner nor the Windows TCB — i.e. some OTHER
    account can reach the credential. Case-insensitive: Windows account names
    are, and a case difference would otherwise fail closed on a secured file.
    """
    owner = principal.casefold()
    return {
        g for g in grantees
        if g.casefold() != owner and not _is_windows_tcb_principal(g)
    }


class AuthError(RuntimeError):
    """Raised when the process is not provably in subscription-billing mode."""


class CredentialPermissionError(AuthError):
    """Raised when a credential file cannot be restricted to its owner.

    This is a FAIL-CLOSED signal, not a warning. The alternative — writing an
    OAuth token or an ``ANTHROPIC_API_KEY`` into a file whose permissions we
    could not verify — is the failure this class exists to make impossible.
    """


def _windows_owner_principal() -> str:
    """The account to grant the credential file to, as ``DOMAIN\\USER``.

    Derived from the process environment rather than from a Win32 call so no
    dependency is added. ``USERNAME`` is set by every interactive and service
    logon; if it is missing we cannot name a grantee and must fail closed.
    """
    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        raise CredentialPermissionError(
            "cannot secure the credential file: USERNAME is not set, so there "
            "is no account to restrict it to. Set USERNAME, or move "
            "NO_HUMAN_HOME to a directory only you can read."
        )
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    return f"{domain}\\{user}" if domain else user


def _icacls_grantees(path: Path, output: str) -> set[str]:
    """Parse ``icacls <path>`` output into the set of granted principals.

    ``icacls`` prints ``<path> <PRINCIPAL>:(perms)`` on the first line and
    ``<PRINCIPAL>:(perms)`` (indented) on each subsequent one, then a summary.
    Parsing is deliberately permissive about WHAT the permissions are: any
    principal appearing at all is access we did not intend to grant.
    """
    grantees: set[str] = set()
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith(_ICACLS_OK_TAIL):
            continue
        # Strip the path prefix icacls repeats on its first line.
        if line.startswith(str(path)):
            line = line[len(str(path)):].strip()
        if ":(" not in line:
            continue
        grantees.add(line.split(":(", 1)[0].strip())
    return grantees


def _run_icacls(args: list[str]) -> tuple[int, str]:
    """Run ``icacls`` with *args*; return ``(returncode, stdout+stderr)``.

    Split out so tests can drive both the Windows success and failure paths
    from a POSIX host, where ``icacls`` does not exist.
    """
    import shutil as _shutil
    import subprocess as _subprocess

    exe = _shutil.which("icacls")
    if exe is None:
        raise CredentialPermissionError(
            "cannot secure the credential file: `icacls` was not found on "
            "PATH, so its permissions cannot be restricted to your account. "
            "Refusing to write a credential that any account on this machine "
            "could read."
        )
    proc = _subprocess.run(
        [exe, *args], capture_output=True, text=True, timeout=30,
    )
    return proc.returncode, f"{proc.stdout}\n{proc.stderr}"


def windows_restrict_to_owner(path: Path, *, directory: bool = False) -> None:
    """Replace *path*'s ACL with an owner-only one, then VERIFY the result.

    Two steps, and the second is the one that matters: a ``chmod`` that returns
    successfully having done nothing is exactly the defect this replaces, so
    the ACL is read back and every principal on it is checked. Raises
    :class:`CredentialPermissionError` if the file is still reachable by any
    account other than its owner.

    UNTESTED ON WINDOWS — no Windows host was available. The command shapes and
    the readback parser are covered by tests that drive them from POSIX.
    """
    principal = _windows_owner_principal()
    # (OI)(CI) makes a directory's ACE inheritable by its future contents; on a
    # file those flags are meaningless and icacls rejects them.
    rights = "(OI)(CI)(F)" if directory else "(R,W)"
    code, out = _run_icacls([
        str(path), "/inheritance:r", "/grant:r", f"{principal}:{rights}",
    ])
    if code != 0:
        raise CredentialPermissionError(
            f"cannot secure {path}: icacls exited {code}. {out.strip()}"
        )
    windows_assert_owner_only(path)


def windows_assert_owner_only(path: Path) -> None:
    """Raise unless *path*'s ACL grants access to its owner and nobody else."""
    principal = _windows_owner_principal()
    code, out = _run_icacls([str(path)])
    if code != 0:
        raise CredentialPermissionError(
            f"cannot verify permissions on {path}: icacls exited {code}. "
            f"{out.strip()}"
        )
    grantees = _icacls_grantees(path, out)
    if not grantees:
        raise CredentialPermissionError(
            f"cannot verify permissions on {path}: icacls listed no grantees, "
            f"so the restriction cannot be confirmed to have taken effect."
        )
    # SYSTEM and the local Administrators group are the platform TCB and are
    # accepted (see _non_owner_grantees / _WINDOWS_TCB_*); any OTHER non-owner
    # grantee is the fail-closed signal.
    extra = _non_owner_grantees(grantees, principal)
    if extra:
        raise CredentialPermissionError(
            f"refusing to write a credential to {path}: it is still readable "
            f"by {', '.join(sorted(extra))}. Move NO_HUMAN_HOME to a location "
            f"only your account can reach, or fix the ACL with: "
            f'icacls "{path}" /inheritance:r /grant:r "{principal}:(R,W)"'
        )


@dataclass
class ScrubReport:
    """What the startup scrub found and removed."""

    removed: list[str] = field(default_factory=list)
    api_key_present: bool = False


def scrub_metered_auth(
    env: dict[str, str] | os._Environ | None = None,
    *,
    keep: tuple[str, ...] = (),
) -> ScrubReport:
    """Remove every metered-auth variable from ``env`` (process env by default).

    Returns a report listing what was removed and whether the dangerous
    ``ANTHROPIC_API_KEY`` was among them. Callers decide whether its presence is
    fatal (see :func:`assert_subscription_mode`). Scrubbing is unconditional so
    that even a caller that swallows the error cannot fall through to metered
    billing.

    ``keep`` names variables to leave in place. Its only sanctioned use is
    BYO-API-key mode, where ``ANTHROPIC_API_KEY`` is the CHOSEN billing path and
    every OTHER redirect (auth token, Bedrock, Vertex) is still scrubbed so the
    run bills exactly one path. Empty by default — subscription mode scrubs all.
    """
    target = os.environ if env is None else env
    report = ScrubReport()
    for var in METERED_AUTH_VARS:
        if var in keep:
            continue
        if var in target and target[var]:
            report.removed.append(var)
            if var == "ANTHROPIC_API_KEY":
                report.api_key_present = True
            del target[var]
    return report


def _read_env_file(env_path: Path | None = None) -> dict[str, str]:
    """Parse ``~/.no_human/.env`` into ``{key: value}``, dropping blanks.

    Comments, blank lines, and keys with an empty value are skipped, and
    surrounding quotes are stripped. The returned values are secrets: callers
    must never log or return them (constraint §8).

    ``env_path`` resolves at CALL time. A ``= ENV_PATH`` default binds at
    import, so a test redirecting ``config.ENV_PATH`` would still read (and,
    for the writer, WRITE) the operator's real credential file.
    """
    env_path = ENV_PATH if env_path is None else env_path
    entries: dict[str, str] = {}
    if not env_path.exists():
        return entries
    # split("\n"), NOT splitlines(): the latter also breaks on \x0b \x0c
    # \x1c \x1d \x1e \x85 U+2028 U+2029, so a value carrying any of them
    # would be parsed as EXTRA VARIABLES that no writer ever wrote.
    # Explicit UTF-8 both here and in `atomic_write_0600`: Python's default
    # text encoding is the locale's, which is cp1252 on most Windows installs,
    # so a round trip through the default would corrupt (or raise on) any
    # non-ASCII value this file has always been able to hold.
    for raw in env_path.read_text(encoding="utf-8").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            entries[key.strip()] = value
    return entries


# `\Z`, not `$`: `$` also matches just BEFORE a trailing newline, so the
# charset above would have exempted one — "personal2\n" read as valid.
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\Z")
# A profile name is a label, not free text. The regex alone accepts
# `sk-ant-oat01-…` — a credential is lowercase letters, digits and hyphens — so
# a token pasted into the profile box was stored as a NAME and then echoed back
# by the status endpoint, which advertises "names and booleans only". Two
# adjacent text boxes in a settings form is the likeliest operator error, and
# the active profile name is stamped on every run, so it also reaches the DB and
# logs. A length cap plus a credential-shape check makes that unreachable.
MAX_PROFILE_NAME_LEN = 32
# Unauthenticated + unbounded is a bad combination: a 2MB token was accepted
# and written. Real OAuth tokens are a few hundred bytes.
MAX_TOKEN_LEN = 4096
_CREDENTIAL_SHAPED = ("sk-ant-", "sk_ant_")


def validate_profile_name(profile: str) -> str:
    """Normalise and validate an auth profile name; return the normalised form.

    NEVER echoes the input. The credential-shape and length branches were
    written not to, but the regex branch echoed `{profile!r}` — and that is
    precisely the branch an enterprise token falls into, since enterprise
    tokens are deliberately not whitelisted by shape. Putting a pasted
    credential in the error message just moves the disclosure into the 422.
    """
    profile = profile.strip().lower()
    if profile.startswith(_CREDENTIAL_SHAPED):
        raise AuthError(
            "that looks like a token, not a profile name — did you paste it "
            "into the wrong field? A profile is a short label like 'personal'.")
    if len(profile) > MAX_PROFILE_NAME_LEN:
        raise AuthError(
            f"auth profile name is too long (max {MAX_PROFILE_NAME_LEN} "
            f"chars) — a profile is a short label.")
    if not _PROFILE_NAME_RE.match(profile):
        raise AuthError(
            "invalid auth profile name — use lowercase letters, digits, "
            "'-' and '_' only.")
    return profile


def profile_token_var(profile: str) -> str:
    """The .env variable holding *profile*'s token. Names only, never values.

    The name is validated with the same rule ``set_auth_profile`` applies. This
    function builds an .env KEY, and a key carrying a newline would let a
    caller inject an arbitrary extra line into ``~/.no_human/.env`` — a forged
    ``ANTHROPIC_API_KEY=`` among them, which is exactly the metered-billing
    escape constraint #1 exists to prevent. Harmless while the only caller was
    the operator's own shell; not harmless once a profile name can arrive over
    HTTP.
    """
    profile = validate_profile_name(profile)
    if profile == DEFAULT_AUTH_PROFILE:
        return SUBSCRIPTION_TOKEN_VAR
    return f"{SUBSCRIPTION_TOKEN_VAR}_{profile.upper()}"


def available_auth_profiles(env_path: Path | None = None) -> list[str]:
    """Profile names that have a token, in ``~/.no_human/.env`` or the process
    environment. Returns names only — a token value is never returned or logged.
    """
    env_path = ENV_PATH if env_path is None else env_path
    prefix = SUBSCRIPTION_TOKEN_VAR + "_"
    found: set[str] = set()
    for source in (_read_env_file(env_path), os.environ):
        for key, value in source.items():
            if not value:
                continue
            if key == SUBSCRIPTION_TOKEN_VAR:
                found.add(DEFAULT_AUTH_PROFILE)
            elif key.startswith(prefix):
                found.add(key[len(prefix):].lower())
    return sorted(found)


def active_auth_profile() -> str | None:
    """The profile whose token this process actually exported, or None.

    This reports what :func:`load_env_token` did, not what config.yaml currently
    says. A long-lived server exports its token once at startup; if the operator
    then runs ``nh auth use other``, config on disk changes but the running
    process is still billing the old subscription. Attributing a burn to the
    config value would be a lie, so every stamp reads this instead.
    """
    return _ACTIVE_AUTH_PROFILE


def load_env_token(
    env_path: Path | None = None, *, profile: str | None = None
) -> str | None:
    """Resolve *profile*'s token and export it as ``CLAUDE_CODE_OAUTH_TOKEN``.

    The .env is the source of truth (chmod 600, gitignored, never in the repo).
    A token already in the process environment is used as a fallback and is not
    overwritten. Exactly one token is exported — the SDK reads only the
    unsuffixed variable — so a run can never span two subscriptions.

    Returns the active token, or None if the *default* profile has none. A named
    profile with no token raises :class:`AuthError` rather than falling back to
    the default: a silent fallback would bill the wrong subscription.
    """
    global _ACTIVE_AUTH_PROFILE
    profile = (profile or DEFAULT_AUTH_PROFILE).strip().lower()
    var = profile_token_var(profile)
    # .env wins over an inherited token: it is the curated source.
    env_path = ENV_PATH if env_path is None else env_path
    token = _read_env_file(env_path).get(var) or os.environ.get(var)

    if not token:
        if profile != DEFAULT_AUTH_PROFILE:
            available = ", ".join(available_auth_profiles(env_path)) or "none"
            raise AuthError(
                f"auth profile '{profile}' has no token. Expected {var} in "
                f"{env_path} (chmod 600) or the process environment.\n"
                f"Profiles with a token: {available}\n"
                f"Switch with:  nh auth use <profile>"
            )
        return None

    os.environ[SUBSCRIPTION_TOKEN_VAR] = token
    _ACTIVE_AUTH_PROFILE = profile
    return token


def load_api_key(env_path: Path | None = None) -> str | None:
    """BYO-API-key mode only: resolve ``ANTHROPIC_API_KEY`` from
    ``~/.no_human/.env`` (source of truth) or the process env, and export it.

    Mirrors :func:`load_env_token`'s discipline — .env wins, an inherited value
    is a non-overwritten fallback — but for the metered key the operator has
    explicitly chosen to bill. Returns the key or None; NEVER echoes it. Only
    :func:`assert_subscription_mode` in ``api_key`` mode may call this; every
    other path treats ``ANTHROPIC_API_KEY`` as forbidden.
    """
    env_path = ENV_PATH if env_path is None else env_path
    key = _read_env_file(env_path).get(API_KEY_VAR) or os.environ.get(API_KEY_VAR)
    if key:
        os.environ[API_KEY_VAR] = key
    return key or None


def assert_codex_api_key_mode(env_path: Path | None = None) -> ScrubReport:
    """Enforce BYO-API-key billing for the Codex coding backend.

    Called ONLY when ``worker.backend`` is ``"codex"``, and IN ADDITION to
    :func:`assert_subscription_mode` — not instead of it. That is deliberate and
    is the one place the "a run bills exactly one path" rule needed restating
    for a two-vendor world: with Codex selected, the CODER bills OpenAI and the
    reviewer, planner, supervisor and utility tiers still bill Anthropic,
    because the review gate and the four model tiers are pinned to Claude by
    constraint. So the invariant is
    per-vendor: exactly one Anthropic credential and exactly one OpenAI
    credential, each the one the operator chose, with every alternate routing
    for both scrubbed. Two vendors, two bills, no third path.

    Raises :class:`AuthError` when no ``OPENAI_API_KEY`` resolves. Never echoes
    the key.
    """
    env_path = ENV_PATH if env_path is None else env_path
    key = _read_env_file(env_path).get(CODEX_API_KEY_VAR) or os.environ.get(
        CODEX_API_KEY_VAR)
    report = ScrubReport()
    for var in CODEX_ALTERNATE_ROUTING_VARS:
        if os.environ.get(var):
            report.removed.append(var)
            del os.environ[var]
    if not key:
        raise AuthError(
            "worker.backend is 'codex' but no OPENAI_API_KEY was found. The "
            "Codex backend runs on YOUR OWN OpenAI API key — there is no "
            "subscription path, because OpenAI's terms prohibit using ChatGPT "
            "to power third-party services.\n"
            f"Add the key to {env_path} (chmod 600):\n"
            "  echo 'OPENAI_API_KEY=sk-...' >> ~/.no_human/.env\n"
            "It must never go in config.yaml. To go back to Claude, set "
            "worker.backend: claude."
        )
    os.environ[CODEX_API_KEY_VAR] = key
    return report


def load_env_var(name: str, env_path: Path | None = None) -> str | None:
    """Load a single secret (e.g. ``JENKINS_API_TOKEN``) from ``~/.no_human/.env``
    into the process env, following the same discipline as the OAuth token: the
    .env (chmod 600, gitignored, never in the repo) is the source of truth, an
    inherited value is a non-overwritten fallback. Returns the active value or
    None. Used for CI/VCS credentials — these must never live in config.yaml or
    the repo, only in the private .env or the process environment.
    """
    if name in METERED_AUTH_VARS:
        # Defensive: a metered-auth var must never be loaded as a generic secret.
        raise AuthError(f"{name} is a metered-auth variable and must never be loaded.")
    env_path = ENV_PATH if env_path is None else env_path
    value = _read_env_file(env_path).get(name)
    if value:
        os.environ[name] = value
    return os.environ.get(name) or None


def credential_status(
    keys: list[str], env_path: Path | None = None
) -> dict[str, bool]:
    """Report which of ``keys`` currently resolve to a value — from the process
    env or ``~/.no_human/.env``. Returns ``{key: present}`` and NEVER returns or
    logs the value itself (constraint §8: secrets are never echoed). Used by
    ``nh onboard`` to tell the human exactly which .env keys are still missing.
    """
    env_path = ENV_PATH if env_path is None else env_path
    present_in_env = _read_env_file(env_path)
    return {
        key: bool(os.environ.get(key)) or key in present_in_env
        for key in keys
    }


def assert_single_env_line(text: str, what: str = "value") -> None:
    """Reject anything that would not survive a round-trip as ONE .env line.

    Checking only ``\n``/``\r`` was not enough: ``str.splitlines()`` — which
    the reader used — also breaks on ``\x0b \x0c \x1c \x1d \x1e \x85``,
    U+2028 and U+2029, so eight characters slipped past a guard that claimed to
    stop line injection. A NUL is rejected for a different reason: it round-trips
    fine but ``os.environ[...] = value`` then raises ``ValueError: embedded null
    byte`` on EVERY subsequent start, which bricks the daemon persistently.

    The value is never echoed back in the error (constraint §8).
    """
    if "\x00" in text:
        raise AuthError(f"{what} must not contain a null byte")
    # `splitlines()` DROPS a trailing separator, so a length check alone
    # accepts "tok\u2028": it survived the write and the reader then returned
    # a silently TRUNCATED token — no injection, but a credential that fails
    # for no visible reason. Comparing against the round trip catches leading,
    # interior AND trailing breaks in one rule, for all ten separators.
    parts = text.splitlines()
    if len(parts) > 1 or (parts and parts[0] != text) or (not parts and text):
        raise AuthError(f"{what} must be a single line")


def secure_credential_file(path: Path) -> None:
    """Restrict an ALREADY-WRITTEN credential file to its owner, or raise.

    For writers that cannot use :func:`atomic_write_0600` because something
    else produced the file (Playwright's ``storage_state``, for one). POSIX:
    ``chmod 0600``. Windows: owner-only ACL plus readback, because ``chmod``
    there is a silent no-op. Raises on Windows when the restriction cannot be
    proven — the caller must then DELETE the file it could not secure.
    """
    if _IS_WINDOWS:
        windows_restrict_to_owner(Path(path))
    else:
        os.chmod(path, 0o600)


def ensure_private_dir(path: Path) -> Path:
    """Create *path* and make it private (0700), even if it ALREADY EXISTS.

    `mkdir(mode=0o700)` is NOT sufficient and was a silent no-op here: Python
    applies `mode` only when it CREATES the directory, and several other call
    sites (the DB, config.yaml, the repo-map cache) create ~/.no_human at the
    process umask first — so by the time a credential is written the directory
    is already 0755 and stays that way. The .env's own 0600 does not protect
    the config.yaml, no_human.db and cache/ sitting beside it.

    chmod only when the bits are actually wrong, so this never churns a
    directory the operator has already locked down further.
    """
    # `mode=` reaches only the LEAF: CPython's makedirs recurses without
    # forwarding it, so `makedirs(a/b/c, mode=0o700)` gives a=0755, b=0755,
    # c=0700 (measured). That still closes the window where it matters most —
    # the leaf is where the file is about to be written — while intermediate
    # levels are born at the umask and repaired a moment later by the walk
    # below, before this function returns and anything is written.
    #
    # An earlier version of this comment claimed `mode=` applied to every level
    # this call creates. It does not, and nothing observes the difference, so
    # dropping `mode=` was the one mutation the suite did not catch.
    os.makedirs(path, mode=0o700, exist_ok=True)
    if _IS_WINDOWS:
        # `mode=` and the chmod walk below are no-ops on Windows. Apply the
        # ACL equivalent instead. Unlike the credential FILE this is NOT fatal:
        # the directory holds config.yaml, the DB and the cache, none of them
        # credentials, and `nh init` refusing to run over a directory ACL it
        # cannot rewrite would be worse than the exposure. The .env inside is
        # secured (and fails closed) independently.
        try:
            windows_restrict_to_owner(path, directory=True)
        except (CredentialPermissionError, OSError) as exc:
            log.warning("could not secure %s (%s); the credential file inside "
                        "is still restricted to your account independently",
                        path, exc)
        return path
    # Secure the ANCESTORS too, up to and including ~/.no_human. `parents=True`
    # creates every missing level at the process umask, so
    # `ensure_private_dir(~/.no_human/cache)` on a fresh machine left
    # ~/.no_human itself at 0755 while the leaf was private — and the
    # credential store, config.yaml and the DB all live at THAT level. Bounded
    # to our own subtree: nothing above NO_HUMAN_HOME is ever touched.
    targets = [path]
    try:
        rel = path.resolve().relative_to(NO_HUMAN_HOME.resolve())
        targets = [NO_HUMAN_HOME.joinpath(*rel.parts[:i])
                   for i in range(len(rel.parts) + 1)]
    except (ValueError, OSError):
        pass  # not under ~/.no_human (a tmp dir, a custom path): leaf only
    for target in targets:
        try:
            # NEVER chmod through a symlink. `Path.chmod` follows links, so a
            # symlink planted inside ~/.no_human pointing at an outside
            # directory would have that target tightened to 0700 (measured:
            # 0755 -> 0700). We only secure the REAL directories we create in
            # our own subtree; a symlink the operator placed (e.g. cache on
            # fast storage) is theirs, and the .env inside is 0600 regardless.
            if target.is_symlink():
                # Skipping a planted leaf symlink is correct and silent. But if
                # NO_HUMAN_HOME ITSELF is a symlink (the operator relocated the
                # store to another disk), skipping it leaves the store dir — and
                # the config.yaml / no_human.db beside the 0600 .env — at the
                # process umask. That is a real downgrade, so make it visible
                # rather than silent (review of #221).
                if target == NO_HUMAN_HOME:
                    log.warning(
                        "%s is a symlink; leaving its target's mode as-is. "
                        "The .env is still 0600, but secure the store dir "
                        "yourself (chmod 700) if it is on a shared host.",
                        NO_HUMAN_HOME)
                continue
            mode = target.stat().st_mode & 0o7777
            if mode & 0o077:
                # CLEAR group/other, preserve everything else. `chmod(0o700)`
                # was not the no-churn rule this function documents: it
                # restored owner-write on a 0550 directory and silently dropped
                # setgid on 02750. The security goal is "no group or other
                # access"; every other bit is the operator's business.
                target.chmod(mode & ~0o077)
        except OSError as exc:  # not fatal — file modes still apply
            # But do NOT fail silently: the caller is about to write a
            # credential into a directory we could not secure.
            log.warning("could not secure %s (%s); its contents rely on their "
                        "own file modes", target, exc)
    return path


def atomic_write_0600(path: Path, content: str) -> None:
    """Atomically write *content* to *path*, mode 0600 from the first byte.

    Writes to a sibling temp file created with ``O_CREAT`` at 0600 (so there is
    never a window where it exists at the process umask), then ``os.replace``s
    it onto *path* — atomic on POSIX and immune to a world/group-readable
    window even on first creation.

    On WINDOWS the 0600 above is a silent no-op (see ``_IS_WINDOWS``), so the
    temp file's ACL is replaced with an owner-only one and READ BACK to confirm
    it took — both while the file is still EMPTY, so a credential byte is never
    written to a file whose permissions are unproven. If it cannot be secured,
    this raises :class:`CredentialPermissionError` and leaves *path* untouched
    rather than writing a token any account on the machine could read.
    """
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        if _IS_WINDOWS:
            # Release the handle before handing the path to icacls, and secure
            # + verify it BEFORE any content exists in it.
            os.close(fd)
            windows_restrict_to_owner(tmp)
            fd = os.open(str(tmp), os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        # Broader than FileNotFoundError: on Windows the unlink of a leftover
        # temp can fail with PermissionError, and that must not mask the
        # CredentialPermissionError that is the reason we are here.
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def upsert_env_var(env_path: Path, key: str, value: str) -> None:
    """Upsert ``KEY=value`` into the .env file: replace the line if the key is
    already present, append if not, preserving every other line (including
    comments and blanks) verbatim. Written atomically at 0600. Never logs
    ``value``.

    Guards line injection HERE, at the choke point every writer goes through,
    rather than only in each caller: a value that Python considers multi-line
    would forge extra .env entries — a planted ``ANTHROPIC_API_KEY=`` among
    them, which is the metered-billing escape constraint #1 exists to prevent.
    """
    assert_single_env_line(key, "key")
    assert_single_env_line(value, "value")
    lines = (env_path.read_text(encoding="utf-8").split("\n")
             if env_path.exists() else [])
    if lines and lines[-1] == "":
        lines.pop()   # split("\n") keeps the trailing empty field
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            existing_key = stripped.split("=", 1)[0].strip()
            if existing_key == key:
                out.append(f"{key}={value}")
                replaced = True
                continue
        out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    ensure_private_dir(env_path.parent)
    atomic_write_0600(env_path, "\n".join(out) + "\n")


def assert_oauth_token_usable(token: str) -> str:
    """Refuse an OAuth token that cannot work; return it stripped.

    The refusal half of :func:`set_profile_token`, split out so a caller that
    only wants to JUDGE a token — `nh init` reporting on a credential it
    FOUND already on the machine — asks the same question without writing
    anything. Two copies of "what is a usable token" is how the CLI, the HTTP
    path and the desktop app ended up with three opinions (onboarding
    walkthrough 2026-08-09, B4b); this keeps it at one.

    Refuses, in this order: an empty token; an over-length one; one carrying a
    line break that would forge a second .env line; a metered ``sk-ant-api…``
    key, which silently bills the metered API (constraint #1 is OAuth-only).
    Both personal and enterprise OAuth tokens are first-class, so it rejects
    the known-bad shape rather than whitelisting a format.
    """
    token = (token or "").strip()
    if not token:
        raise AuthError("token must not be empty")
    if len(token) > MAX_TOKEN_LEN:
        raise AuthError(
            f"token is implausibly long ({len(token)} chars, max "
            f"{MAX_TOKEN_LEN}) — refusing to write it")
    assert_single_env_line(token, "token")
    if token.casefold().startswith("sk-ant-api"):
        raise AuthError(
            "that is an ANTHROPIC_API_KEY, not an OAuth token. This field "
            "takes a subscription or enterprise OAuth token "
            "(CLAUDE_CODE_OAUTH_TOKEN) — create one with: claude setup-token. "
            "To bill your own Anthropic API account with that key, set "
            "llm.auth_mode: api_key and keep the key in ~/.no_human/.env."
        )
    return token


def set_profile_token(profile: str, token: str,
                      env_path: Path | None = None) -> str:
    """Store *profile*'s OAuth token in ``~/.no_human/.env``; return the KEY.

    ``env_path`` is resolved at CALL time, not bound as a default argument. A
    ``= ENV_PATH`` default is captured at import, so a test that redirects
    ``config.ENV_PATH`` still writes to the operator's REAL credential file —
    which is not a hypothetical: it clobbered a live token during this
    function's own development.

    Returns the variable NAME only — the token is never returned, logged, or
    echoed (constraint §8). Refuses, in this order:

    - an invalid profile name (via :func:`profile_token_var`);
    - an empty token;
    - a token containing a newline/carriage return, which would inject an
      arbitrary extra line into .env;
    - a metered API key. Constraint #1 is OAuth-only: an ``sk-ant-api…``
      credential silently bills the metered API, which is precisely what this
      product refuses to do. Both personal and enterprise OAuth tokens are
      first-class, so the check rejects the one known-bad shape rather than
      whitelisting a format and locking out a valid enterprise token.

    The last four live in :func:`assert_oauth_token_usable` so a validate-only
    caller cannot drift from the writer.
    """
    env_path = ENV_PATH if env_path is None else env_path
    key = profile_token_var(profile)
    token = assert_oauth_token_usable(token)
    upsert_env_var(env_path, key, token)
    return key


def _assert_api_key_mode(env_path: Path | None) -> ScrubReport:
    """BYO-API-key billing (``llm.auth_mode: "api_key"``).

    An operator-authorized, explicit departure from the OAuth-only default,
    for friends/commercial installs that pay Anthropic directly with THEIR OWN
    ``ANTHROPIC_API_KEY``. Invariants preserved: the run still bills exactly ONE
    path, so every OTHER metered redirect (auth token, Bedrock, Vertex) is
    scrubbed; a missing key fails loudly; no OAuth token is exported; the
    billing path is stamped as the "api_key" profile for attribution.
    """
    global _ACTIVE_AUTH_PROFILE
    env_path = ENV_PATH if env_path is None else env_path
    key = load_api_key(env_path)
    # Scrub every metered redirect EXCEPT the key we intentionally bill with.
    # ANTHROPIC_AUTH_TOKEN alongside the key yields a 401; Bedrock/Vertex would
    # silently route billing to a cloud account.
    report = scrub_metered_auth(keep=(API_KEY_VAR,))
    # An inherited subscription token must not reach the SDK subprocess either:
    # "bills exactly one path" holds by construction, not by SDK precedence.
    if os.environ.get(SUBSCRIPTION_TOKEN_VAR):
        report.removed.append(SUBSCRIPTION_TOKEN_VAR)
        del os.environ[SUBSCRIPTION_TOKEN_VAR]
    if not key:
        raise AuthError(
            "llm.auth_mode is 'api_key' but no ANTHROPIC_API_KEY was found. "
            f"Add it to {env_path} (chmod 600) or the process environment:\n"
            "  echo 'ANTHROPIC_API_KEY=sk-ant-...' >> ~/.no_human/.env\n"
            "This bills your own Anthropic API account (metered). To pay with a "
            "Claude subscription instead, set llm.auth_mode: subscription."
        )
    _ACTIVE_AUTH_PROFILE = "api_key"
    return report


def assert_subscription_mode(
    env_path: Path | None = None,
    *,
    strict: bool = True,
    profile: str | None = None,
    auth_mode: str = "subscription",
) -> ScrubReport:
    """Enforce the configured billing mode before any task runs.

    ``auth_mode="subscription"`` (the default):
      1. Scrub all metered-auth variables from the process environment.
      2. If ``ANTHROPIC_API_KEY`` was present, refuse to start (``strict``) — the
         user must unset it; a silent scrub-and-continue would mask a real
         misconfiguration the operator should know about.
      3. Load and require *profile*'s subscription token (see
         :func:`load_env_token`), exporting exactly that one.

    ``auth_mode="api_key"`` (operator-authorized BYO-API-key): bill Anthropic
    directly with the operator's own key — see :func:`_assert_api_key_mode`.

    Returns the :class:`ScrubReport` on success. Raises :class:`AuthError`
    otherwise.
    """
    if auth_mode == "api_key":
        return _assert_api_key_mode(env_path)

    report = scrub_metered_auth()

    if report.api_key_present and strict:
        raise AuthError(
            "ANTHROPIC_API_KEY is set in the environment while llm.auth_mode "
            "is 'subscription'. The key has been scrubbed from this process so "
            "a run bills exactly one path, but startup is aborted so you can "
            "fix the source.\n"
            "Unset it before starting:  unset ANTHROPIC_API_KEY\n"
            "(To bill your own Anthropic API account with that key, set "
            "llm.auth_mode: api_key.)"
        )

    env_path = ENV_PATH if env_path is None else env_path
    token = load_env_token(env_path, profile=profile)
    if not token:
        raise AuthError(
            f"No subscription token found. Expected {SUBSCRIPTION_TOKEN_VAR} in "
            f"{env_path} (chmod 600) or the process environment.\n"
            "Create one with:  claude setup-token\n"
            "Inspect configured profiles with:  nh auth status"
        )
    return report


# --------------------------------------------------------------------------- #
# Config file                                                                  #
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 8420},
    "worker": {
        # WHICH CODING BACKEND THE IMPLEMENTER RUNS ON. "claude" (the default)
        # is the Claude Agent SDK path and is unchanged in every respect — an
        # operator who edits nothing sees no behavioural difference from before
        # this key meant anything. "codex" routes the CODER, and only the coder,
        # to the OpenAI Codex CLI on the operator's own OPENAI_API_KEY.
        #
        # Reviewer, planner, supervisor and utility stay on Claude regardless:
        # the review gate and all four model tiers are pinned by ID in the
        # project's non-negotiable constraints, and the 2026-08-01 amendment
        # that sanctioned a second backend moved neither. See
        # `agent.backend.CLAUDE_PINNED_ROLES`.
        "backend": "claude",
    },
    "llm": {
        # Billing mode. "subscription" (the default) bills a Claude
        # subscription via CLAUDE_CODE_OAUTH_TOKEN and scrubs ANTHROPIC_API_KEY.
        # "api_key" (operator-authorized BYO-API-key, for friends/commercial
        # installs) bills the operator's own ANTHROPIC_API_KEY from .env instead;
        # every OTHER metered redirect is still scrubbed. Only the MODE lives in
        # config — the key itself stays in ~/.no_human/.env (never config.yaml,
        # enforced by _reject_api_key_in_config).
        "auth_mode": "subscription",
        # Which subscription pays for this process. "default" is the unsuffixed
        # CLAUDE_CODE_OAUTH_TOKEN in ~/.no_human/.env; any other name <p>
        # resolves CLAUDE_CODE_OAUTH_TOKEN_<P>. Read once at startup and
        # exported into the canonical variable, so one run can never span two
        # subscriptions. Change it with `nh auth use <profile>` and restart the
        # server — a live process keeps the token it started with.
        "auth_profile": DEFAULT_AUTH_PROFILE,
        "primary_model": "claude-sonnet-5",
        "review_model": "claude-opus-5",
        "planner_model": "claude-opus-5",
        # The supervisor is a sparse every-N-tool-calls course-corrector running
        # at effort="low", max_turns=1. It used to ride on review_model, so it
        # silently ran on Opus. It is a judging call on a short prompt, not a
        # reasoning-heavy one, and Sonnet 5 is enough for it — an explicit key
        # so the choice is visible instead of inherited.
        "supervisor_model": "claude-sonnet-5",
        # Utility tier: single-turn, effort="low", advisory jobs that summarize,
        # classify, or distill — never the implement/plan/review gates. Routing
        # these to Haiku frees the Opus window; a wrong answer here degrades a
        # hint, never a verdict. It is never the implementer, planner, reviewer
        # or supervisor — those four tiers are fixed above.
        "utility_model": "claude-haiku-4-5",
        # --- OpenAI Codex backend (only read when worker.backend == "codex") ---
        # Chosen EXPLICITLY rather than derived from a Claude tier: the four
        # Claude IDs above are fixed by constraint and mean nothing to Codex,
        # so the Codex model gets its own key and its own default. Overriding
        # this is the supported way to move the Codex tier; nothing else here
        # changes when it does.
        "codex_model": "gpt-5-codex",
        # Codex's `model_reasoning_effort`. None ⇒ let the CLI use its own
        # default. The orchestrator's `effort=` ("low"/"medium"/"high") is
        # mapped onto this per call and takes precedence when it is set.
        "codex_reasoning_effort": None,
        # Absolute path to the `codex` binary, for installs where it is not on
        # PATH. None ⇒ resolve it the way the CLI itself is normally found.
        "codex_cli_path": None,
        # MoA (Mixture-of-Agents) planning fan-out — on by default. Runs N
        # independent plan proposals from different angles, then ONE
        # aggregator call synthesizes a single plan (evidence-based synthesis,
        # never a numeric score). Reuses planner_model; no new model tier
        # introduced. Only the (cheap) planning step is affected —
        # never the implement/review loop. Set enabled=False to fall back to
        # a single planner call.
        "moa_planning": {
            "enabled": True,
            "proposers": 3,
            # Complexity gate (B2). Measured on task 61406d02, one MoA plan cost
            # 13210 + 12027 + 14493 proposer tokens + 10796 aggregator ≈ 50.5K
            # Opus tokens; the single-planner path on d9d458b5 cost 3.2K — ~16×.
            # Worse, a trivial task only discovers it is trivial after all three
            # proposers have answered SKIP_PLAN. So fan out only when the task
            # shows at least `min_signals` of the pre-plan complexity signals
            # (see orchestrator._moa_complexity_signals). Set min_signals to 0
            # for unconditional MoA, or enabled=False for none.
            "min_signals": 2,
            "criteria_threshold": 5,      # acceptance criteria ≥ this = complex
            "description_threshold": 2000,  # spec chars ≥ this = complex
        },
    },
    "database": {"path": str(DB_PATH)},
    "notifications": {
        # Write-only webhooks, alert channel only. Read context uses separate
        # read-only tokens (Phase 1). None disables the channel (logs instead);
        # with both None, notifications are logged and nothing is sent.
        "slack_webhook_url": None,
        # Microsoft Teams, via a Power Automate "Workflows" webhook. NOT the
        # classic Office 365 connector — Microsoft disabled those between
        # 2026-05-18 and 2026-05-22 and they no longer function, so
        # notify/teams.py refuses a connector URL loudly instead of posting
        # into a dead endpoint. Create one in Teams: Workflows app → "Post to a
        # channel when a webhook request is received". The URL carries its own
        # SAS credential in the query string — treat it as a secret (it is
        # scrubbed from /api/config like every other *webhook* key).
        "teams_webhook_url": None,
        # Where a human should click through to. Rendered as the Teams Adaptive
        # Card's single "Open in no_human" button (Action.OpenUrl) — the button
        # that card format was chosen for. NOT a secret and not scrubbed. Left
        # None because the board binds 127.0.0.1 by default and a localhost
        # link is dead on the phone these alerts are read on; set it only when
        # the board is actually reachable from where Teams is read.
        "board_url": None,
        "email_to": "dev@example.com",
    },
    "updates": {
        # A once-a-day check against PyPI's public JSON API that prints a single
        # line when a newer `nh` has been published. It never blocks a command
        # (the fetch runs on a daemon thread and the notice is rendered from the
        # previous run's cache) and never fails one. Set false — or export
        # NH_NO_UPDATE_CHECK=1, which also covers CI — to turn it off entirely.
        # No telemetry: this is an outbound GET for a version string, and
        # nothing about the machine or the operator is sent.
        "enabled": True,
        "interval_seconds": 86400,
    },
    "approval": {
        "require_before_merge": True,   # ALWAYS true — agent never merges
        "auto_merge_on_approval": False,  # there is no auto-merge
        "approval_timeout": "24h",
    },
    "git": {
        "branch_prefix": "no-human/",
        "commit_prefix": "",
        "never_push_to": ["main", "master", "release/*"],
        # Extra GitHub Enterprise hosts treated as GitHub (github.com is always
        # recognized). Add your GHE host (e.g. "code.example.com") to open real PRs.
        "github_hosts": ["github.com", "code.example.com"],
        # Labels applied when the agent opens a PR/MR. Some repos gate CI on a
        # label (some repos require a V* version label on PRs into their integration
        # branch). Usually
        # a per-repo concern — a task can override via its own `pr_labels`.
        "pr_labels": [],
        "agent_identity_name": "no_human",
        "agent_identity_email": "no-human@acme.com",
    },
    "safety": {
        # No size cap by default. A line/file count is a proxy for "scope
        # explosion" that cannot tell a legitimately large change (a 645-line
        # Jenkinsfile stage) from a runaway refactor, and the check runs AFTER the
        # commit — so it saves no compute, it only stops lint, tests, the reviewer
        # and the PR from ever running. The real scope guards are semantic and
        # already in place: the plan's FILES TO CHANGE list (agent/scope_guard.py),
        # the tamper guard, the evidence-based reviewer, and the human who approves
        # the PR. Set either key to a positive int to opt back in per install; a
        # task may raise its own via task.config (blockers/actions.py).
        "max_files_changed": None,
        "max_lines_changed": None,
        "forbidden_paths": [".env", "secrets/", "*.key", "*.pem"],
        "block_test_weakening": True,
    },
    "pipeline": {
        # Proportionality (2026-08-09). Measured: a one-line edit to a markdown
        # file took 35+ minutes of intake grill → 9-turn Opus planning → 9
        # skills → multi-stage review, while the complexity gate had already
        # (correctly) computed "tier simple" and nothing downstream read it.
        # ON: a task whose file set is ≤2 non-executed prose files skips the
        # grill, plans on the utility model in ≤2 turns, loads no discovered
        # skills, and gets a BOUNDED (not skipped, not weakened) review; it
        # escalates back to full ceremony the moment the plan or the actual
        # diff leaves that file set. OFF: exactly the pre-2026-08-09 pipeline.
        # What this never touches: the review gate itself, the tamper guard,
        # the export gate, and the human merge.
        "trivial_tier": {"enabled": True},
    },
    "planning": {
        # Plan-first worker (Phase 1): generate a detailed implementation plan
        # before the implement loop. Sonnet explores the codebase and writes a
        # plan the Opus worker follows. Skipped for code_review tasks.
        "enabled": True,
        "max_turns": 10,
    },
    "bounds": {
        # Must stay in step with core.bounds.Bounds' field defaults — the one
        # place the rationale for each number lives. The guard that catches drift
        # is
        #   tests/test_run_84251cb2_regressions.py
        #     ::test_bounds_defaults_have_exactly_one_source_of_truth
        # which iterates DEFAULT_CONFIG["bounds"] and asserts each key equals
        # getattr(Bounds(), key) — except max_correction_rounds, which the test
        # exempts (WAKE_ONLY) because Bounds carries no such field, and which
        # blockers/wake.py duplicates as a hardcoded fallback, so THAT number is
        # guarded by nothing and drifts silently.
        #
        # It is NOT tests/test_bounds.py, which this comment used to name and
        # which never reads DEFAULT_CONFIG at all. That mattered: changing
        # max_attempts here and running the named file gives 28 passed, so an
        # editor who does exactly what the comment says learns nothing. A pointer
        # to a guard is itself an unguarded claim — verify by breaking the value
        # and seeing which test dies, not by reading.
        "max_attempts": 3,
        "max_turns_per_attempt": 500,
        "max_correction_rounds": 2,
        # Megaplan P3: complex tasks (>4 files / large plan / decompose verdict)
        # get max_turns_per_attempt × this, so they don't exhaust turns
        # mid-implementation and fail with an empty diff (B5). 1.0 disables.
        "complex_multiplier": 1.5,
        # Lifetime caps across the task's WHOLE life, resumes included.
        # max_attempts bounds one loop, but every resume starts a fresh loop:
        # task 84251cb2 reached attempt 17 and 21.2M cache-read tokens with no
        # cap ever firing. Exceeding either cap raises a BUDGET_EXHAUSTED
        # blocker — an honest park; the human raises the budget or abandons.
        # Both are per-task overridable via task.config (the option's action).
        "lifetime_attempts": 9,
        # COST-WEIGHTED tokens, not raw ones: fresh in/out x1.0, cache write
        # x1.25, cache read x0.1 (core.pricing). 4M replaces the converted
        # 1.6M cap, which was calibrated on a ledger whose subagent spend was
        # under-counted (~17%-visible gauge, since fixed) — against honest
        # numbers 1.6M parks 117/221 real tasks (52.9%); 4M parks 6.8% and
        # sits at the knee. Full derivation and the post-baseline re-sweep
        # obligation live on core.bounds.Bounds; kept in step with it.
        "lifetime_tokens": 4_000_000,
        # Per-attempt spend cap — ends the ATTEMPT (bounded loop retries),
        # never parks the task. Raised with the lifetime cap (2:1 shape).
        # Rationale on core.bounds.Bounds.attempt_tokens.
        "attempt_tokens": 2_000_000,
    },
    # A separate section from `bounds` on purpose: `bounds` is mirrored
    # key-for-key by core.bounds.Bounds and guarded by
    # tests/test_run_84251cb2_regressions.py::
    # test_bounds_defaults_have_exactly_one_source_of_truth, which asserts every
    # key there has a Bounds field. This is policy, not a bound.
    "budget": {
        # An exhausted lifetime budget ENDS the task (status `failed`) with a
        # structured record and a wake condition naming what a human would have
        # to do, instead of asking the human "spend more, or stop here?".
        #
        # Default ON because the answer never varied. Measured 2026-08-09: of
        # 119 parked tasks awaiting a human, 69 were this one question, and the
        # operator's standing rule is "the answer is STOP. NEVER RAISE A CAP.
        # Budget raises have never once produced a merge on this project. An
        # exhausted budget means the TICKET is wrong — answer stop, then rewrite
        # it inline-complete and re-file." A question whose answer is invariant
        # policy is the product's problem, not the operator's.
        #
        # This changes the OUTCOME, never the CAP: the caps stay in `bounds`,
        # resume spend still counts against them, and raising one is still a
        # human-only act (`nh task config <id> lifetime_tokens=N`).
        #
        # False restores the old behaviour exactly — ESCALATED, with the
        # question and the raise/stop options — for an operator who would
        # rather be asked.
        "exhaustion_terminal": True,
    },
    # The nightly funnel eval (Phase C). The only knob it has: a run REFUSES
    # to start when the corpus's own ceiling sum exceeds this, so an unattended
    # 03:00 job cannot be authorised to spend more than the corpus was designed
    # to cost. The default IS that sum (400k + 1.5M + 3M + 4M + 2M across the
    # five tiers), weighted exactly as `bounds.lifetime_tokens` is weighted —
    # so out of the box the guard permits the corpus and nothing more. Raise it
    # only with a corpus that justifies the raise; `tests/test_funnel_eval.py
    # ::test_the_default_budget_is_the_corpus_ceiling` is the drift guard, and
    # it recomputes the sum from the corpus rather than restating it.
    "eval": {
        "nightly_budget_tokens": 10_900_000,
    },
    "bounds_investigation": {
        "max_attempts": 8,
        "max_turns_per_attempt": 80,
        "max_correction_rounds": 4,
    },
    "repro_gate": {
        # The reproduction-test gate (M2): "off" | "advisory" | "required".
        #   off      — never runs.
        #   advisory — runs and reports for every kind, and ENFORCES for a
        #              bugfix whose edits IN THIS ATTEMPT touched .py (the
        #              agent-edit hook's Write/Edit/MultiEdit/NotebookEdit
        #              events, reset per attempt — NOT the branch diff. So a
        #              .py edit made through bash/sed/python -c is invisible to
        #              it, as is a resumed attempt that edits only JS while the
        #              shipped diff touches Python): a
        #              "fail" OR a "waived" (no manifest) verdict fails that
        #              attempt and sends it back. Non-Python and non-bugfix
        #              changes stay report-only, so a JS/CSS bugfix is never
        #              asked for a pytest repro.
        #   required — enforces for every kind and every change.
        # advisory is NOT passive. It was when this default was written; the
        # bugfix carve-out (orchestrator: `enforced = ...`) made it partly
        # enforcing, and this comment still claimed otherwise until 2026-07-22.
        "mode": "advisory",
    },
    "tamper_adjudication": {
        # When the test-tampering guard fires, ask ONE fresh-context reviewer
        # whether the ticket REQUIRED those test changes, instead of ending the
        # task on a human's desk in the guard's own counter jargon.
        # Operator-directed, 2026-08-09.
        #
        # ON BY DEFAULT, and the reasoning is worth keeping next to the switch:
        # the guard's DETECTOR is unchanged and still absolute, and every
        # unresolved outcome still stops the run (a TAMPERING verdict costs a
        # bounded attempt, a second one parks, and any doubt at all parks). The
        # only new outcome is "the ticket asked for this, here is the criterion,
        # printed on the PR" — which is strictly more information than the
        # escalation it replaces, in front of the same human.
        #
        # false restores the pre-2026-08-09 behaviour byte for byte: every fire
        # escalates immediately with the raw findings. It exists for an operator
        # who wants no LLM in this path at all, and because a feature that
        # changes what a SAFETY gate does should be answerable with a config
        # line rather than a revert.
        "enabled": True,
    },
    "context": {
        # Repo-map seed (M3): a ~3K-token map of the repo in the coder prompt
        # to cut exploration turns. Cached per (repo, HEAD). Off = fall back to
        # pure agentic exploration.
        "repo_map_enabled": True,
    },
    # C1 seed-context diet: user-level (~/.claude/skills) skills are delivered
    # to the coder only when relevant to the task (token overlap on title/
    # description/repo path). Project-level and DB-confirmed skills are always
    # delivered. False = deliver every user skill on every task.
    "filter_user_skills": True,
    "blockers": {
        # Part 22 blocker handling.
        "max_alternatives_before_escalate": 2,
        "max_park_duration": "48h",
        "wake_poll_interval": "10m",
        "transient_infra_retries": 2,
        "escalate_on_low_confidence_below": 0.6,
        # PR comments from these authors never trigger a revision ("[bot]"
        # logins are always ignored on top). A CI service account that posts a
        # test-results table on every build is the shape this exists for:
        # treated as operator feedback, it burns an attempt per PR. The
        # default names none — set yours in `blockers:`. NOTE: a user-yaml `blockers:` section
        # replaces this map wholesale, so wake.py carries the same default.
        "ignore_comment_authors": [],
        "max_ci_fix_rounds": 3,
        # Bounded CI_GATE-failure → fix cycles on an open PR (M6), counted per
        # distinct failure signature like max_ci_fix_rounds; past the cap the
        # failing job is escalated to the human.
        "max_ci_gate_fix_rounds": 3,
        # Stuck-active watchdog: a task emitting NO event for this many minutes
        # while in an active state (implementing/reviewing/testing/planning/
        # context) is escalated as a probable hung Agent-SDK session (the
        # 2026-07-11 reviewer hang). 40 > the 30-min run_tests timeout so a
        # long test never trips it; 0 disables. wake.py mirrors this default
        # (the deep-merge trap: a user `blockers:` block replaces this map).
        "stuck_active_minutes": 40,
    },
    "supervisor": {
        # In-flight human-replacement (EVOLUTION_PLAN Phase 1). The PostToolUse
        # hook evaluates every `check_every` calls (NOT 1 — per-call LLM ≈ 8× cost
        # and serializes every action; see §1.2). preflight runs one plan check
        # before the first edit (skipped for trivial tasks via SKIP_PLAN gate).
        "enabled": True,
        "check_every": 5,
        "preflight": True,
    },
    "reviewer": {
        # Independent staff reviewer (EVOLUTION_PLAN Phase 2). 3-pass prompt,
        # pass/fail with cited evidence only — never a numeric score (constraint
        # #3). feedback_rounds reuses the bounded attempt loop, then escalates.
        "passes": ["correctness", "architecture", "edge_cases"],
        "feedback_rounds": 3,
        # When no reviewer is wired, the gate FAILS CLOSED (the task escalates).
        # It used to return a passing decision, which made the one hard gate a
        # silent rubber stamp. Set true only for eval/replay flows that skip the
        # gate on purpose; even then the skip is announced on the board.
        "allow_advisory": False,
    },
    "onboarding": {"completed": False},
    "profile": {
        # Megaplan P1 (full autonomy). By default a profile drives a task only
        # after a human confirms it (ProjectProfile.is_usable). These opt-in
        # flags let an unattended deployment run without that click:
        #   auto_confirm_proven — trust a profile whose test_cmd was PROVEN to
        #     run clean (exact command exited 0 in a real subprocess), even if a
        #     human never confirmed it. Proof, not a click, is the safety signal.
        #   auto_onboard — if a task's repo has no usable profile, derive+prove
        #     one inline before the first attempt (best-effort; never blocks).
        "auto_confirm_proven": False,
        "auto_onboard": False,
    },
    "isolation": {
        # WHERE one task runs. Every task gets its own throwaway git worktree,
        # so the agent's working tree is never the checkout the operator is
        # sitting in. On by default and independent of `concurrency` below —
        # the two were one flag, and because parallelism defaults off, the
        # default run used the live checkout and could overwrite uncommitted
        # work. Set false to deliberately run in the primary checkout; a run
        # then edits whatever is in it.
        "enabled": True,
        # Where the per-task worktrees live. None → ~/.no_human/worktrees.
        # `concurrency.worktree_root` is still read for configs written before
        # the split.
        "worktree_root": None,
    },
    "concurrency": {
        # HOW MANY tasks run at once. Phase 7: `nh serve` drains the queue into
        # a bounded asyncio pool. Default off → one task at a time. Parallelism
        # requires `isolation.enabled` (workers sharing one checkout would stomp
        # each other's index and branch), so the pool refuses rather than
        # downgrading when isolation is opted out.
        "enabled": False,
        "max_workers": 2,
        "poll_interval": "10s",   # how often `nh serve` checks for new pending tasks
        "worktree_root": None,    # pre-split alias for isolation.worktree_root
    },
    "decomposition": {
        # A single task NEVER spawns child tasks by default. All delegation for a
        # complex task happens IN-SESSION via sub-agents (the SDK Agent tool /
        # no_human_researcher), and one task may still open multiple PRs. This
        # gate (default off) is the ONLY switch that re-enables the legacy
        # LeadAgent child-task decomposition path; leave it off to keep all work
        # for a task inside that task.
        "enabled": False,
    },
    "ci": {
        # The install-wide FALLBACK. `Orchestrator._resolve_ci_runner` reads
        # this block when the project profile names no pipeline target; the
        # profile wins when it does, because it describes one repo and this
        # describes every repo the install touches. Read that method for the
        # precedence rules and docs/configuration.md for the user-facing
        # version. Until 2026-08-02 nothing read this at all — a user who
        # configured CI exactly as documented got no gate and no diagnostic —
        # so if you are changing the resolver, that is the regression to avoid.
        #
        # Opt-in per project. Set enabled=true and provide project path.
        "enabled": False,
        "backend": "gitlab",      # gitlab | github_actions | jenkins | circleci
        # The pipeline target, read by every backend: "namespace/repo"
        # (gitlab), "owner/repo" (github_actions / ghe_checkruns) or the
        # CircleCI API v2 project slug "<vcs>/<org>/<repo>", e.g. "gh/acme/svc".
        "project": "",
        "hostname": "gitlab.acme.net",
        "variables": {},          # extra pipeline variables (sent as the POST body's variables array)
        "timeout_minutes": 60,
        "max_infra_retries": 2,   # infra failures only: retry after 2 min, max 2
        "poll_interval": 30,
        "result_parser": "pytest",  # or "surefire" for Maven projects
        # --- Jenkins backend (build.example.com) ---
        # job: full job path to the branch/PR job, e.g.
        #   "job/acme-universe/job/acme-core-test-master/job/PR-042"
        "job": "",
        "base_url": "https://build.example.com",
        # mode: watch (DEFAULT, read-only poll of the PR-triggered build) |
        #       trigger (POST buildWithParameters — outward-facing, opt-in) |
        #       human_gated (a person must build the image first — park-and-wake)
        "mode": "watch",
        # Credentials are NEVER stored here. The backend reads JENKINS_USER /
        # JENKINS_API_TOKEN from ~/.no_human/.env (chmod 600) or the process env.
        "wake_hint": "",
        # auth: "token" (basic auth, DEFAULT) | "cookie" (form-login session
        # cookie). CloudBees build.example.com rejects API-token basic auth, so
        # it needs "cookie": a one-time Playwright form login (SSO_USERNAME /
        # SSO_PASSWORD in ~/.no_human/.env) captures a session that is reused
        # headlessly and auto-refreshed on expiry.
        "auth": "token",
        # crumb_path: CSRF crumb issuer, relative to base_url, used for POST
        # (trigger mode) under cookie auth. For CJOC controllers this is
        # "cjoc/crumbIssuer/api/json".
        "crumb_path": "crumbIssuer/api/json",
        # storage_state_path: where the Playwright session is persisted. Null =>
        # ~/.no_human/jenkins_storage_state.json.
        "storage_state_path": None,
        "cookie_auto_refresh": True,
    },
    "integrations": {
        # First-class integration config. github/gitlab/jenkins/slack are NOT
        # here — their status is a read-only VIEW over ci.* / notifications.*
        # (one source of truth per setting). Tokens live in ~/.no_human/.env,
        # never in this world-readable file.
        "jira": {
            "enabled": False, "site": "", "project_key": "", "jql": "", "email": "",
            # JIRA_API_TOKEN in ~/.no_human/.env
            "default_repo": "",       # where polled-in tasks run
            "write_back": False,      # opt-in: comment on status change (never transition/close)
            "poll_interval": "5m",    # floor 60s enforced at the serve hook
        },
        "linear": {
            # Polled issue intake, same role as `jira` above (a server-side
            # poller, not an argument to `nh task add`). LINEAR_API_KEY lives
            # in ~/.no_human/.env, never in this world-readable file.
            "enabled": False,
            "team_key": "",           # e.g. "ENG" — the prefix in ENG-123
            # WorkflowState.type values to pull in. Linear's seven documented
            # types are triage/backlog/unstarted/started/completed/canceled/
            # duplicate; the default takes only work nobody has started.
            "state_types": ["triage", "backlog", "unstarted"],
            "label": "",              # optional: only issues carrying this label
            "default_repo": "",       # where polled-in tasks run
            "write_back": False,      # opt-in: comment + type-matched state move
            "poll_interval": "5m",    # floor 60s enforced at the serve hook
        },
        "monday": {
            # Polled item intake, same role as `jira`/`linear` above.
            # MONDAY_API_TOKEN lives in ~/.no_human/.env, never in this
            # world-readable file.
            #
            # THE ONE REAL DIFFERENCE FROM JIRA AND LINEAR, and why this block
            # looks nothing like the one above it: Jira and Linear expose a
            # TYPED workflow state, so "pull the backlog" means the same thing
            # on every workspace. monday does not — a status column is a bag of
            # user-defined labels ("Ready for Dev", "Fixing", "Known Bug", ...)
            # that differs per board, and nothing in the API says which of them
            # means "not started yet". So the label→meaning mapping is stated
            # HERE, explicitly, and is never inferred from label text or colour.
            # With board_id/status_column unset the adapter RAISES rather than
            # returning nothing, because a silent empty result is
            # indistinguishable from an empty board.
            "enabled": False,
            "board_id": "",           # which board to pull from (numeric id, as a string)
            "status_column": "",      # the status column's ID, e.g. "bug_status" —
                                      # NOT its title. Discover with:
                                      #   boards { columns { id title type } }
            "todo_labels": [],        # labels meaning "not started yet", e.g. ["Ready for Dev"]
            "in_progress_label": "",  # optional: label to move to when work starts
            "done_label": "",         # optional: label to move to on completion
            "default_repo": "",       # where polled-in tasks run
            "write_back": False,      # opt-in: update (comment) + status-label move
            "poll_interval": "5m",    # floor 60s enforced at the serve hook
        },
        # No `circleci` block. It held `enabled` + `org_slug` + `project` and
        # NOTHING read any of the three: the CI layer builds CircleCICI from
        # `ci.project` (the API v2 project slug), and `ci.enabled` is the only
        # switch that turns a CI gate on. So the block rendered an onboarding
        # form and an on/off toggle that governed nothing, while the panel told
        # the operator CircleCI was their active CI backend and no gate ran.
        # CircleCI is configured in the `ci:` block above, exactly like
        # github_actions / gitlab / jenkins. An older config that still carries
        # this block loads fine (unknown keys are merged, not rejected) and is
        # reported unconfigured with a re-save nudge — see
        # `integrations._CIRCLECI_LEGACY_DETAIL` for why it is NOT auto-promoted.
        "slack": {
            # Opt-in Socket-Mode intake worker (SCRUM-60/61/62 split). Default
            # OFF: no worker starts and no import-time side effects occur.
            # SLACK_BOT_TOKEN / SLACK_APP_TOKEN live in ~/.no_human/.env only —
            # never in this world-readable file.
            "intake": False,
        },
        "teams": {
            # Microsoft Teams notify-OUT (notify/teams.py), the write-only
            # sibling of notify/slack.py. Until this block existed, Teams was
            # the one integration in the registry (integrations/__init__.py
            # `_ORDER`) with NO config block at all, so nothing could offer it
            # to a user and it could only be reached by hand-editing YAML.
            #
            # The webhook URL is deliberately NOT duplicated here: it stays at
            # `notifications.teams_webhook_url`, where notify.build_notifier
            # already reads it — one source of truth per setting. That URL
            # carries its own SAS credential (`sp`/`sv`/`sig`) in the query
            # string, so it is a SECRET and is never collected by onboarding.
            #
            # `enabled` is a mute switch: it turns the channel off without
            # making the operator delete a webhook they pasted. Honoured by
            # notify.build_notifier. Default True, so an install that already
            # has a webhook keeps delivering byte-for-byte as before — a False
            # default here would silently stop existing Teams alerts.
            "enabled": True,
        },
    },
    "ci_gate": {
        # M6: post-PR CI_GATE integration validation, run as a WakeWatcher rung
        # (blockers/wake.py) once the PR's normal CI is green. Deploys the service +
        # runs the integration tests on the GitLab pipeline project in a
        # throwaway per-PR namespace — NEVER a prod environment. Trigger is a
        # subprocess to `glab` (operator's local auth), not the Agent SDK.
        # Every value below is deliberately EMPTY: this block describes one
        # deployment's private CI topology (project ids, cluster names, job
        # paths), which must never ship inside the product — a packaged build
        # serves the effective config over /api/config, so anything left here
        # is readable by whoever installs it. An operator using this gate fills
        # these in via ~/.no_human/config.yaml on their own machine.
        "enabled": False,
        # GitLab numeric project id of the pipeline project to trigger.
        "project_id": None,
        "hostname": "",
        "ref": "main",
        # Repos governed by this gate, matched against the PR's repo name.
        # Empty list = gate never fires even when enabled. NOTE: a user-yaml
        # `ci_gate:` block replaces this list wholesale (deep-merge trap), so
        # consumers must treat a missing/empty list as "no match", never crash.
        # gate.py guards on `not enabled or not repos or not project_id`, so the
        # empty defaults below are inert rather than a crash.
        "repos": [],
        # Throwaway namespace, one per PR. Checked for collisions pre-trigger.
        # Must contain `{pr_number}`.
        "namespace_template": "ci-gate-pr{pr_number}",
        # The pipeline variable that carries the namespace. Must match what the
        # operator's pipeline actually reads.
        "namespace_variable": "CI_GATE_NAMESPACE",
        # Static variables sent with the pipeline trigger. Deployment-specific:
        # the operator supplies whatever their pipeline requires. Namespace and
        # image variables are injected at run time.
        "variables": {},
        "poll_interval": 30,
        "timeout": 3600,
        # Cluster used to resolve latest_dev images (ci_gate/images.py) and to
        # check namespace collisions before triggering. kubectl only — the
        # registry API 401s and the enrich job is a separate (Part A) path.
        "kubeconfig": "",
        # Part A: code PRs get an image built FROM the PR via a Jenkins enrich
        # job, triggered externally with the operator's own SSO Basic auth
        # + session-scoped crumb — NEVER the Jenkins credential store.
        # pr_build=False turns code PRs into honest escalations instead.
        "pr_build": True,
        "enrich_job_url": "",
        "jenkins_controller": "",
        "registry_prefix": "",
    },
    "hooks": {
        # Per-edit lint feedback (agent/lint_hook.py): after each Edit/Write,
        # lint the changed file and inject hard errors straight back into the
        # session. SWE-agent's single biggest ACI win (arXiv 2405.15793) —
        # a non-parsing edit costs one hook round instead of a whole failed
        # attempt. ON by default (W1.3): deterministic, no LLM cost, no-op
        # unless the repo has a confirmed lint command, fail-open on linter
        # timeout/absence (lint_hook.py: `not result.ran → {}`).
        "per_edit_lint": True,
    },
    "docs": {
        # M-A: the local, in-house repo wiki (docs_gen). Generated by the
        # existing Claude backend into <repo>/.no_human/wiki/ (commit-excluded,
        # never sent to any third party) and provided to the agent /
        # no_human_researcher as an on-demand reference.
        # `nh docs generate <repo>` is always available; these keys gate ONLY
        # the background WikiRefreshJob in `nh serve`. Default off → no
        # unattended backend cost until you opt in.
        "auto_refresh": False,
        "refresh_interval_seconds": 3600,  # HEAD-diff check cadence when serving
        "max_turns": 12,                   # bound the read-only recon session
    },
    # The team-brain client (src/no_human/brain/). THREE keys, and this is the
    # whole of its configuration surface — see that package's docstring for why
    # a fourth would be a problem rather than a feature.
    #
    # `enabled: false` is not a default, it is invariant L4: with it false the
    # package is never imported, no file is created, no socket is opened, and
    # not one byte of any prompt differs from a build with src/no_human/brain/
    # deleted. tests/test_brain_invariants.py asserts that byte-identity.
    #
    # `control_plane_url` is the ONLY thing this product knows about the hosted
    # service. No region, no account id, no table, no bucket, no ARN — the
    # service's shape is deliberately unlearnable from the client, and a grep
    # gate in the same test file fails the build if any of it appears.
    #
    # No credential lives here. The brain credential is a separate secret in a
    # separate file (~/.no_human/brain/credentials.json) read by a separate
    # loader, and it is never placed in os.environ — unlike the Claude token
    # above, which is exported on purpose because the Agent SDK subprocess must
    # inherit it. That difference is the point.
    "team_brain": {
        "enabled": False,
        "control_plane_url": "",
        # A withdrawn rule must not live forever on a laptop that stopped
        # syncing: past this many days without a VERIFIED sync, remote rules
        # stop being injected until one succeeds.
        "max_stale_days": 14,
    },
    "learning": {
        # D3-M1: auto-confirm a RECURRING review-origin lesson without a human
        # click — but ONLY into the CODER's channel, NEVER the reviewer's. The
        # channel split (core/db.py `confirmed_by` + the orchestrator's
        # reviewer-memory exclusion) preserves gate independence (constraint #3)
        # BY CONSTRUCTION: an auto-confirmed review lesson reaches the coder and
        # can never reach the reviewer that produced it.
        #
        # Modeled on `profile.auto_confirm_proven` — the same "proof, not a
        # click" shape, default OFF. Here the proof is the same review finding
        # recurring across >=2 DISTINCT tasks in one project that each reached
        # HUMAN approval (a MERGED PR outcome, migration 0010). A miss is always
        # the safe direction: it withholds a lesson, it never lets one through.
        "auto_confirm_recurring": False,
    },
}


def worktree_isolation_enabled(config: dict[str, Any]) -> bool:
    """True when a task must run in its own git worktree. Default TRUE.

    Reads ``isolation.enabled``. Every config.yaml written before isolation and
    parallelism were split lacks the block entirely — including the full default
    dump ``nh init`` writes, which pins ``concurrency.enabled: false`` — so an
    absent key has to resolve to the new default, not to the old coupled one.
    """
    return bool((config.get("isolation") or {}).get("enabled", True))


def parallelism_enabled(config: dict[str, Any]) -> bool:
    """True when more than one task may run at a time. Default FALSE."""
    return bool((config.get("concurrency") or {}).get("enabled", False))


def worktree_root(config: dict[str, Any]) -> Path:
    """Directory the per-task worktrees are created under.

    ``isolation.worktree_root`` first, then the pre-split
    ``concurrency.worktree_root`` (an operator who relocated their worktrees
    must not have them silently move back), then ``~/.no_human/worktrees``.
    """
    root = ((config.get("isolation") or {}).get("worktree_root")
            or (config.get("concurrency") or {}).get("worktree_root"))
    return Path(root).expanduser() if root else (NO_HUMAN_HOME / "worktrees")


# A worktree directory is named `<task_id>.<owner_pid>.<token>`. The three parts
# each do one job: the task id makes the directory attributable (the doctor's
# orphan check reads it, and so does an operator staring at the root), the owner
# pid says which process is entitled to it, and the random token is what makes
# the name UNIQUE PER RUN — the whole point. Before this shape the path was the
# bare task id, so overlapping attempts of one task shared one checkout and the
# first to finish removed the directory the other was working in.
#
# Directories in the OLD bare-`<task_id>` shape still exist under existing
# worktree roots; both readers below accept them (the parse simply yields the
# whole name) so nothing is orphaned by the rename.
def worktree_owner(dir_name: str) -> tuple[str, int | None]:
    """Split a worktree directory NAME into ``(task_id, owner_pid)``.

    ``owner_pid`` is None for the legacy bare-``<task_id>`` shape and for any
    name that does not parse — callers must treat "no owner" as "cannot prove a
    live owner", never as "definitely dead"… except where the old code already
    took the directory (see the orchestrator's reaper, which is scoped to the
    one task it is about to run and is the same reclaim the old acquire did).
    """
    parts = dir_name.split(".")
    if len(parts) >= 3 and parts[1].isdigit():
        return parts[0], int(parts[1])
    return parts[0], None


def pid_alive(pid: int) -> bool:
    """Whether *pid* names a live process.

    Errs toward ALIVE. Every caller uses this to decide whether removing a
    worktree is safe, so an unanswerable question — a pid we are not allowed to
    signal, a platform that refuses — must read as "in use". A recycled pid
    likewise reads as alive: that leaks a directory the doctor then reports,
    where the opposite error deletes a checkout somebody is working in.
    """
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto a DEEP copy of ``base``.

    The copy has to be deep. ``dict(base)`` duplicates only the top level, so
    every section the user's config.yaml does not mention came back as
    DEFAULT_CONFIG's OWN nested dict — and a caller writing into its own
    resolved config then re-pointed the default for the whole process. Measured
    2026-08-10: the nightly eval sets ``server.port`` on its isolated instance's
    config, which moved ``DEFAULT_CONFIG["server"]["port"]`` from 8420 to 8431
    and surfaced (a suite away) as a README-claims failure. Lists are copied for
    the same reason — ``never_push_to`` and ``forbidden_paths`` are the shapes a
    caller is most likely to append to.
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


@dataclass
class Config:
    """Resolved configuration: defaults overlaid with the user's config.yaml."""

    data: dict[str, Any]
    path: Path

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def primary_model(self) -> str:
        return self.data["llm"]["primary_model"]

    @property
    def review_model(self) -> str:
        return self.data["llm"]["review_model"]

    @property
    def planner_model(self) -> str:
        return self.data.get("llm", {}).get("planner_model", self.review_model)

    @property
    def utility_model(self) -> str:
        return self.data.get("llm", {}).get(
            "utility_model", DEFAULT_CONFIG["llm"]["utility_model"]
        )

    @property
    def worker_backend(self) -> str:
        return self.data.get("worker", {}).get("backend", "claude")

    @property
    def db_path(self) -> Path:
        return Path(self.data["database"]["path"]).expanduser()


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (POSIX ``os.replace``).

    Writes to a sibling ``.tmp`` file first, then replaces the target in a
    single rename — so a concurrent reader of *path* will never see a
    half-written file.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def load_config(
    config_path: Path = CONFIG_PATH,
    *,
    create_if_missing: bool = True,
) -> Config:
    """Load ``~/.no_human/config.yaml``, generating a default if absent.

    Refuses to honour an ``ANTHROPIC_API_KEY`` smuggled into the config file —
    that variable must never appear in config (constraint §3.1).
    """
    # Outside the fresh-config branch: an ALREADY-INITIALISED install never
    # reached this, so its ~/.no_human stayed at whatever umask created it
    # until someone happened to write a credential.
    # Chmod ONLY our own directory: widening that to every load meant a caller
    # passing a custom config path had ITS directory forced to 0700 —
    # including, for a relative path, the CWD.
    #
    # But still CREATE whatever parent was asked for. Scoping the chmod also
    # dropped the mkdir, which silently narrowed a contract: a custom path
    # under a missing parent used to be created and started raising
    # FileNotFoundError from _atomic_write_text instead.
    if config_path.parent == NO_HUMAN_HOME:
        ensure_private_dir(NO_HUMAN_HOME)
    elif create_if_missing:
        config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists() and create_if_missing:
        _atomic_write_text(config_path, yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False))

    user_data: dict[str, Any] = {}
    if config_path.exists():
        user_data = yaml.safe_load(config_path.read_text()) or {}

    _reject_api_key_in_config(user_data)
    if "tracker" in user_data:
        warnings.warn(
            "The 'tracker' config section is deprecated and ignored — the TRACKER "
            "integration was removed. Delete it from config.yaml to silence this.",
            DeprecationWarning,
            stacklevel=2,
        )
    merged = _deep_merge(DEFAULT_CONFIG, user_data)
    merged.pop("tracker", None)  # ignore any stale block from an old config
    return Config(data=merged, path=config_path)


def set_auth_profile(profile: str, config_path: Path = CONFIG_PATH) -> str:
    """Pin ``llm.auth_profile`` in config.yaml. Returns the normalized name.

    The key is edited as text, not via a ``safe_load``/``safe_dump`` round-trip,
    because that would silently delete the operator's hand-written comments —
    among them the "model IDs are intentionally NOT pinned here" warning that
    exists precisely because a frozen dump once shadowed the real defaults.

    A text edit into YAML is only safe if the value cannot inject structure, so
    the name is validated first; the result is then verified by re-resolving the
    config, and the original file is restored on any mismatch.
    """
    profile = validate_profile_name(profile)

    load_config(config_path)  # materialize a default file if there is none
    original = config_path.read_text()
    lines = original.splitlines()

    try:
        start = next(i for i, ln in enumerate(lines) if ln.rstrip() == "llm:")
    except StopIteration:
        lines.extend(["llm:", f"  auth_profile: {profile}"])
    else:
        end = len(lines)
        for i in range(start + 1, len(lines)):
            line = lines[i]
            if line.strip() and not line[:1].isspace():
                end = i
                break
        for i in range(start + 1, end):
            stripped = lines[i].lstrip()
            if stripped.startswith("auth_profile:"):
                indent = lines[i][: len(lines[i]) - len(stripped)]
                lines[i] = f"{indent}auth_profile: {profile}"
                break
        else:
            lines.insert(start + 1, f"  auth_profile: {profile}")

    _atomic_write_text(config_path, "\n".join(lines) + "\n")

    resolved = load_config(config_path).data["llm"].get("auth_profile")
    if resolved != profile:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set auth profile: {config_path} resolved to {resolved!r} "
            f"after the edit, not {profile!r}. The file has been restored."
        )
    return profile


def _reject_api_key_in_config(data: dict[str, Any]) -> None:
    """Fail loudly if a metered API key was placed in config (it never should).

    Covers BOTH vendors' keys. The rule is not "Anthropic's key is special" —
    it is that config.yaml is a plain, world-readable, frequently-copied file
    and no credential belongs in one. Adding the second coding backend added a
    second key that could be put there, so it is named here in the same breath;
    a guard that enumerates one vendor is a guard that misses the next one.
    """
    banned = {API_KEY_VAR, CODEX_API_KEY_VAR}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.upper() in banned:
                    raise AuthError(
                        f"{key.upper()} must never appear in config.yaml. "
                        "The auth *mode* may live in config; the key itself "
                        "belongs only in ~/.no_human/.env (chmod 600) or the "
                        "process environment."
                    )
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
