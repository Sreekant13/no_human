"""GitHub Actions backend — READ-ONLY status reader (Phase 2C, CI.1).

GitHub Actions runs are triggered automatically by the push, and an OAuth/PAT
token can *read* check runs but cannot *create* them (writing check runs is
GitHub-App-only). So this backend never runs ``gh workflow run`` — it polls the
branch head's check runs (Actions results surface there) until they reach a
terminal state, and returns a parsed ``CIResult``.

Design decisions (see plans/2026-07-20-phase2c-ci-design.md):
- **No trigger.** Re-triggering would double-run and needs write scope we don't
  assume. Read-only only.
- **No CI configured must NOT read as green.** Zero check runs -> ``UNKNOWN``
  (``CIResult.passed`` is SUCCESS-only), never a false pass.
- **A missing/invalid token is an ACCESS wall, not a transient infra blip.** It
  maps to ``access_failure`` + the exact env key to set, so the orchestrator
  parks for a human instead of pointlessly retrying.

Reuses the proven read-only helpers in ``ghe_checkruns`` (``fetch_check_runs``,
``check_runs_to_result``) so there is one code path for reading GitHub check
runs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from .base import CIBackend, CIResult, PipelineStatus
from .ghe_checkruns import check_runs_to_result, fetch_check_runs

log = logging.getLogger(__name__)

# The env key a human must set to clear an access wall. GitHub's CLI reads this.
_ACCESS_ENV_KEY = "GH_TOKEN"

# Substrings in a `gh api` error that mean "auth wall" (not a transient blip):
# retrying will never help — only a human granting a valid token will. Matched
# as "http 401"/"http 403" (NOT bare "401"/"403", which appear inside byte
# counts / millisecond timers on unrelated transient errors).
_AUTH_SIGNALS: tuple[str, ...] = (
    "http 401", "http 403", "bad credentials", "requires authentication",
    "must authenticate", "gh auth login", "resource not accessible",
    "not accessible by",
)

# Transient conditions that HAPPEN to carry a 403/404 but are NOT auth walls —
# retrying (with backoff) is correct, so these must fall through to infra_failure.
_TRANSIENT_SIGNALS: tuple[str, ...] = (
    "rate limit",        # GitHub emits `HTTP 403: API rate limit exceeded` — clears itself
    "no commit found",   # a 404 for a not-yet-pushed SHA — a token won't fix it; re-check
    "no ref found",
)


def _is_auth_error(message: str) -> bool:
    m = message.lower()
    if any(sig in m for sig in _TRANSIENT_SIGNALS):
        return False
    return any(sig in m for sig in _AUTH_SIGNALS)


class GitHubActionsCI(CIBackend):
    name = "github_actions"
    max_infra_retries = 0  # read-only, nothing to trigger/retry

    def __init__(
        self,
        repo: str,
        workflow: str = "",
        ref_prefix: str = "",
        timeout_minutes: int = 60,
        max_infra_retries: int = 2,
        poll_interval: int = 30,
        variables: dict[str, str] | None = None,
        result_parser: str = "pytest",
        *,
        hostname: str = "",
        # Injectable so tests never touch the network / the `gh` CLI. Signature
        # mirrors ``fetch_check_runs(repo, ref, *, hostname) -> list[dict]``.
        fetch_runs: Callable[..., Any] | None = None,
    ):
        self.repo = repo
        self.workflow = workflow
        self.timeout_minutes = timeout_minutes
        self.poll_interval = poll_interval
        self.variables = variables or {}
        self.result_parser = result_parser
        self.hostname = hostname
        self._fetch_runs = fetch_runs or fetch_check_runs

    async def _read(self, ref: str) -> CIResult:
        """One read of ``ref``'s check runs -> CIResult, classifying failures.

        An auth wall -> ``access_failure``; any other read error -> a transient
        ``infra_failure``. Zero check runs -> ``UNKNOWN`` (handled by
        ``check_runs_to_result``), which is never green.
        """
        try:
            runs = await self._fetch_runs(self.repo, ref, hostname=self.hostname)
        except Exception as exc:  # noqa: BLE001 — normalize gh/RuntimeError to a CIResult
            msg = str(exc)
            if _is_auth_error(msg):
                log.warning("GitHub Actions read blocked (access) for %s@%s: %s",
                            self.repo, ref, msg)
                return CIResult(
                    pipeline_id=ref, pipeline_url="",
                    status=PipelineStatus.UNKNOWN,
                    access_failure=True, access_env_key=_ACCESS_ENV_KEY,
                    parsed_output=f"cannot read GitHub checks: {msg}",
                )
            log.warning("GitHub Actions read failed (infra) for %s@%s: %s",
                        self.repo, ref, msg)
            return CIResult(
                pipeline_id=ref, pipeline_url="",
                status=PipelineStatus.UNKNOWN,
                infra_failure=True,
                parsed_output=f"transient error reading GitHub checks: {msg}",
            )
        return check_runs_to_result(runs, ref=ref)

    async def trigger(
        self, branch: str, extra_variables: dict[str, str] | None = None
    ) -> CIResult:
        """READ-ONLY: poll ``branch``'s check runs until terminal or timeout.

        Named ``trigger`` to satisfy the ``CIBackend`` contract, but it does NOT
        start a run — GitHub Actions is already running from the push.
        """
        deadline = asyncio.get_event_loop().time() + self.timeout_minutes * 60
        while True:
            result = await self._read(branch)
            # Terminal, or nothing to wait on (UNKNOWN = no checks / access /
            # infra) -> return now. A read error already carries its flag.
            if result.status.is_terminal or result.status == PipelineStatus.UNKNOWN:
                return result
            if asyncio.get_event_loop().time() > deadline:
                log.warning("GitHub Actions timeout for %s@%s", self.repo, branch)
                return result
            await asyncio.sleep(self.poll_interval)

    async def check_status(self, pipeline_id: str) -> CIResult:
        """Non-blocking single read of ``pipeline_id`` (a branch or SHA)."""
        return await self._read(pipeline_id)
