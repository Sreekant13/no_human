"""Context gathering: keywords, codebase search, completeness, parallel gather."""

import subprocess

import pytest

from no_human.context import ContextGatherer, check_completeness, keywords
from no_human.context.base import ContextChunk, TaskContext
from no_human.context.codebase import CodebaseSource
from no_human.context.teams import TeamsSource
from no_human.core.task import Task


def test_keywords_extracts_identifiers_and_id():
    t = Task.new("Fix AnalyticsExportService retention bug", external_id="PROJ-1",
                 description="The results topic retention is 36 hours")
    kw = keywords(t)
    assert "PROJ-1" in kw
    assert any("AnalyticsExportService" == k or "retention" == k for k in kw)
    assert "the" not in kw  # stopword


@pytest.fixture
def code_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "exporter.py").write_text(
        "class AnalyticsExporter:\n    RETENTION_HOURS = 36\n    def export(self): ...\n"
    )
    (repo / "unrelated.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "add exporter"], check=True, capture_output=True)
    return repo


async def test_codebase_source_finds_relevant_file(code_repo):
    t = Task.new("update AnalyticsExporter retention", repo_path=str(code_repo))
    chunks = await CodebaseSource().gather(t)
    files = [c.title for c in chunks]
    assert "exporter.py" in files
    assert any(c.title == "recent commits" for c in chunks)


async def test_codebase_source_no_repo():
    t = Task.new("x", repo_path="/nonexistent/path")
    assert await CodebaseSource().gather(t) == []


async def test_codebase_source_excludes_pytest_cache(code_repo):
    """Cache/lint dirs (.pytest_cache, .mypy_cache, etc.) must never surface as
    context — they're internal tool state, not source code, and their contents
    (e.g. cached test node ids) can accidentally keyword-match the search terms."""
    cache_dir = code_repo / ".pytest_cache" / "v" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "nodeids").write_text(
        "test_exporter.py::test_retention_hours_is_36\n"
    )
    t = Task.new("update AnalyticsExporter retention", repo_path=str(code_repo))
    chunks = await CodebaseSource().gather(t)
    assert not any(".pytest_cache" in c.title for c in chunks)


async def test_codebase_source_excludes_suffixed_venv_dirs(code_repo):
    """Exact-name matching missed suffixed virtualenv dirs (e.g. `.venv312`,
    `venv39`) that coexist with a plain `.venv` in the same repo — a real
    case observed in a work repo. The exclusion must be a glob."""
    venv_dir = code_repo / ".venv312" / "lib" / "site-packages" / "pytest"
    venv_dir.mkdir(parents=True)
    (venv_dir / "__init__.py").write_text(
        "RETENTION_HOURS = 36  # unrelated vendored code that happens to match\n"
    )
    t = Task.new("update AnalyticsExporter retention", repo_path=str(code_repo))
    chunks = await CodebaseSource().gather(t)
    assert not any(".venv312" in c.ref for c in chunks)


def test_completeness_binary():
    t = Task.new("x", repo_path="/r")
    t.acceptance_criteria = ["does the thing"]
    ctx = TaskContext(chunks=[ContextChunk("codebase", "f.py", "...")])
    rep = check_completeness(t, ctx)
    assert rep.ok is True
    assert set(rep.present) == {"acceptance_criteria", "target_repo", "worked_example_or_doc"}


def test_completeness_flags_missing():
    t = Task.new("x")  # no repo, no criteria
    rep = check_completeness(t, TaskContext())
    assert rep.ok is False
    assert "acceptance_criteria" in rep.missing
    assert "target_repo" in rep.missing
    assert "worked_example_or_doc" in rep.missing


class _FakeComms:
    def search(self, query, limit=10):
        return [{"title": f"thread about {query}", "body": "discussion text",
                 "url": "https://teams/x"}]


async def test_teams_source_with_injected_client():
    t = Task.new("Analytics export retention", external_id="PROJ-1")
    chunks = await TeamsSource(_FakeComms()).gather(t)
    assert chunks and chunks[0].source == "teams"
    assert "PROJ-1" in chunks[0].title


def test_graph_client_requires_token():
    import pytest
    from no_human.context.teams import GraphTeamsClient
    with pytest.raises(RuntimeError, match="token not configured"):
        GraphTeamsClient().search("anything")


def test_graph_client_real_search_wiring():
    """GraphTeamsClient issues the documented Graph /search/query request and
    parses the response into the gatherer's record shape (mock transport — the
    live token run is user-gated)."""
    import json as _json

    import httpx

    from no_human.context.teams import GraphTeamsClient

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"value": [{"hitsContainers": [{"hits": [
            {"summary": "chat summary", "resource": {
                "body": {"content": "deploy is broken"},
                "from": {"user": {"displayName": "Dana"}},
                "webUrl": "https://teams/msg/1"}},
            {"resource": {
                "subject": "Re: retention", "bodyPreview": "see the doc",
                "from": {"emailAddress": {"address": "x@y.com"}},
                "webLink": "https://outlook/mail/2"}},
        ]}]}]})

    client = GraphTeamsClient({"context": {"m365": {"token": "ro-tok"}}},
                              transport=httpx.MockTransport(handler))
    results = client.search("retention", limit=7)

    # request was the documented Graph search shape, with the read-only token
    assert seen["url"] == GraphTeamsClient.GRAPH_SEARCH_URL
    assert seen["auth"] == "Bearer ro-tok"
    req = seen["body"]["requests"][0]
    assert req["query"]["queryString"] == "retention"
    assert req["size"] == 7
    assert req["entityTypes"] == ["chatMessage", "message"]
    # response parsed into {title, body, from, url} for both chat + email hits
    assert results[0] == {"title": "Dana", "subject": "", "body": "deploy is broken",
                          "from": "Dana", "url": "https://teams/msg/1"}
    assert results[1]["subject"] == "Re: retention"
    assert results[1]["url"] == "https://outlook/mail/2"


class _BoomSource:
    name = "boom"

    async def gather(self, task):
        raise RuntimeError("kaboom")


class _SlowSource:
    name = "slow"

    async def gather(self, task):
        import asyncio
        await asyncio.sleep(5)
        return []


async def test_gatherer_isolates_failure_and_timeout(code_repo):
    t = Task.new("AnalyticsExporter", repo_path=str(code_repo))
    t.acceptance_criteria = ["x"]
    # The timeout must be well ABOVE the real CodebaseSource's file I/O (which
    # under parallel CI load can exceed a 0.2s budget and flake) and well BELOW
    # _SlowSource's 5s sleep — 1.5s isolates both cleanly.
    g = ContextGatherer([CodebaseSource(), _BoomSource(), _SlowSource()],
                        per_source_timeout=1.5)
    ctx = await g.gather(t)
    # codebase still produced chunks despite the other two failing
    assert any(c.source == "codebase" for c in ctx.chunks)
    assert "boom" in ctx.errors
    assert "slow" in ctx.errors and "timed out" in ctx.errors["slow"]
    assert ctx.completeness.ok is True


async def test_failure_recall_finds_the_past_fix(tmp_path):
    """W3.3: a new task hitting a familiar failure gets 'a similar failure
    was recorded on task X' from FTS recall — instead of rediscovering it."""
    import time
    from no_human.core.db import Store
    from no_human.core.task import Task
    from no_human.context.sessions import SessionsSource

    async with Store(tmp_path / "nh.db") as store:
        old = Task.new("Jenkinsfile CPS fix", repo_path="/tmp/x")
        await store.create_task(old)
        await store.save_events(old.id, [{
            "source": "watcher", "kind": "pr_ci_red", "ts": time.time(),
            "text": "CI failing (continuous-integration/jenkins/pr-head): "
                    "MethodTooLargeException in Jenkinsfile CPS compiler",
        }])
        new = Task.new("fix the Jenkinsfile pipeline stage", repo_path="/tmp/x")
        new.description = "jenkins CPS compiler MethodTooLargeException again"
        chunks = await SessionsSource(store)._recall_failures(
            ["jenkinsfile", "cps", "methodtoolargeexception"])
        assert chunks, "the past failure must be recalled"
        assert old.id[:8] in chunks[0].title or old.id[:8] in chunks[0].content
        assert "MethodTooLargeException" in chunks[0].content


async def test_failure_recall_never_breaks_gathering(tmp_path):
    from no_human.core.db import Store
    from no_human.context.sessions import SessionsSource
    async with Store(tmp_path / "nh.db") as store:
        # Hostile FTS tokens must yield [] not an exception.
        out = await SessionsSource(store)._recall_failures(['a"b', "NEAR", ""])
        assert out == []
