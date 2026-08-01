"""Intake: source detection + pure normalizers (GitHub/GitLab, verified on fixtures)."""

import pytest

from no_human.intake import parse_source
from no_human.intake.base import clean_html, dv, html_list_items
from no_human.intake.github_issues import GitHubAdapter, parse_issue_url
from no_human.intake.gitlab_issues import GitLabAdapter


@pytest.mark.parametrize("text,kind", [
    ("https://code.example.com/org/repo/issues/12", "github"),
    ("https://github.com/o/r/issues/9", "github"),
    ("https://gitlab.acme.net/g/s/p/-/issues/3", "gitlab"),
    ("just do the thing", "freeform"),
])
def test_parse_source(text, kind):
    assert parse_source(text).kind == kind


def test_html_helpers():
    assert clean_html("<p>a &amp; b</p>") == "a & b"
    items = html_list_items("<ol><li>one</li><li>two &mdash; 2</li></ol>")
    assert items == ["one", "two — 2"]
    # plain numbered text fallback
    assert html_list_items("1. first\n2. second") == ["first", "second"]
    assert dv({"value": "2", "display_value": "Work in progress"}) == "Work in progress"
    assert dv("plain") == "plain"
    assert dv(None) == ""


def test_github_normalize():
    raw = {
        "number": 12, "title": "Fix the parser",
        "body": "Background.\n\n- [ ] handles empty input\n- [x] covers unicode\n",
        "html_url": "https://code.example.com/o/r/issues/12",
        "labels": [{"name": "bug"}], "state": "open",
        "_ref": {"host": "code.example.com", "owner": "o", "repo": "r"},
    }
    task = GitHubAdapter().normalize(raw)
    assert task.source == "github"
    assert task.external_id == "o/r#12"
    assert task.acceptance_criteria == ["handles empty input", "covers unicode"]
    assert task.context["github"]["labels"] == ["bug"]


def test_github_url_parse():
    ref = parse_issue_url("https://code.example.com/myorg/myrepo/issues/99")
    assert (ref.host, ref.owner, ref.repo, ref.number) == (
        "code.example.com", "myorg", "myrepo", "99")


def test_gitlab_normalize():
    raw = {
        "iid": 3, "title": "Add retry", "description": "do it\n- [ ] retries 3x\n",
        "web_url": "https://gitlab.acme.net/g/s/p/-/issues/3",
        "labels": ["enhancement"], "state": "opened",
        "_ref": {"host": "gitlab.acme.net", "project_path": "g/s/p"},
    }
    task = GitLabAdapter().normalize(raw)
    assert task.source == "gitlab"
    assert task.external_id == "g/s/p#3"
    assert task.acceptance_criteria == ["retries 3x"]


def test_a_bare_tracker_key_is_rejected_not_ingested_as_freeform():
    """docs/quickstart.md said `PROJ-42` "is ingested as freeform text". It is not.

    `parse_source` classifies it as freeform, and `ingest_from_url` raises on
    exactly that classification — so `nh task add PROJ-42` prints
    "intake failed:" and exits 1. A quickstart is the first thing a new user
    reads, and this told them a command would work that always fails.

    Both halves are asserted, because the doc's error was believing the first
    one implied the second.
    """
    from no_human.intake import ingest_from_url

    assert parse_source("PROJ-42").kind == "freeform"
    with pytest.raises(ValueError, match="not a recognized task URL/id"):
        ingest_from_url("PROJ-42")


def test_quickstart_does_not_promise_freeform_ingestion_of_a_tracker_key():
    """The doc half of the coupling above.

    Anchored to the behaviour, not to phrasing: the guard reads the sentence
    only to confirm it has not gone back to promising ingestion.
    """
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "quickstart.md").read_text(
        encoding="utf-8")
    assert "PROJ-42" in doc, (
        "quickstart no longer discusses a bare tracker key — if the section was "
        "removed, remove this guard deliberately rather than letting it pass "
        "over a subject that is gone"
    )
    assert "ingested as freeform text" not in doc, (
        "quickstart claims a bare tracker key is ingested as freeform text; "
        "ingest_from_url raises and the CLI exits 1"
    )
