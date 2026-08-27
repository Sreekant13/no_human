"""Background runner for persisted wiki-generation jobs (migrations/0017).

Wiki generation is a bounded Agent SDK session that can take minutes. Running
it as a foreground `await` in the onboarding endpoint meant the result died
with the request — a wizard unmount or a server restart lost it. Here it is a
job row the board polls: ``run_job`` drives one row queued → running →
done|failed, and ``resume_unfinished`` fails the orphans a restart left behind.

Deliberately thin: it knows a ``Store`` (the four ``*_wiki_job`` methods) and a
*generator* that has ``async generate(repo_path) -> WikiResult``. It does NOT
know how to build a backend or a profile — that stays in the caller, so this
module has no Agent SDK or config dependency and is trivially testable.
"""

from __future__ import annotations

import json
from typing import Any

from .core.db import _now


async def run_job(store: Any, job_id: str, generator: Any) -> None:
    """Drive one wiki job to completion. Any exception becomes a failed row."""
    job = await store.get_wiki_job(job_id)
    if job is None:
        return
    await store.update_wiki_job(job_id, status="running", started_at=_now())
    try:
        result = await generator.generate(job["repo_path"])
    except Exception as exc:  # noqa: BLE001 — any failure is a failed job, not a crash
        await store.update_wiki_job(
            job_id, status="failed", error=str(exc), finished_at=_now())
        return
    if result.error:
        await store.update_wiki_job(
            job_id, status="failed", error=result.error, finished_at=_now())
    else:
        await store.update_wiki_job(
            job_id, status="done",
            files=json.dumps(result.files_written), finished_at=_now())


async def resume_unfinished(store: Any) -> None:
    """At startup, fail every queued/running job — nobody is running it now.

    Mirrors the scheduler's orphan recovery: a job left mid-flight by a restart
    must not read as still in progress. The board shows the failure; the user
    can regenerate."""
    for job in await store.list_wiki_jobs(status="queued"):
        await store.update_wiki_job(
            job["id"], status="failed", error="server restarted",
            finished_at=_now())
    for job in await store.list_wiki_jobs(status="running"):
        await store.update_wiki_job(
            job["id"], status="failed", error="server restarted",
            finished_at=_now())
