"""PreToolUse safety guard policy (PLAN.md Part 10)."""

from no_human.agent import guard

FORBIDDEN = [".env", "secrets/", "*.key", "*.pem"]
PROTECTED = ["main", "master", "release/*"]


def _ev(tool, inp):
    return guard.evaluate(tool, inp, forbidden_paths=FORBIDDEN, never_push_to=PROTECTED)


def test_allows_normal_edit():
    assert _ev("Edit", {"file_path": "src/app.py"}).allow


def test_blocks_ask_user_question():
    # The exact call a planner proposer made in run 0305e5ce, after which it
    # received nothing and wrote "No answer given — I'll default to ...".
    d = _ev("AskUserQuestion", {"questions": [{
        "question": "The spec says to verify kubectl cluster access on the Jenkins "
                    "agent before choosing the cleanup mechanism, but that can only "
                    "be confirmed at runtime. How should I proceed?",
        "header": "Cleanup path",
        "multiSelect": False,
        "options": [{"label": "kubectl first, GitLab fallback", "description": "..."}],
    }]})
    assert not d.allow
    # The deny reason must redirect, or the agent just retries the tool.
    assert "BLOCKER_JSON_START" in d.reason
    assert "do not silently guess" in d.reason.lower()


def test_blocks_ask_user_question_in_readonly_reviewer_too():
    d = guard.evaluate("AskUserQuestion", {}, forbidden_paths=FORBIDDEN,
                       never_push_to=PROTECTED, readonly=True)
    assert not d.allow


def test_blocks_write_to_env():
    assert not _ev("Write", {"file_path": ".env"}).allow
    assert not _ev("Write", {"file_path": "config/secrets/db.key"}).allow
    assert not _ev("Edit", {"file_path": "certs/server.pem"}).allow


def test_blocks_rm_rf():
    assert not _ev("Bash", {"command": "rm -rf /tmp/x"}).allow
    assert not _ev("Bash", {"command": "rm -fr build"}).allow
    assert _ev("Bash", {"command": "rm file.txt"}).allow  # non-recursive ok


def test_blocks_git_merge():
    assert not _ev("Bash", {"command": "git merge origin/main"}).allow


def test_blocks_push_to_protected():
    assert not _ev("Bash", {"command": "git push origin main"}).allow
    assert not _ev("Bash", {"command": "git push origin HEAD:master"}).allow
    assert not _ev("Bash", {"command": "git push origin release/1.2"}).allow


def test_allows_push_to_feature_branch():
    assert _ev("Bash", {"command": "git push -u origin no-human/abc123"}).allow


def test_blocks_force_push():
    assert not _ev("Bash", {"command": "git push --force origin no-human/x"}).allow


def test_blocks_hard_reset_to_ref():
    assert not _ev("Bash", {"command": "git reset --hard HEAD~3"}).allow


def test_readonly_blocks_all_write_tools():
    def _ro(tool, inp):
        return guard.evaluate(tool, inp, forbidden_paths=FORBIDDEN, never_push_to=PROTECTED,
                              readonly=True)
    # All write tools blocked in readonly mode, regardless of path.
    for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        d = _ro(tool, {"file_path": "src/totally_fine.py"})
        assert not d.allow, f"{tool} should be blocked in readonly mode"
    # Bash / Read are still allowed (read-only operations).
    assert _ro("Bash", {"command": "pytest -q"}).allow
    assert _ro("Read", {"file_path": "README.md"}).allow


def test_readonly_still_blocks_destructive_bash():
    def _ro(tool, inp):
        return guard.evaluate(tool, inp, forbidden_paths=FORBIDDEN, never_push_to=PROTECTED,
                              readonly=True)
    assert not _ro("Bash", {"command": "rm -rf ."}).allow
    assert not _ro("Bash", {"command": "git merge origin/main"}).allow
