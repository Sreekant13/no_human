"""GitHub Enterprise check-runs reader (IMPROVEMENT_PLAN D).

Reads check-run status from the GitHub / GHE Checks API for a given commit/ref.
This is NOT GitHub Actions (which *triggers* workflows) — it reads status checks
that external systems (Jenkins, GitLab, etc.) report to GHE.

Uses ``gh api`` for auth + GHE host routing (the CLI handles ``GH_ENTERPRISE_TOKEN``
and host selection transparently). Falls back cleanly when ``gh`` is missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from typing import Any

from .base import CIBackend, CIResult, JobResult, PipelineStatus

log = logging.getLogger(__name__)

# Map GHE check-run conclusions to our PipelineStatus.
_CONCLUSION_MAP: dict[str, PipelineStatus] = {
    "success": PipelineStatus.SUCCESS,
    "failure": PipelineStatus.FAILED,
    "cancelled": PipelineStatus.CANCELED,
    "skipped": PipelineStatus.SKIPPED,
    "timed_out": PipelineStatus.FAILED,
    "action_required": PipelineStatus.MANUAL,
    "neutral": PipelineStatus.SUCCESS,  # neutral = advisory, not failure
    "stale": PipelineStatus.UNKNOWN,
}


def _run_gh(args: list[str], *, hostname: str = "") -> dict[str, Any]:
    """Run ``gh api`` and parse the JSON response.

    ``hostname`` targets a GHE instance (e.g. ``code.example.com``); when empty,
    ``gh`` uses its default host.
    """
    cmd = ["gh", "api"]
    if hostname:
        cmd += ["--hostname", hostname]
    cmd += args
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api failed ({result.returncode}): {result.stderr.strip()}")
    return json.loads(result.stdout)


async def fetch_check_runs(
    repo: str,
    ref: str,
    *,
    hostname: str = "",
) -> list[dict[str, Any]]:
    """Fetch check runs for ``ref`` (branch or SHA) from ``repo``.

    Returns the raw check_runs list from the API. Runs in a thread to keep the
    event loop unblocked.
    """
    endpoint = f"/repos/{repo}/commits/{ref}/check-runs"
    data = await asyncio.to_thread(_run_gh, [endpoint], hostname=hostname)
    return data.get("check_runs", [])


def check_runs_to_result(
    check_runs: list[dict[str, Any]],
    *,
    ref: str = "",
    url_hint: str = "",
) -> CIResult:
    """Convert raw check_runs into a ``CIResult``.

    The overall status is SUCCESS only when ALL check runs have concluded
    successfully. If any are still in progress, status is RUNNING.
    """
    if not check_runs:
        return CIResult(
            pipeline_id=ref,
            pipeline_url=url_hint,
            status=PipelineStatus.UNKNOWN,
        )

    jobs: list[JobResult] = []
    any_in_progress = False
    any_failed = False
    any_indeterminate = False

    # A completed check counts as "clean" ONLY with one of these conclusions.
    # Anything else that has completed (action_required, stale, startup_failure,
    # a null/unknown conclusion, ...) is INDETERMINATE — we must not call it
    # green (a workflow that failed to start or is awaiting manual approval has
    # NOT run the tests). SKIPPED = didn't need to run; NEUTRAL maps to SUCCESS.
    _CLEAN = (PipelineStatus.SUCCESS, PipelineStatus.SKIPPED)

    for cr in check_runs:
        name = cr.get("name", "?")
        conclusion = (cr.get("conclusion") or "").lower()
        status = (cr.get("status") or "").lower()
        web_url = cr.get("html_url", "")

        if status in ("queued", "in_progress"):
            any_in_progress = True
            jobs.append(JobResult(name=name, status="running", web_url=web_url))
            continue

        mapped = _CONCLUSION_MAP.get(conclusion, PipelineStatus.UNKNOWN)
        if mapped in (PipelineStatus.FAILED, PipelineStatus.CANCELED):
            any_failed = True
        elif mapped not in _CLEAN:
            any_indeterminate = True
        jobs.append(JobResult(
            name=name,
            status=mapped.value,
            failure_reason=conclusion if mapped == PipelineStatus.FAILED else None,
            web_url=web_url,
        ))

    if any_in_progress:
        overall = PipelineStatus.RUNNING
    elif any_failed:
        overall = PipelineStatus.FAILED
    elif any_indeterminate:
        # A completed-but-not-clean check we cannot vouch for -> UNKNOWN, never
        # green. The caller treats UNKNOWN as "no verdict", not a pass.
        overall = PipelineStatus.UNKNOWN
    else:
        overall = PipelineStatus.SUCCESS

    return CIResult(
        pipeline_id=ref,
        pipeline_url=url_hint,
        status=overall,
        jobs=jobs,
    )


class GHECheckRunsCI(CIBackend):
    """Read-only CI backend that reads check-run results from GitHub/GHE.

    Unlike GitHubActionsCI, this does NOT trigger CI — it only reads the
    status checks that external systems post to the GHE Checks API.
    """

    name = "ghe_checkruns"
    max_infra_retries = 0  # read-only, nothing to retry

    def __init__(
        self,
        repo: str,
        *,
        hostname: str = "",
        poll_interval: int = 30,
        timeout_minutes: int = 60,
    ):
        self.repo = repo
        self.hostname = hostname
        self.poll_interval = poll_interval
        self.timeout_minutes = timeout_minutes

    async def trigger(
        self, branch: str, extra_variables: dict[str, str] | None = None
    ) -> CIResult:
        """Poll check runs until all are complete or timeout."""
        deadline = asyncio.get_event_loop().time() + self.timeout_minutes * 60
        while True:
            runs = await fetch_check_runs(
                self.repo, branch, hostname=self.hostname,
            )
            result = check_runs_to_result(runs, ref=branch)
            if result.status.is_terminal or result.status == PipelineStatus.UNKNOWN:
                return result
            if asyncio.get_event_loop().time() > deadline:
                log.warning("GHE check-runs timeout for %s/%s", self.repo, branch)
                return result
            await asyncio.sleep(self.poll_interval)

    async def check_status(self, pipeline_id: str) -> CIResult:
        """Non-blocking check of current status for a ref."""
        runs = await fetch_check_runs(
            self.repo, pipeline_id, hostname=self.hostname,
        )
        return check_runs_to_result(runs, ref=pipeline_id)
