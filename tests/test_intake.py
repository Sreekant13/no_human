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
