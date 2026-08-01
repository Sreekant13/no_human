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


class AuthError(RuntimeError):
    """Raised when the process is not provably in subscription-billing mode."""


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
    for raw in env_path.read_text().split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            entries[key.strip()] = value
    return entries


_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
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
    """
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
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
    lines = env_path.read_text().split("\n") if env_path.exists() else []
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
    """
    env_path = ENV_PATH if env_path is None else env_path
    key = profile_token_var(profile)
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
        # Single in-process backend: the Claude Agent SDK. No alternate
        # backend abstraction (lean-stack).
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
        # Write-only webhook, alert channel only. Read context uses separate
        # read-only tokens (Phase 1). None disables Slack (logs instead).
        "slack_webhook_url": None,
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
        # label (e.g. metrics-core requires a V* version label on PRs into dev). Usually
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
    "planning": {
        # Plan-first worker (Phase 1): generate a detailed implementation plan
        # before the implement loop. Sonnet explores the codebase and writes a
        # plan the Opus worker follows. Skipped for code_review tasks.
        "enabled": True,
        "max_turns": 10,
    },
    "bounds": {
        # Must stay in step with core.bounds.Bounds' field defaults — the one
        # place the rationale for each number lives. tests/test_bounds.py fails
        # if they drift.
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
        # x1.25, cache read x0.1 (core.pricing). 1.6M weighted is the old raw
        # 8M converted at this install's measured 0.1985 weighted/raw ratio —
        # the same real spend, now bounded the same way for every task rather
        # than 5.7x looser for a cache-heavy one. Measured rationale and the
        # migration note live on core.bounds.Bounds; kept in step with it.
        "lifetime_tokens": 1_600_000,
        # Per-attempt spend cap — ends the ATTEMPT (bounded loop retries),
        # never parks the task. Also cost-weighted (old raw 4M converted).
        # Rationale on core.bounds.Bounds.attempt_tokens.
        "attempt_tokens": 800_000,
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
        # Opt-in per project. Set enabled=true and provide project path.
        "enabled": False,
        "backend": "gitlab",      # gitlab | github_actions | jenkins | circleci
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
        "circleci": {
            "enabled": False, "org_slug": "", "project": "",
            # CIRCLECI_TOKEN in ~/.no_human/.env
        },
        "slack": {
            # Opt-in Socket-Mode intake worker (SCRUM-60/61/62 split). Default
            # OFF: no worker starts and no import-time side effects occur.
            # SLACK_BOT_TOKEN / SLACK_APP_TOKEN live in ~/.no_human/.env only —
            # never in this world-readable file.
            "intake": False,
        },
    },
    "ci_gate": {
        # M6: post-PR CI_GATE integration validation, run as a WakeWatcher rung
        # (blockers/wake.py) once the PR's normal CI is green. Deploys metrics-core +
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


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto a copy of ``base``."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
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
    """Fail loudly if a metered API key was placed in config (it never should)."""

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.upper() == "ANTHROPIC_API_KEY":
                    raise AuthError(
                        "ANTHROPIC_API_KEY must never appear in config.yaml. "
                        "The auth *mode* may live in config; the key itself "
                        "belongs only in ~/.no_human/.env (chmod 600) or the "
                        "process environment."
                    )
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
