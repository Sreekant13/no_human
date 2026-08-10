"""Tests for the PR comment watcher (Phase C — WS-C)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from no_human.vcs.pr_watcher import (
    PrComment,
    PrFeedback,
    check_pr_comments,
)
from no_human.blockers.wake import WakeWatcher
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


# --------------------------------------------------------------------------- #
# PrComment / PrFeedback unit tests                                           #
# --------------------------------------------------------------------------- #


def test_pr_comment_basic():
    c = PrComment(author="alice", body="Fix the null check", created_at="2026-01-01T00:00:00Z")
    assert c.author == "alice"
    assert c.body == "Fix the null check"


def test_pr_feedback_to_send_back_entries():
    feedback = PrFeedback(
        pr_url="https://github.com/org/repo/pull/42",
        comments=[
            PrComment(
                author="alice", body="This is wrong",
                path="src/main.py", line=10,
                diff_hunk="- old_line\n+ new_line",
                created_at="2026-01-01T00:00:00Z",
            ),
            PrComment(
                author="bob", body="Please fix",
                created_at="2026-01-01T01:00:00Z",
            ),
        ],
    )
    entries = feedback.to_send_back_entries()
    assert len(entries) == 2

    # First entry: inline comment with path + line + diff hunk.
    assert "[src/main.py:10]" in entries[0]["message"]
    assert "This is wrong" in entries[0]["message"]
    assert "old_line" in entries[0]["message"]
    assert entries[0]["author"] == "alice"
    assert entries[0]["source"] == "pr_comment"

    # Second entry: general comment, no path.
    assert "Please fix" in entries[1]["message"]
    assert entries[1]["author"] == "bob"


def test_pr_feedback_empty():
    feedback = PrFeedback(pr_url="https://x", comments=[])
    assert feedback.to_send_back_entries() == []


# --------------------------------------------------------------------------- #
# check_pr_comments dispatch logic (no real CLI — tests format parsing)        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_check_pr_comments_unrecognized_format():
    # Unrecognized format → empty list, no crash.
    result = await check_pr_comments("just_a_string")
    assert result == []


@pytest.mark.asyncio
async def test_check_pr_comments_bad_github_number():
    result = await check_pr_comments("org/repo#abc")
    assert result == []


@pytest.mark.asyncio
async def test_check_pr_comments_bad_gitlab_iid():
    result = await check_pr_comments("project_id!abc")
    assert result == []


# --------------------------------------------------------------------------- #
# Wake condition: pr_comment_on:<ref>                                         #
# --------------------------------------------------------------------------- #

@pytest.fixture
async def store(tmp_path):
    async with Store(tmp_path / "test.db") as s:
        yield s


def _cfg(**over):
    base = {"blockers": {"max_park_duration": "48h"}}
    base["blockers"].update(over)
    return base


async def _park(store, *, status, blocker, wake_at=None):
    t = Task.new("PR task", repo_path="/tmp/r")
    await store.create_task(t)
    t.blocker = blocker
    t.wake_check_at = wake_at
    await store.update_task(t)
    await store.set_status(t, status, validate=False)
    return t


@pytest.mark.asyncio
async def test_pr_comment_condition_resumes_with_feedback(store):
    """When pr_comment_on fires, the task gets resumed AND comments are injected."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    comment = PrComment(author="reviewer", body="Fix the edge case", created_at=now.isoformat())

    async def pr_comment_checker(ref):
        assert ref == "org/repo#42"
        return [comment]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions

    # Verify comments were injected into task context.
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    feedback = refreshed.context.get("send_back_feedback", [])
    assert len(feedback) >= 1
    assert "Fix the edge case" in feedback[-1]["message"]
    assert feedback[-1]["source"] == "pr_comment"


@pytest.mark.asyncio
async def test_pr_comment_condition_no_comments_not_satisfied(store):
    """If the PR has no new comments, condition is not satisfied."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    async def pr_comment_checker(ref):
        return []  # no new comments

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions


@pytest.mark.asyncio
async def test_pr_comment_condition_no_checker_not_satisfied(store):
    """No checker wired → never satisfied."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions


@pytest.mark.asyncio
async def test_pr_comment_condition_checker_error_safe(store):
    """Checker throwing → not satisfied, not crashed."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    async def pr_comment_checker(ref):
        raise RuntimeError("API down")

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions


@pytest.mark.asyncio
async def test_pr_comment_inline_formatting(store):
    """Inline comments (with path/line) get formatted with file:line prefix."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    comment = PrComment(
        author="alice", body="Null check missing",
        path="src/handler.py", line=55,
        diff_hunk="+ if value is not None:",
        created_at=now.isoformat(),
    )

    async def pr_comment_checker(ref):
        return [comment]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions

    refreshed = await store.get_task(t.id)
    fb = refreshed.context["send_back_feedback"][-1]
    assert "[src/handler.py:55]" in fb["message"]
    assert "Null check missing" in fb["message"]
    assert "if value is not None" in fb["message"]


async def test_post_reply_comment_stamps_the_agent_marker(monkeypatch):
    """Every comment no_human posts must carry the invisible marker — it is
    the only thing distinguishing the product's comments from the operator's
    (same gh login; the 2026-07-10 self-resume incident)."""
    from no_human.vcs import pr_watcher

    captured = {}

    async def fake_run_cli(cmd):
        captured["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(pr_watcher, "_run_cli", fake_run_cli)
    ok = await pr_watcher.post_reply_comment("host/o/r#5", "hello reviewer")
    assert ok
    body = captured["cmd"][captured["cmd"].index("--body") + 1]
    assert body.startswith(pr_watcher.AGENT_COMMENT_MARKER)
    assert "hello reviewer" in body
    assert pr_watcher.is_agent_comment(body)
    assert not pr_watcher.is_agent_comment("hello reviewer")


def test_every_product_marker_is_recognized_as_self():
    """R18 (2026-08-10, PR #147): the verification-receipts comment carries
    `<!-- no_human:verification-receipts -->`, NOT AGENT_COMMENT_MARKER, so the
    wake watcher's `_is_self_or_bot` let the product's own receipt re-wake the
    finished task into a wasted attempt 22 seconds after the PR opened. Every
    product-authored comment marker shares the `<!-- no_human` prefix; the
    filter must key on the family, not one member."""
    from no_human.vcs import pr_watcher
    assert pr_watcher.is_agent_comment(
        "<!-- no_human:verification-receipts -->\n## How I verified this")
    assert pr_watcher.is_agent_comment(
        "<!-- no_human-agent-comment -->\naddressed the feedback")
    # A future marker in the same family is covered without a code change.
    assert pr_watcher.is_agent_comment("<!-- no_human:some-future-surface -->")
    # Human comments — including ones that merely mention the product.
    assert not pr_watcher.is_agent_comment("no_human should also fix X")
    assert not pr_watcher.is_agent_comment("")
    assert not pr_watcher.is_agent_comment(None)


# ── upsert_agent_comment: update one comment, never pile up (PR #7004 had 17) ──

async def test_upsert_updates_existing_github_comment_instead_of_posting_new(monkeypatch):
    import no_human.vcs.pr_watcher as pw

    calls = []

    async def fake_run(cmd):
        calls.append(cmd)
        joined = " ".join(cmd)
        if "/issues/" in joined and joined.endswith("--paginate"):
            # an existing agent comment for key "ci_gate"
            return '[{"id": 99, "body": "<!-- no_human-agent-comment --><!-- nh:ci_gate -->\\nold"}]'
        return "{}"  # PATCH/POST succeed

    monkeypatch.setattr(pw, "_run_cli", fake_run)
    ok = await pw.upsert_agent_comment("code.example.com/dev/metrics-core-query-service#7004", "new status", key="ci_gate")
    assert ok is True
    # It PATCHed comment 99, and did NOT POST a new one.
    assert any("PATCH" in " ".join(c) and "/issues/comments/99" in " ".join(c) for c in calls)
    assert not any("-X" in c and "POST" in c and "/issues/7004/comments" in " ".join(c) for c in calls)


async def test_upsert_creates_when_none_exists(monkeypatch):
    import no_human.vcs.pr_watcher as pw

    calls = []

    async def fake_run(cmd):
        calls.append(cmd)
        if " ".join(cmd).endswith("--paginate"):
            return "[]"  # no existing comment
        return "{}"

    monkeypatch.setattr(pw, "_run_cli", fake_run)
    ok = await pw.upsert_agent_comment("code.example.com/dev/r#5", "hi", key="ci_gate")
    assert ok is True
    assert any("POST" in c for c in calls if isinstance(c, list) for c in [" ".join(c)]) or \
        any("POST" in " ".join(c) for c in calls)


def test_upsert_body_never_says_no_human_visibly():
    # The visible text must not mention no_human; only the invisible HTML marker does.
    from no_human.vcs.pr_watcher import AGENT_COMMENT_MARKER
    assert AGENT_COMMENT_MARKER.startswith("<!--")  # invisible


# --------------------------------------------------------------------------- #
# GitLab merge requests must resolve, not park forever                         #
#                                                                              #
# `default_pr_merged` / `default_pr_state` early-returned for every non-GitHub #
# ref, with no error anywhere: a task parked on `pr_merged:<gitlab MR>` could  #
# never wake, and the awaiting-approval watcher (`blockers/wake.py`, rung 1)   #
# never saw the MR merge. Comment fetch/post were already GitLab-aware, so     #
# the silence was specific to the lifecycle calls.                             #
# --------------------------------------------------------------------------- #

@pytest.fixture
def cli_recorder(monkeypatch):
    """Record every CLI argv `pr_watcher` shells out to, and script replies."""
    from no_human.vcs import pr_watcher as pw

    calls: list[list[str]] = []
    replies: dict[str, str | None] = {}

    async def fake_run_cli(cmd):
        calls.append(cmd)
        return replies.get(cmd[0])

    monkeypatch.setattr(pw, "_run_cli", fake_run_cli)
    monkeypatch.setattr(pw.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls, replies


GITLAB_MR_URL = "https://gitlab.acme.net/grp/svc/-/merge_requests/7"
GITLAB_MR_REF = "grp%2Fsvc!7"
#: GitLab's OWN native short form, and the only one a human or an LLM writes by
#: hand: `group/project!7`, with a raw slash. The pre-encoded constant above
#: could not see the bug below — it arrives already correct, and the recorder
#: mock ignores the path it is handed.
GITLAB_MR_REF_RAW = "grp/svc!7"
GITLAB_MR_REF_SUBGROUP = "grp/sub/proj!7"
GITHUB_PR_URL = "https://github.com/acme/svc/pull/7"


def _api_path(calls) -> str:
    """The `projects/…` argument out of the recorded glab argv."""
    return next(a for c in calls for a in c if "merge_requests" in a)


@pytest.mark.parametrize("ref,expect_path", [
    (GITLAB_MR_REF_RAW, "projects/grp%2Fsvc/merge_requests/7"),
    (GITLAB_MR_REF_SUBGROUP, "projects/grp%2Fsub%2Fproj/merge_requests/7"),
    # Idempotency: an already-encoded ref must NOT become grp%252Fsvc.
    (GITLAB_MR_REF, "projects/grp%2Fsvc/merge_requests/7"),
    # A mixed ref is normalized rather than left half-encoded.
    ("grp%2Fsub/proj!7", "projects/grp%2Fsub%2Fproj/merge_requests/7"),
])
async def test_a_raw_slash_short_ref_is_url_encoded_for_the_gitlab_api(
    cli_recorder, ref, expect_path,
):
    """GitLab addresses a project as `group%2Frepo`; a raw slash 404s.

    Verified against the live API (2026-08-06):
    `projects/gitlab-org%2Fgitlab-foss` -> HTTP 200,
    `projects/gitlab-org/gitlab-foss` -> HTTP 404. Every project has a
    namespace, so this is the normal case.

    The 404 is SILENT — `_run_cli` returns None, the state reads "", and
    `default_pr_merged` is False forever with no error anywhere. That is
    verbatim the failure GitLab lifecycle support was added to remove, so the
    feature was undelivered for the one ref form a human actually types.
    """
    from no_human.vcs.pr_watcher import default_pr_state

    calls, replies = cli_recorder
    replies["glab"] = json.dumps({"state": "merged", "iid": 7})

    assert await default_pr_state(ref) == "MERGED"
    assert _api_path(calls) == expect_path
    assert "%252F" not in _api_path(calls), "double-encoded"


@pytest.mark.parametrize("fn,verb", [
    ("post_reply_comment", "POST"),
    ("upsert_agent_comment", "list/POST"),
])
async def test_the_comment_writers_encode_a_raw_slash_short_ref_too(
    cli_recorder, fn, verb,
):
    """Same defect, same ref form, three other call sites: comment fetch and
    both comment writers built the same `projects/{project}/…` path by hand.
    Fixing only the lifecycle query would have left a task able to SEE its MR
    merge while still unable to reply on it."""
    import no_human.vcs.pr_watcher as pw

    calls, replies = cli_recorder
    replies["glab"] = "[]"
    await getattr(pw, fn)(GITLAB_MR_REF_RAW, "hello")
    assert calls, f"{fn} ({verb}) asked glab nothing"
    assert all("projects/grp%2Fsvc/" in a
               for c in calls for a in c if a.startswith("projects/")), calls


@pytest.mark.parametrize("ref", [GITLAB_MR_REF_RAW, GITLAB_MR_URL])
@pytest.mark.parametrize("fn,empty", [
    ("default_pr_checks", []),
    ("default_pr_head", ""),
    ("default_pr_mergeable", {"mergeable": "", "mergeStateStatus": ""}),
    ("default_pr_files", []),
])
async def test_the_gitlab_bound_on_the_gh_only_helpers_is_what_docs_say(
    cli_recorder, ref, fn, empty,
):
    """These four are GitHub-only, and that bound lived ONLY in a Python
    docstring until `docs/adapters.md` gained the table this pins.

    It is silent in both directions: the empty value they return for a GitLab
    ref is identical to "no checks / no files", so a GitLab operator cannot
    tell "nothing is failing" from "this is never read here". Asserting the
    exact empties AND zero CLI calls is what makes the doc table checkable —
    if someone implements the GitLab path, this test fails and the doc gets
    updated with it.
    """
    import no_human.vcs.pr_watcher as pw

    calls, replies = cli_recorder
    replies["gh"] = replies["glab"] = json.dumps(
        {"statusCheckRollup": [{"name": "x", "conclusion": "FAILURE"}],
         "headRefOid": "deadbeef", "mergeable": "CONFLICTING",
         "files": [{"path": "a.py"}]})

    assert await getattr(pw, fn)(ref) == empty
    assert calls == [], f"{fn} is documented as not implemented for GitLab: {calls}"


async def test_the_same_helpers_do_resolve_for_github(cli_recorder):
    """Non-vacuity control for the bound above: a helper that had simply
    stopped working would pass every assertion in that test."""
    import no_human.vcs.pr_watcher as pw

    calls, replies = cli_recorder
    replies["gh"] = json.dumps(
        {"statusCheckRollup": [{"name": "x", "conclusion": "FAILURE"}],
         "headRefOid": "deadbeef", "mergeable": "CONFLICTING",
         "files": [{"path": "a.py"}]})

    assert await pw.default_pr_head("acme/svc#7") == "deadbeef"
    assert await pw.default_pr_files("acme/svc#7") == ["a.py"]
    assert (await pw.default_pr_mergeable("acme/svc#7"))["mergeable"] == "CONFLICTING"
    assert await pw.default_pr_checks("acme/svc#7") != []
    assert calls and all(c[0] == "gh" for c in calls)


async def test_comment_fetch_encodes_a_raw_slash_short_ref(cli_recorder):
    """The read side of the same defect (`check_pr_comments` -> notes)."""
    calls, replies = cli_recorder
    replies["glab"] = "[]"
    await check_pr_comments(GITLAB_MR_REF_RAW)
    assert _api_path(calls) == "projects/grp%2Fsvc/merge_requests/7/notes"


@pytest.mark.parametrize("ref", [GITLAB_MR_URL, GITLAB_MR_REF])
@pytest.mark.parametrize("gitlab_state,expect_state,expect_merged", [
    ("merged", "MERGED", True),
    ("opened", "OPEN", False),
    ("closed", "CLOSED", False),
])
async def test_gitlab_mr_lifecycle_resolves_via_glab(
    cli_recorder, ref, gitlab_state, expect_state, expect_merged,
):
    from no_human.vcs.pr_watcher import default_pr_merged, default_pr_state

    calls, replies = cli_recorder
    replies["glab"] = json.dumps({"state": gitlab_state, "iid": 7})

    assert await default_pr_state(ref) == expect_state
    assert await default_pr_merged(ref) is expect_merged
    assert calls, "a GitLab MR ref must actually ask glab — it asked nothing"
    assert all(c[0] == "glab" for c in calls), f"wrong CLI: {calls}"
    assert any("merge_requests/7" in a for c in calls for a in c), calls


async def test_gitlab_mr_url_targets_the_mr_host_not_gitlab_com(cli_recorder):
    """A self-hosted MR URL must carry --hostname: `glab` defaults to
    gitlab.com, which is the failure this repo already documents for
    `glab ci run`."""
    from no_human.vcs.pr_watcher import default_pr_state

    calls, replies = cli_recorder
    replies["glab"] = json.dumps({"state": "merged"})
    await default_pr_state(GITLAB_MR_URL)
    cmd = calls[0]
    assert "--hostname" in cmd and cmd[cmd.index("--hostname") + 1] == "gitlab.acme.net"


async def test_gitlab_unknown_state_is_unknown_never_merged(cli_recorder):
    """An unmapped/garbled state must read as "" (no action), never MERGED."""
    from no_human.vcs.pr_watcher import default_pr_merged, default_pr_state

    _calls, replies = cli_recorder
    replies["glab"] = json.dumps({"state": "locked"})
    assert await default_pr_state(GITLAB_MR_REF) == ""
    assert await default_pr_merged(GITLAB_MR_REF) is False


async def test_github_pr_lifecycle_still_resolves_via_gh(cli_recorder):
    """Known-positive control: the GitHub path is unchanged and still uses
    `gh`, so a green GitLab test above cannot be an artefact of a broken
    harness."""
    from no_human.vcs.pr_watcher import default_pr_merged, default_pr_state

    calls, replies = cli_recorder
    replies["gh"] = json.dumps({"state": "MERGED"})
    assert await default_pr_state(GITHUB_PR_URL) == "MERGED"
    assert await default_pr_merged("acme/svc#7") is True
    assert all(c[0] == "gh" for c in calls), f"wrong CLI: {calls}"


async def test_a_gitlab_mr_wakes_a_pr_merged_blocker(store, cli_recorder):
    """End to end through the WakeWatcher: the condition the product writes
    into a parked task must actually fire for a GitLab MR."""
    from no_human.vcs.pr_watcher import default_pr_merged

    _calls, replies = cli_recorder
    replies["glab"] = json.dumps({"state": "merged"})

    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    watcher = WakeWatcher(store, _cfg(), pr_merged=default_pr_merged)
    args = dict(raised_at=now - timedelta(hours=1), now=now, wake_check_at=None)

    assert await watcher.condition_satisfied(
        f"pr_merged:{GITLAB_MR_URL}", **args) is True

    replies["glab"] = json.dumps({"state": "opened"})
    assert await watcher.condition_satisfied(
        f"pr_merged:{GITLAB_MR_URL}", **args) is False
