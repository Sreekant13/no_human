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
from .ghe_checkruns import (
    _ACCESS_ENV_KEY,
    _is_auth_error,
    check_runs_to_result,
    fetch_check_runs,
    fetch_commit_statuses,
    merge_ci_results,
    statuses_to_result,
)

log = logging.getLogger(__name__)


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
        # Injectable so tests never touch the network / the `gh` CLI. Signatures
        # mirror ``fetch_check_runs(repo, ref, *, hostname) -> list[dict]`` and
        # ``fetch_commit_statuses(repo, ref, *, hostname) -> dict``.
        fetch_runs: Callable[..., Any] | None = None,
        fetch_statuses: Callable[..., Any] | None = None,
    ):
        self.repo = repo
        self.workflow = workflow
        self.timeout_minutes = timeout_minutes
        self.poll_interval = poll_interval
        self.variables = variables or {}
        self.result_parser = result_parser
        self.hostname = hostname
        self._fetch_runs = fetch_runs or fetch_check_runs
        self._fetch_statuses = fetch_statuses or fetch_commit_statuses

    async def _read_source(
        self, fetch: Callable[..., Any], to_result: Callable[..., CIResult],
        ref: str,
    ) -> CIResult:
        """Fetch ONE surface (check-runs or commit-statuses) -> CIResult,
        classifying read failures.

        An auth wall -> ``access_failure``; any other read error -> a transient
        ``infra_failure``. Both map to ``UNKNOWN`` (never green) and carry their
        flag, which ``merge_ci_results`` propagates and never drops.
        """
        try:
            data = await fetch(self.repo, ref, hostname=self.hostname)
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
        return to_result(data, ref=ref)

    async def _read(self, ref: str) -> CIResult:
        """Read BOTH surfaces for ``ref`` and merge -> a single CIResult.

        Actions results surface as check-runs; external reporters may instead
        POST to the Commit Status API. Reading only one would miss the other, so
        both are read and merged such that a green on one surface never hides a
        failure (or an unread/access wall) on the other. Zero of both -> UNKNOWN,
        never green.
        """
        checks = await self._read_source(self._fetch_runs, check_runs_to_result, ref)
        statuses = await self._read_source(self._fetch_statuses, statuses_to_result, ref)
        return merge_ci_results(checks, statuses, ref=ref)

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
