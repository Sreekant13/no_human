"""PreToolUse safety guard policy (PLAN.md Part 10)."""

from no_human.agent import guard

FORBIDDEN = [".env", "secrets/", "*.key", "*.pem"]
PROTECTED = ["main", "master", "release/*"]


def _ev(tool, inp):
    return guard.evaluate(tool, inp, forbidden_paths=FORBIDDEN, never_push_to=PROTECTED)


def test_allows_normal_edit():
    assert _ev("Edit", {"file_path": "src/app.py"}).allow


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
