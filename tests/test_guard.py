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


def test_blocks_merging_a_pull_request():
    """The agent never merges (§3.2). Until 2026-07-10 only `git merge` was
    blocked — which is not how a PR gets merged. `gh pr merge` sailed through,
    and PR #531 was opened by an agent with nothing stopping it merging."""
    assert not _ev("Bash", {"command": "gh pr merge 531 --squash"}).allow
    assert not _ev("Bash", {"command": "glab mr merge 12"}).allow
    assert not _ev(
        "Bash", {"command": "gh api -X PUT repos/o/r/pulls/531/merge"}
    ).allow


def test_allows_the_agent_to_merge_into_its_own_branch():
    """User, 2026-07-10: the agent may commit, push its own branch, merge a ref
    into it, and open a PR. Only merging the PR is forbidden. A local merge can
    never reach a protected branch, because the push is denied."""
    assert _ev("Bash", {"command": "git merge origin/dev"}).allow
    assert _ev("Bash", {"command": "git commit -m 'fix'"}).allow
    assert _ev("Bash", {"command": "git push -u origin scratch/dev/x"}).allow
    assert _ev("Bash", {"command": "gh pr create --base dev --head scratch/x"}).allow


def test_the_pr_base_branch_must_be_pushable_by_nobody():
    """`never_push_to` lists main/master/release/* — but metrics-core's base is `dev`,
    so `git push origin HEAD:dev` merged without review. The orchestrator adds
    the task's base to the protected list per attempt."""
    d = guard.evaluate(
        "Bash", {"command": "git push origin HEAD:dev"},
        forbidden_paths=FORBIDDEN, never_push_to=[*PROTECTED, "dev"],
    )
    assert not d.allow
    assert "merging without review" in d.reason


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
    # A read-only session explores and reports; it never writes. Relaxing the
    # blanket `git merge` ban for the coder must not relax it for the reviewer.
    assert not _ro("Bash", {"command": "git merge origin/main"}).allow
    assert not _ro("Bash", {"command": "git commit -m x"}).allow
    assert not _ro("Bash", {"command": "git push origin x"}).allow
    assert not _ro("Bash", {"command": "gh pr create --base dev"}).allow
    # ...but reading the repo is the whole point of the session
    assert _ro("Bash", {"command": "git log --oneline -5"}).allow
    assert _ro("Bash", {"command": "git diff HEAD~1"}).allow


# --------------------------------------------------------------------------- #
# The hook the SDK actually calls (not just the pure policy underneath)        #
# --------------------------------------------------------------------------- #

async def test_pretooluse_hook_denies_ask_user_question():
    """guard.evaluate is pure, but what the Agent SDK invokes is the hook that
    wraps it. Assert the wire format the SDK acts on, not just the policy."""
    from no_human.agent.claude_backend import _make_guard_hook

    hook = _make_guard_hook(FORBIDDEN, PROTECTED)
    out = await hook({"tool_name": "AskUserQuestion", "tool_input": {}}, None, None)

    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "BLOCKER_JSON_START" in hso["permissionDecisionReason"]


async def test_pretooluse_hook_allows_a_normal_tool():
    from no_human.agent.claude_backend import _make_guard_hook

    hook = _make_guard_hook(FORBIDDEN, PROTECTED)
    out = await hook({"tool_name": "Read", "tool_input": {"file_path": "a.py"}}, None, None)
    assert out == {}, "an allowed tool must return an empty hook result"


# --------------------------------------------------------------------------- #
# A read-only planner must not busy-wait on its own subagents                  #
# --------------------------------------------------------------------------- #

def _ro(tool, inp=None):
    return guard.evaluate(tool, inp or {}, forbidden_paths=FORBIDDEN,
                          never_push_to=PROTECTED, readonly=True)


def test_readonly_session_cannot_poll_for_its_subagents():
    """Run 087e2d3a: the test-first proposer used ToolSearch to find Monitor and
    TaskStop, then spawned five subagents whose only job was to wait for two
    other subagents — 100 events against minimal-first's 22, for the same one
    plan draft."""
    for tool in ("Monitor", "TaskStop", "ToolSearch"):
        d = _ro(tool)
        assert not d.allow, f"{tool} must be denied in a read-only session"
        assert "do not poll" in d.reason


def test_readonly_session_keeps_the_tools_it_actually_needs():
    """The Agent tool returns its result directly — risk-first spawned three
    subagents with zero Monitor calls. Denying it would break real research."""
    for tool in ("Read", "Grep", "Glob", "Agent"):
        assert _ro(tool, {"file_path": "Jenkinsfile"}).allow, f"{tool} must stay"
    assert _ro("Bash", {"command": "git log --oneline -5"}).allow


def test_the_coder_may_still_use_background_tools():
    """Only read-only sessions are restricted; the implementer is not."""
    for tool in ("Monitor", "TaskStop", "ToolSearch"):
        assert _ev(tool, {}).allow, f"{tool} must remain available to the coder"
