"""Tell the operator a newer `nh` exists — without ever getting in the way.

The CLI half of the update story needs no code signing, so unlike the desktop
shell it works today. It is also the half most likely to become an irritant, so
the constraints are deliberately severe:

* **Never blocks.** The notice is rendered from a CACHE that was written by an
  earlier invocation. The network fetch happens on a daemon thread that nobody
  waits for. A command's runtime is therefore unaffected even on a dead link —
  the worst case is that the cache stays stale one more invocation.
* **Never fails the command.** Every path here is wrapped. A corrupt cache, a
  read-only home, a DNS failure, PyPI returning HTML: all of them mean "no
  notice", never a traceback and never a non-zero exit.
* **Never nags.** At most one network check a day, and the notice itself is a
  single line.
* **Always disableable**, by config (`updates.enabled: false`) or by the
  `NH_NO_UPDATE_CHECK` environment variable, which also covers CI without
  needing a config file.

The daemon-thread design has one honest limitation, recorded rather than
hidden: a command that exits in well under the time of an HTTPS round trip
(`nh --version`) dies before the refresh completes, so its cache write is lost
and the next invocation tries again. That is the correct trade — a version
check must not add latency to the fastest commands.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

#: PyPI's public JSON API. The project name is the DISTRIBUTION name
#: (`no-human`), not the import name (`no_human`) — they differ, and using the
#: import name returns 404 forever, which reads exactly like "no update".
PYPI_JSON_URL = "https://pypi.org/pypi/no-human/json"

#: Once a day. Matches the desktop shell's throttle so the two halves behave
#: the same way.
CHECK_INTERVAL_SECONDS = 86_400

CACHE_NAME = "update-check.json"

#: Short enough that an abandoned daemon thread does not linger, long enough
#: for a normal round trip.
FETCH_TIMEOUT_SECONDS = 3.0

#: Set to any non-empty value other than "0" to silence the check entirely.
DISABLE_ENV_VAR = "NH_NO_UPDATE_CHECK"


def cache_dir() -> Path:
    """Where the cached result lives. Mirrors context/repo_map.py's cache."""
    return Path.home() / ".no_human" / "cache"


# --------------------------------------------------------------------------- #
# version comparison
# --------------------------------------------------------------------------- #

def parse_version(text: Any) -> tuple[int, ...] | None:
    """Parse a dotted numeric version, or return None.

    Deliberately strict: anything that is not a plain `x.y.z` (a dev build, an
    HTML error page, an empty string) yields None, and `is_newer` refuses to
    act on None. Announcing an "update" derived from a parse failure is worse
    than announcing nothing.
    """
    if not isinstance(text, str):
        return None
    core = text.strip().lstrip("v").split("+")[0].split("-")[0]
    if not core:
        return None
    parts = core.split(".")
    out: list[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        out.append(int(part))
    return tuple(out) if out else None


def is_newer(latest: Any, current: Any) -> bool:
    """True only when both parse and *latest* is strictly greater."""
    a = parse_version(latest)
    b = parse_version(current)
    if a is None or b is None:
        return False
    # Pad so 0.2 and 0.2.0 compare equal rather than by length.
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #

def read_cache(directory: Path | None = None) -> dict[str, Any]:
    """Read the cached check. Always a dict, never raises."""
    directory = directory or cache_dir()
    try:
        raw = (directory / CACHE_NAME).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cache(data: dict[str, Any], directory: Path | None = None) -> bool:
    """Persist the cached check ATOMICALLY. Returns whether it landed.

    The write is atomic because the writer is a daemon thread that can be
    killed at any instant by process exit; a half-written file would then be
    read as corrupt on every subsequent run.
    """
    directory = directory or cache_dir()
    try:
        try:
            from .config import ensure_private_dir

            ensure_private_dir(directory)
        except Exception:  # noqa: BLE001 - privacy hardening is best-effort
            directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, directory / CACHE_NAME)
        except BaseException:
            # Includes the KeyboardInterrupt/SystemExit a dying thread may see.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except (OSError, ValueError):
        return False


def is_due(last_check: Any, now: float, interval: float = CHECK_INTERVAL_SECONDS) -> bool:
    """Whether enough time has passed to hit the network again."""
    try:
        last = float(last_check)
    except (TypeError, ValueError):
        return True  # never checked, or unreadable state
    return (now - last) >= interval


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #

def fetch_latest_version(url: str = PYPI_JSON_URL,
                         timeout: float = FETCH_TIMEOUT_SECONDS) -> str | None:
    """Ask PyPI for the newest published version. None on any failure.

    Uses urllib rather than httpx: this runs on a throwaway thread during CLI
    startup, and the stdlib import is already paid for. Follows the existing
    precedent in cli/commands.py::_server_owns_worker — lazy import, small
    timeout, enumerated exceptions, failure is a value.
    """
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if getattr(resp, "status", 200) != 200:
                return None
            # Cap the read: a redirected or hostile endpoint must not stream an
            # unbounded body into a background thread.
            payload = json.loads(resp.read(1_000_000).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    except Exception:  # noqa: BLE001 - a version check may never raise
        return None
    version = (payload or {}).get("info", {}).get("version")
    return version if isinstance(version, str) else None


def refresh_cache(now: float, directory: Path | None = None,
                  fetch: Callable[[], str | None] | None = None) -> dict[str, Any] | None:
    """Fetch and persist. Returns the new cache entry, or None on failure."""
    fetcher = fetch or fetch_latest_version
    try:
        latest = fetcher()
    except Exception:  # noqa: BLE001 - never propagate out of the thread
        return None
    if not latest:
        # Record the ATTEMPT anyway, or an offline machine retries on every
        # single invocation forever.
        write_cache({"last_check": now}, directory)
        return None
    entry = {"last_check": now, "latest": latest}
    write_cache(entry, directory)
    return entry


# --------------------------------------------------------------------------- #
# the entry point the CLI calls
# --------------------------------------------------------------------------- #

def is_disabled(config: Any = None, env: dict[str, str] | None = None) -> bool:
    """Whether the operator has turned the check off."""
    env = os.environ if env is None else env
    flag = env.get(DISABLE_ENV_VAR, "")
    if flag.strip() and flag.strip() != "0":
        return True
    if config is None:
        return False
    try:
        # `or {}` is load-bearing: a config.yaml with a bare `updates:` key and
        # a commented-out body deep-merges to None, not to a dict.
        section = config.get("updates") or {}
        return section.get("enabled", True) is False
    except Exception:  # noqa: BLE001 - a malformed config must not break the CLI
        return False


def update_notice(current: str, cache: dict[str, Any]) -> str | None:
    """The one line to show, or None. Pure — no clock, no network, no disk."""
    latest = cache.get("latest")
    if not is_newer(latest, current):
        return None
    return (f"A new version of no_human is available: {latest} "
            f"(you have {current}). Upgrade with:  pip install --upgrade no-human")


def check_for_update(current: str, *, config: Any = None,
                     directory: Path | None = None,
                     now: float | None = None,
                     fetch: Callable[[], str | None] | None = None,
                     background: bool = True) -> str | None:
    """Return the notice to print, and schedule a refresh if one is due.

    Never raises. Never blocks when *background* is True (the default) — the
    returned notice always comes from the cache an EARLIER invocation wrote.
    """
    try:
        if is_disabled(config):
            return None
        import time as _time

        moment = _time.time() if now is None else now
        cache = read_cache(directory)

        if is_due(cache.get("last_check"), moment):
            if background:
                # daemon=True: process exit must never wait on this.
                thread = threading.Thread(
                    target=refresh_cache, args=(moment, directory, fetch),
                    name="nh-update-check", daemon=True)
                thread.start()
            else:
                refreshed = refresh_cache(moment, directory, fetch)
                if refreshed is not None:
                    cache = refreshed

        return update_notice(current, cache)
    except Exception:  # noqa: BLE001 - a version check may NEVER break a command
        return None
