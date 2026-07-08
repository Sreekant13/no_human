"""Jenkins session-cookie auth for CloudBees Jenkins (build.example.com).

This Jenkins uses a **local form login** (``j_username`` / ``j_password``);
API-token basic auth is rejected. We obtain a session cookie (``JSESSIONID*``)
via a one-time Playwright form login, persist it as a Playwright
``storage_state`` JSON (chmod 600), and reuse it for the curl-based REST calls
in ``JenkinsCI``. On expiry we refresh headlessly.

Credentials (``SSO_USERNAME`` / ``SSO_PASSWORD``) come from ``~/.no_human/.env``
or the process env — never from config, never logged. Cookie *values* are never
logged either; only names.

Playwright is an optional dependency (the ``e2e`` extra). If it is not
installed, refresh degrades gracefully: any existing ``storage_state`` is still
usable, and if none exists the caller sees an access wall (park) rather than a
crash.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlparse

log = logging.getLogger("no_human.ci.jenkins_session")

# Default location of the persisted Playwright storage_state (session cookies).
DEFAULT_STATE = os.path.expanduser("~/.no_human/jenkins_storage_state.json")

# Login form selectors (Jenkins classic security realm), with generic fallbacks.
_USER_SEL = ("input[name='j_username'], input#j_username, "
             "input[type='text'][id*='user'], input[type='email']")
_PASS_SEL = "input[name='j_password'], input#j_password, input[type='password']"
_SUBMIT_SEL = ("button[type='submit'], input[type='submit'], "
               "input[name='Submit'], button:has-text('Sign in'), "
               "button:has-text('log in')")


def load_cookies(state_path: str, base_url: str) -> dict[str, str]:
    """Return {name: value} for cookies in *state_path* matching *base_url*'s
    host. Path is intentionally ignored — curl sends the cookie on every request
    to the host, which is how the session cookie (set on ``/cjoc``) authenticates
    the REST API. Returns {} if the file is missing/unparseable."""
    try:
        with open(state_path) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {}
    host = urlparse(base_url).netloc
    out: dict[str, str] = {}
    for c in state.get("cookies", []) or []:
        dom = str(c.get("domain", "")).lstrip(".")
        if dom and (host == dom or host.endswith("." + dom)):
            name, val = c.get("name"), c.get("value")
            if name and val is not None:
                out[name] = val
    return out


def refresh(
    base_url: str,
    state_path: str = DEFAULT_STATE,
    *,
    headless: bool = True,
    username: str | None = None,
    password: str | None = None,
    timeout_ms: int = 60000,
) -> bool:
    """Perform a Playwright form login against *base_url* and persist the
    resulting session to *state_path* (chmod 600). Returns True if a session
    cookie was captured. Never logs credentials or cookie values."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("playwright not installed (pip install 'playwright'); "
                    "cannot refresh Jenkins session")
        return False

    if username is None or password is None:
        from ..config import load_env_var
        username = username or load_env_var("SSO_USERNAME")
        password = password or load_env_var("SSO_PASSWORD")
    if not username or not password:
        log.warning("no SSO_USERNAME/SSO_PASSWORD in ~/.no_human/.env; "
                    "cannot refresh Jenkins session")
        return False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(base_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_selector(_USER_SEL, timeout=timeout_ms)
            page.fill(_USER_SEL, username)
            page.fill(_PASS_SEL, password)
            page.click(_SUBMIT_SEL)
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:  # noqa: BLE001 — networkidle can be flaky; state is what matters
                pass
            os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
            ctx.storage_state(path=state_path)
            os.chmod(state_path, 0o600)
            browser.close()
    except Exception as exc:  # noqa: BLE001 — refresh is best-effort
        log.warning("Jenkins session refresh failed: %s", exc)
        return False

    cookies = load_cookies(state_path, base_url)
    if cookies:
        log.info("refreshed Jenkins session; cookie names: %s",
                 sorted(cookies))
        return True
    log.warning("Jenkins login completed but no session cookie was captured")
    return False


def get_session_cookies(
    base_url: str,
    state_path: str = DEFAULT_STATE,
    *,
    auto_refresh: bool = True,
    force_refresh: bool = False,
) -> dict[str, str]:
    """Return session cookies for *base_url*, refreshing via Playwright when
    needed. ``force_refresh`` re-logs in even if a state file exists (used after
    an access wall, since presence of an expired cookie can't be detected from
    the file alone)."""
    if force_refresh:
        if refresh(base_url, state_path):
            return load_cookies(state_path, base_url)
        return {}
    cookies = load_cookies(state_path, base_url)
    if cookies:
        return cookies
    if auto_refresh and refresh(base_url, state_path):
        return load_cookies(state_path, base_url)
    return {}
