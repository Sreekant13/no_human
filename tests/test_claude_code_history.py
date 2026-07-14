"""Tests for multi-root Claude Code history extraction (north-star A1).

Covers: reading BOTH config roots (~/.claude + ~/.claude-personal), original
token-usage/duration/cwd/branch extraction from the session JSONL, the
corrections count (the babysitting signal), and the exclusion of no_human's
own machine-generated sessions (eval/shadow/pytest/worktrees/temp dirs) so the
benchmark corpus only ever contains the operator's real conversations.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from no_human.history.claude_code import extract_claude_code_transcripts


def _line(
    kind: str,
    text: str,
    *,
    ts: str,
    usage: dict | None = None,
    sidechain: bool = False,
    meta: bool = False,
    cwd: str = "/Users/op/git/repo",
    branch: str = "master",
) -> str:
    o = {
        "type": kind,
        "timestamp": ts,
        "cwd": cwd,
        "gitBranch": branch,
        "message": {"role": kind, "content": [{"type": "text", "text": text}]},
    }
    if usage is not None:
        o["message"]["usage"] = usage
    if sidechain:
        o["isSidechain"] = True
    if meta:
        o["isMeta"] = True
    return json.dumps(o)


def _write_session(root: Path, project: str, name: str, lines: list[str]) -> Path:
    proj = root / project
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{name}.jsonl"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """Two config roots shaped like the real ones: <home>/.claude/projects and
    <home>/.claude-personal/projects — labels derive from the parent dir name."""
    ent = tmp_path / ".claude" / "projects"
    per = tmp_path / ".claude-personal" / "projects"
    ent.mkdir(parents=True)
    per.mkdir(parents=True)
    return ent, per


USAGE_1 = {"input_tokens": 100, "output_tokens": 50,
           "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200}
USAGE_2 = {"input_tokens": 10, "output_tokens": 5,
           "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}


def _rich_session_lines() -> list[str]:
    return [
        # Slash-command stdout is plumbing — must never become the title,
        # the request, or a correction (live bug found on 2026-07-14).
        _line("user", "<local-command-stdout>Set model to Fable 5"
              "</local-command-stdout>", ts="2026-07-01T09:59:59.000Z"),
        _line("user", "Fix the flaky login test", ts="2026-07-01T10:00:00.000Z"),
        _line("assistant", "Looking at it now.", ts="2026-07-01T10:00:30.000Z",
              usage=USAGE_1),
        _line("user", "No — you broke the fixture, revert that part",
              ts="2026-07-01T10:05:00.000Z"),
        _line("assistant", "Reverted; the real cause was the clock mock.",
              ts="2026-07-01T10:06:00.000Z", usage=USAGE_2),
        # Sidechain + meta lines must count for nothing (not messages, not usage).
        _line("assistant", "subagent chatter", ts="2026-07-01T10:07:00.000Z",
              usage=USAGE_1, sidechain=True),
        _line("user", "injected", ts="2026-07-01T10:07:30.000Z", meta=True),
        _line("user", "ok merge-ready, thanks", ts="2026-07-01T10:10:00.000Z"),
    ]


def test_reads_both_roots_with_source_labels(roots):
    ent, per = roots
    _write_session(ent, "-Users-op-git-repo", "e1", _rich_session_lines())
    _write_session(per, "-Users-op-git-side", "p1", _rich_session_lines())

    out = extract_claude_code_transcripts(days=36500, roots=[ent, per])

    assert len(out) == 2
    by_source = {t.source for t in out}
    assert by_source == {"cc:enterprise", "cc:personal"}


def test_usage_duration_cwd_branch_and_corrections(roots):
    ent, _ = roots
    _write_session(ent, "-Users-op-git-repo", "e1", _rich_session_lines())

    (t,) = extract_claude_code_transcripts(days=36500, roots=[ent])

    # Usage: ONLY non-sidechain assistant lines (USAGE_1 + USAGE_2).
    assert t.usage == {"input_tokens": 110, "output_tokens": 55,
                       "cache_read_input_tokens": 1100,
                       "cache_creation_input_tokens": 220}
    # Wall clock spans first→last real message timestamp.
    assert t.started == "2026-07-01T10:00:00.000Z"
    assert t.ended == "2026-07-01T10:10:00.000Z"
    assert t.cwd == "/Users/op/git/repo"
    assert t.git_branch == "master"
    # Corrections = non-noise user messages AFTER the first (2 here).
    assert t.corrections == 2
    assert t.title.startswith("Fix the flaky login test")


def test_excludes_no_human_machine_sessions(roots):
    ent, _ = roots
    for project in (
        "-private-var-folders-kc-4f5s3w29-T-nh-eval-w1s7-add-mul-work",
        "-private-var-folders-kc-4f5s3w29-T-nh-shadow-sh2a-clone",
        "-private-var-folders-kc-4f5s3w29-T-pytest-of-op-pytest-0-work",
        "-private-tmp-claude-501-scratchpad-test-repo",
        "-Users-op--no-human-worktrees-0d00bf0a",
    ):
        _write_session(ent, project, "s", _rich_session_lines())
    # A real conversation survives.
    _write_session(ent, "-Users-op-git-repo", "real", _rich_session_lines())

    out = extract_claude_code_transcripts(days=36500, roots=[ent])

    assert [t.cascade_id for t in out] == ["cc:real"]


def test_min_user_msgs_parameter(roots):
    ent, _ = roots
    one_shot = [
        _line("user", "Write a design doc for the cache layer",
              ts="2026-07-02T09:00:00.000Z"),
        _line("assistant", "Here is the doc.", ts="2026-07-02T09:01:00.000Z",
              usage=USAGE_2),
    ]
    _write_session(ent, "-Users-op-git-repo", "one", one_shot)

    # Default (learning pipeline) keeps requiring >=2 user messages.
    assert extract_claude_code_transcripts(days=36500, roots=[ent]) == []
    # The bench corpus can opt in to one-shot sessions.
    out = extract_claude_code_transcripts(days=36500, roots=[ent],
                                          min_user_msgs=1)
    assert len(out) == 1
    assert out[0].corrections == 0


def test_missing_root_is_skipped_not_fatal(roots, tmp_path):
    ent, _ = roots
    _write_session(ent, "-Users-op-git-repo", "e1", _rich_session_lines())
    ghost = tmp_path / "nope" / "projects"

    out = extract_claude_code_transcripts(days=36500, roots=[ent, ghost])

    assert len(out) == 1


def test_mined_rules_scoped_by_cwd_fallback(roots):
    """CC transcripts have no workspaces; the analyzer must scope mined rules
    to the session cwd, or a personal-repo rule goes global (learning-queue
    flood). Windsurf workspaces still win when present."""
    from no_human.history.analyzer import analyze_transcript
    from no_human.history.extractor import Transcript, Message

    ent, _ = roots
    _write_session(ent, "-Users-op-git-repo", "e1", _rich_session_lines())
    (t,) = extract_claude_code_transcripts(days=36500, roots=[ent])
    t.messages.append(Message(role="user",
                              content="never commit the lockfile", step_type="user"))

    findings = analyze_transcript(t)

    assert findings, "the 'never commit' rule should be mined"
    assert all(f.project == "/Users/op/git/repo" for f in findings)

    ws = Transcript(cascade_id="w1", title="x", created="2026-07-01",
                    workspaces=["file:///Users/op/git/ws-repo/"],
                    cwd="/Users/op/elsewhere",
                    messages=[Message(role="user",
                                      content="never commit the lockfile",
                                      step_type="user")])
    ws_findings = analyze_transcript(ws)
    assert ws_findings and ws_findings[0].project == "/Users/op/git/ws-repo"


def test_cli_json_out_is_pure_json(roots, monkeypatch):
    """`nh history --json-out` stdout must be pipeable JSON — status lines
    (incl. the windsurf-skipped notice) must not pollute it."""
    from click.testing import CliRunner
    from no_human.cli.commands import cli
    from no_human.history import extractor

    ent, _ = roots
    _write_session(ent, "-Users-op-git-repo", "e1", _rich_session_lines())

    def _no_ide(**_kw):
        raise extractor.IDENotRunningError("no IDE")
    monkeypatch.setattr("no_human.history.extractor.extract_transcripts", _no_ide)

    result = CliRunner().invoke(
        cli, ["history", "--days", "36500", "--json-out",
              "--roots", str(ent)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)  # raises if polluted
    assert len(data) == 1 and data[0]["source"] == "cc:enterprise"


def test_windsurf_transcript_defaults_stay_compatible():
    """New Transcript fields must default safely for the Windsurf path."""
    from no_human.history.extractor import Transcript

    t = Transcript(cascade_id="w1", title="x", created="2026-07-01")
    assert t.usage == {}
    assert t.source == ""
    assert t.corrections == 0
    assert t.started == "" and t.ended == ""
    assert t.cwd == "" and t.git_branch == ""
