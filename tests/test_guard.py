"""PreToolUse safety guard policy (PLAN.md Part 10)."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

from no_human.agent import guard

FORBIDDEN = [".env", "secrets/", "*.key", "*.pem"]
PROTECTED = ["main", "master", "release/*"]

#: An empty stand-in for the session's worktree. In production the backend
#: always passes the worktree it runs commands in; `_ev` mirrors that. Tests
#: about the no-cwd conservative path call `guard.evaluate` directly.
_WT = tempfile.mkdtemp(prefix="guard-session-wt-")


def _ev(tool, inp):
    return guard.evaluate(tool, inp, forbidden_paths=FORBIDDEN,
                          never_push_to=PROTECTED, cwd=_WT)


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
    and PR #7004 was opened by an agent with nothing stopping it merging."""
    assert not _ev("Bash", {"command": "gh pr merge 7004 --squash"}).allow
    assert not _ev("Bash", {"command": "glab mr merge 12"}).allow
    assert not _ev(
        "Bash", {"command": "gh api -X PUT repos/o/r/pulls/7004/merge"}
    ).allow


def test_allows_the_agent_to_merge_into_its_own_branch():
    """User, 2026-07-10: the agent may commit, push its own branch, merge a ref
    into it, and open a PR. Only merging the PR is forbidden. A local merge can
    never reach a protected branch, because the push is denied."""
    assert _ev("Bash", {"command": "git merge origin/dev"}).allow
    assert _ev("Bash", {"command": "git commit -m 'fix'"}).allow
    assert _ev("Bash", {"command": "git push -u origin scratch/dev/x"}).allow
    assert _ev("Bash", {"command": "gh pr create --base dev --head scratch/x"}).allow


def test_blocks_nh_merge_stack_run_in_every_mode():
    """G6, proven live 2026-08-08: `nh merge-stack run --yes` reached
    `gh pr merge` (shelled at cli/commands.py:1793) with the guard returning
    allow=True in BOTH modes — the product's own CLI spelling of the forge
    merge was unmatched while every direct spelling was denied. The agent
    never merges (§2), and that includes merging through our own wrapper."""
    for readonly in (False, True):
        for cmd in (
            "nh merge-stack run --yes",
            "nh merge-stack run --squash --yes",
            "cd /repo && nh merge-stack run --yes",
            ".venv/bin/nh merge-stack run --yes",
            "uv run nh merge-stack run --yes",
            'sh -c "nh merge-stack run --yes"',
        ):
            d = guard.evaluate("Bash", {"command": cmd},
                               forbidden_paths=FORBIDDEN,
                               never_push_to=PROTECTED, readonly=readonly)
            assert not d.allow, f"readonly={readonly} must deny: {cmd}"
            assert "merge" in d.reason.lower(), d.reason


def test_forge_merge_family_denied_in_both_modes():
    """Guards get tested in one direction only (documented failure class) —
    so pin the merge family in coder mode AND readonly mode explicitly,
    including the refspec spellings that merge by pushing at the base."""
    for readonly in (False, True):
        for cmd in (
            "gh pr merge 7004 --squash",
            "glab mr merge 12",
            "gh api -X PUT repos/o/r/pulls/7004/merge",
            "glab api --method PUT projects/1/merge_requests/12/merge",
            "git push origin :main",
            "git push origin HEAD:refs/heads/main",
            "git push origin HEAD:refs/heads/release/1.2",
        ):
            d = guard.evaluate("Bash", {"command": cmd},
                               forbidden_paths=FORBIDDEN,
                               never_push_to=PROTECTED, readonly=readonly)
            assert not d.allow, f"readonly={readonly} must deny: {cmd}"


def test_merge_stack_denial_does_not_overcorrect():
    """The other direction: ordinary agent git/gh workflow — including the
    WORD "merge" in a PR title or comment (a guard once denied an agent
    titling its own PR; that class must not come back) — stays allowed.
    Only `nh merge-stack run` merges; `plan` and `link` read/record order."""
    for cmd in (
        'gh pr create --title "Merge the stack guard fix" --base dev',
        'gh pr comment 7 --body "ready to merge after review"',
        "gh pr view 7 --comments",
        'git commit -m "merge base into my branch"',
        "git merge origin/dev",
        "git push -u origin no-human/abc123",
        "nh merge-stack plan",
        "nh merge-stack link https://x/pr/1 https://x/pr/2",
        "nh --help",
    ):
        d = _ev("Bash", {"command": cmd})
        assert d.allow, f"must stay allowed: {cmd} -> {d.reason}"


def test_the_pr_base_branch_must_be_pushable_by_nobody():
    """`never_push_to` lists main/master/release/* — but a real task's base was
    `dev`, so `git push origin HEAD:dev` merged without review. The orchestrator
    adds the task's base to the protected list per attempt."""
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
    """Read/Grep/Glob/Bash stay available — a readonly session explores and
    reports without spawning anything."""
    for tool in ("Read", "Grep", "Glob"):
        assert _ro(tool, {"file_path": "Jenkinsfile"}).allow, f"{tool} must stay"
    assert _ro("Bash", {"command": "git log --oneline -5"}).allow


def test_the_coder_may_still_use_background_tools():
    """Only read-only sessions are restricted; the implementer is not."""
    for tool in ("Monitor", "TaskStop", "ToolSearch"):
        assert _ev(tool, {}).allow, f"{tool} must remain available to the coder"


def test_readonly_session_cannot_spawn_subagents():
    """A readonly reviewer/researcher session must not spawn subagents: the
    spawned session gets its OWN toolset, not the parent's readonly gate — a
    capability-laundering channel out of readonly (sibling hole fixed in
    a21f124a7). `Workflow` explicitly orchestrates subagents; `CronCreate` and
    `RemoteTrigger` launch async work the same way."""
    for tool in ("Task", "Agent", "Workflow", "CronCreate", "RemoteTrigger"):
        d = _ro(tool)
        assert not d.allow, f"{tool} must be denied in a read-only session"
        assert "read-only" in d.reason.lower()


def test_coder_session_may_still_spawn_subagents():
    """Only read-only sessions are restricted; the implementer is not."""
    for tool in ("Task", "Agent"):
        assert _ev(tool, {}).allow, f"{tool} must remain available to the coder"


# ---------------------- D2 #2: tool-time tamper guards --------------------- #
# The tamper gate fires at attempt END — after 1-3M tokens are spent. These
# deterministic denials convert that whole wasted attempt into a 1-turn
# correction the coder sees immediately.

def test_test_file_deletion_denied_at_tool_time():
    for cmd in ("rm tests/test_core.py",
                "git rm -f tests/test_api.py",
                "rm -f web/src/laneView.test.mjs"):
        d = guard.evaluate("Bash", {"command": cmd},
                     forbidden_paths=[], never_push_to=[])
        assert not d.allow, cmd
        assert "test" in d.reason.lower()

def test_legitimate_moves_and_non_test_deletes_still_allowed():
    for cmd in ("git mv tests/test_old.py tests/test_new.py",
                "rm build/artifact.bin",
                "rm notes.md"):
        d = guard.evaluate("Bash", {"command": cmd},
                     forbidden_paths=[], never_push_to=[])
        assert d.allow, cmd

def test_no_human_yml_writes_denied():
    d = guard.evaluate("Write", {"file_path": ".no_human.yml"},
                 forbidden_paths=[], never_push_to=[])
    assert not d.allow
    d2 = guard.evaluate("Edit", {"file_path": "/repo/.no_human.yml"},
                  forbidden_paths=[], never_push_to=[])
    assert not d2.allow
    d3 = guard.evaluate("Bash", {"command": "sed -i '' 's/a/b/' .no_human.yml"},
                  forbidden_paths=[], never_push_to=[])
    assert not d3.allow

def test_reading_no_human_yml_still_allowed():
    d = guard.evaluate("Read", {"file_path": ".no_human.yml"},
                 forbidden_paths=[], never_push_to=[])
    assert d.allow


# ------------------ Phase C: the re-read whale (57.8k/turn) ---------------- #

def test_unbounded_read_of_a_huge_file_is_redirected_not_denied(tmp_path):
    big = tmp_path / "huge.py"
    big.write_text("x = 1\n" * 5000)
    d = guard.evaluate("Read", {"file_path": str(big)},
                       forbidden_paths=[], never_push_to=[])
    assert not d.allow
    # It must TEACH the cheaper move, not just refuse.
    assert "offset/limit" in d.reason and "Grep" in d.reason
    assert "5000 lines" in d.reason


def test_scoped_read_of_a_huge_file_is_allowed(tmp_path):
    big = tmp_path / "huge.py"
    big.write_text("x = 1\n" * 5000)
    d = guard.evaluate("Read", {"file_path": str(big), "limit": 200, "offset": 100},
                       forbidden_paths=[], never_push_to=[])
    assert d.allow


def test_whole_file_read_of_a_normal_file_is_untouched(tmp_path):
    ok = tmp_path / "small.py"
    ok.write_text("x = 1\n" * 300)
    d = guard.evaluate("Read", {"file_path": str(ok)},
                       forbidden_paths=[], never_push_to=[])
    assert d.allow


def test_unstattable_path_never_blocks_a_read(tmp_path):
    """The guard must never fail a call because it could not measure a file."""
    d = guard.evaluate("Read", {"file_path": str(tmp_path / "nope.py")},
                       forbidden_paths=[], never_push_to=[])
    assert d.allow


def test_read_redirect_does_not_fire_for_readonly_sessions(tmp_path):
    """Review #6: reviewer/researcher are one-shot — no re-read loop, so the
    read redirect must not degrade them."""
    big = tmp_path / "huge.py"
    big.write_text("x = 1\n" * 5000)
    d = guard.evaluate("Read", {"file_path": str(big)},
                       forbidden_paths=[], never_push_to=[], readonly=True)
    assert d.allow


def test_read_redirect_exempts_data_files(tmp_path):
    """Review #6: a big JSON/lockfile needs whole-file context and is rarely
    re-read many times."""
    for name in ("data.json", "pnpm-lock.yaml", "schema.sql"):
        f = tmp_path / name
        f.write_text("x\n" * 5000)
        d = guard.evaluate("Read", {"file_path": str(f)},
                           forbidden_paths=[], never_push_to=[])
        assert d.allow, name


def test_rm_of_build_artifacts_is_not_blocked_as_test_deletion(tmp_path):
    """Review #7: .test.js.map and coverage xml are build output, not source."""
    for cmd in ("rm dist/app.test.js.map",
                "rm coverage/test_results.xml",
                "rm build/report.test.js.map"):
        d = guard.evaluate("Bash", {"command": cmd},
                           forbidden_paths=[], never_push_to=[])
        assert d.allow, cmd


def test_rm_of_real_source_test_still_blocked(tmp_path):
    d = guard.evaluate("Bash", {"command": "rm tests/test_core.py"},
                       forbidden_paths=[], never_push_to=[])
    assert not d.allow


def test_live_product_server_is_blocked_in_agent_sessions():
    """Live incident (2026-07-24): a coder task verifying `nh serve` flags
    LAUNCHED a real `nh serve` from its worktree — which shares the operator's
    ~/.no_human config/DB/credentials, so its Jira poller mass-imported 16
    duplicate tasks into the production board. A live product server (or a
    task-running `nh watch`, or a real `nh bench run`) is never a legitimate
    coder-session tool — CLI behavior is tested through the CliRunner suite.
    """
    for cmd in (
        "nh serve",
        "nh serve --max-workers 3",
        ".venv/bin/nh serve",
        "/Users/x/.no_human/worktrees/abc/.venv/bin/nh start --no-open",
        "nh watch abc123",
        "nh bench run --quick",
        "cd /tmp && nh serve &",
    ):
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=[], never_push_to=[])
        assert not d.allow, f"should be blocked: {cmd}"
        assert "server" in d.reason.lower() or "live" in d.reason.lower(), d.reason

    # Reads and unrelated nh subcommands stay allowed.
    for cmd in ("nh --help", "echo nh serve is a command we do not run"):
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=[], never_push_to=[])
        # the echo contains the phrase but as argv of echo it still matches the
        # regex — acceptable? No: a pure echo must stay allowed only if the
        # pattern requires nh at a command position. Assert help stays allowed.
        if cmd == "nh --help":
            assert d.allow, d.reason


# --------------------------------------------------------------------------- #
# Destructive WORKING-TREE git, for the CODER (not just readonly sessions).
#
# Observed 2026-08-06, benchmark spec ns-1746bea3: the coder — told "Do NOT run
# any git command" — ran `git stash` then `git stash pop`, popped a PRE-EXISTING
# unrelated stash, and left 1041 lines of a foreign Jira Forge app in the
# working tree. Its `git reset -- <paths>` only unstaged them. The judge failed
# the spec; the guard never fired, because `_GIT_WRITE` is applied only when
# `readonly` is set and the coder is not readonly.
#
# `security/round6` regressed this same guard by testing ONE direction: a rule
# added to kill false positives turned a total rule into a positional one and 8
# previously-denied commands began to allow. So every case below is stated as a
# PAIR — the destructive form must be denied AND the legitimate form of the same
# subcommand must still run, since a guard that breaks `git commit` breaks the
# product.
# --------------------------------------------------------------------------- #

#: (destructive form, legitimate form of the SAME subcommand)
_WORKTREE_PAIRS = [
    # the incident itself: every spelling of stash, none of which the coder needs
    ("git stash", "git status"),
    ("git stash pop", "git status --porcelain"),
    ("git stash push -u", "git diff"),
    ("git stash apply", "git diff --stat"),
    ("git stash drop", "git log --oneline -5"),
    ("git stash save wip", "git show HEAD"),
    # reset: --hard/--merge/--keep write the tree, --soft/--mixed do not.
    # NOTE `git reset --hard` with no argument escaped the pre-existing
    # `_GIT_DESTRUCTIVE` regex, which requires a `\S` after `--hard`.
    ("git reset --hard", "git reset"),
    ("git reset --hard HEAD~3", "git reset --soft HEAD~1"),
    ("git reset --merge", "git reset --mixed"),
    ("git reset --keep origin/main", "git reset HEAD -- src/index.js"),
    # checkout: the pathspec/force forms vs the branch forms
    ("git checkout -- src/index.js", "git checkout -b feature/x"),
    ("git checkout .", "git checkout feature/x"),
    ("git checkout HEAD -- tests/", "git checkout -b x origin/main"),
    ("git checkout main src/label.js", "git checkout main"),
    ("git checkout -f feature/x", "git checkout -"),
    ("git checkout -p", "git checkout --detach"),
    ("git checkout --ours src/a.py", "git checkout --orphan gh-pages"),
    ("git checkout src/index.js", "git checkout --track origin/topic"),
    ("git checkout --pathspec-from-file=list.txt", "git checkout -B x"),
    # switch has no pathspec form; only the discard flags are destructive
    ("git switch --discard-changes", "git switch main"),
    ("git switch -f main", "git switch -c feature/x"),
    # restore is denied in every form (see the module's rule) — `git reset`
    # unstages without touching the tree, so the coder loses nothing
    ("git restore src/index.js", "git reset -- src/index.js"),
    ("git restore --staged --worktree .", "git reset HEAD -- ."),
    ("git restore .", "git status"),
    # clean: only the dry run is not a deletion
    ("git clean -fd", "git clean -n"),
    ("git clean -fdx", "git clean --dry-run"),
    ("git clean -d", "git clean -nd"),
    # worktree: listing is a read, everything else moves trees around
    ("git worktree remove ../wt", "git worktree list"),
    ("git worktree add ../wt main", "git worktree list --porcelain"),
    # apply adds content; reversing a patch discards it
    ("git apply -R fix.patch", "git apply fix.patch"),
]


def test_destructive_worktree_git_blocked_and_legitimate_form_allowed():
    for bad, good in _WORKTREE_PAIRS:
        d = _ev("Bash", {"command": bad})
        assert not d.allow, f"must be blocked: {bad}"
        # Pin the NEW rule, not whichever check happens to fire first: a couple
        # of these (`reset --hard <sha>`, `clean -fd`) are also caught by the
        # older `_GIT_DESTRUCTIVE` regex, and a pair that passes only because of
        # the old rule would prove nothing about this one.
        assert guard._git_worktree_denial(bad, _WT) is not None, f"new rule missed: {bad}"
        assert "working-tree" in guard._git_worktree_denial(bad, _WT)
        g = _ev("Bash", {"command": good})
        assert g.allow, f"must stay allowed: {good} — {g.reason}"
        assert guard._git_worktree_denial(good, _WT) is None, \
            f"new rule over-blocks: {good}"


def test_worktree_rule_is_default_deny_not_a_name_list():
    """The point of the rule: a spelling nobody enumerated is still denied.

    None of these six appear in the incident report or in any denylist here.
    They are refused because they are not on the list of subcommands that
    provably CANNOT clobber the tree — which is the only shape of rule that
    can catch the seventh spelling.
    """
    for cmd in (
        "git checkout-index -f -a",
        "git read-tree -u -m HEAD",
        "git sparse-checkout set src",
        "git submodule update --force",
        "git rerere forget .",
        "git mergetool",
    ):
        d = _ev("Bash", {"command": cmd})
        assert not d.allow, f"must be blocked: {cmd}"
        assert "not one of the subcommands" in d.reason, d.reason


def test_worktree_rule_survives_wrappers_and_prefixes():
    """`_WRAPPERS` and `VAR=value` prefixes must not launder the command."""
    for cmd in (
        "env git stash",
        "X=1 git stash",
        "X=1 env git restore .",
        "sudo git clean -fd",
        # wrapper FLAGS must not launder it either: `env -i` and `sudo -u me`
        # once defeated the wrapper skip, which only advanced over exact words
        "env -i git stash",
        "sudo -n git reset --hard",
        "sudo -u me git stash",
        "env -i PATH=/usr/bin git restore .",
        "nohup git stash pop",
        "command git stash",
        "exec git reset --hard",
        "time git stash",
        "builtin git stash",
        "/usr/bin/git stash",
        "/opt/homebrew/bin/git restore .",
        # git's OWN global options sit before the subcommand
        "git -C /repo stash",
        "git --git-dir=/r/.git stash",
        "git -c user.name=x stash pop",
        # a nested shell, and a runner that takes the argv directly
        "sh -c 'git stash'",
        'bash -lc "git checkout -- ."',
        "xargs git restore",
        "timeout 30 git restore .",
        'eval "git stash"',
        "eval git stash",
        # a subcommand produced by shell expansion is unreadable here, so
        # default-deny refuses it rather than guessing it is harmless
        "git $(echo stash)",
        "git `echo stash`",
    ):
        assert not _ev("Bash", {"command": cmd}).allow, f"must be blocked: {cmd}"


def test_worktree_rule_survives_compound_commands():
    for cmd in (
        "cd /x && git stash",
        "git stash; git pull",
        "false || git reset --hard",
        "git status && git stash",
        "make test | git stash",
        "echo hi\ngit clean -fd",
    ):
        assert not _ev("Bash", {"command": cmd}).allow, f"must be blocked: {cmd}"


def test_worktree_rule_does_not_break_the_coder_or_fire_on_prose():
    """The other direction, at width. Applying `_GIT_WRITE` wholesale to the
    coder would deny every one of these, which is why the narrow rule exists.
    """
    for cmd in (
        "git commit -m 'fix'", "git commit --amend --no-edit",
        "git push -u origin no-human/abc123", "git merge origin/dev",
        "git add -A", "git add src/index.js", "git mv a.py b.py",
        "git branch -a", "git branch feature/x", "git branch -d old",
        "git rebase origin/main", "git cherry-pick abc123",
        "git revert abc123", "git tag v1", "git fetch origin",
        "git pull --rebase", "git am < patch",
        "git blame src/x.py", "git rev-parse HEAD", "git ls-files",
        "git grep TODO", "git describe --tags", "git merge-base main HEAD",
        "git config user.name", "git remote -v", "git reflog",
        "git shortlog -sn", "git for-each-ref refs/heads",
        "git cat-file -p HEAD", "git format-patch -1",
        # the guard reads argv, so git words inside a MESSAGE are just text
        "git commit -m 'never run git stash again'",
        "echo 'do not run git stash'",
        "grep -rn 'git checkout --' docs/",
        # and it must ignore anything that is not git at all
        "pytest -q", "ls -la", "python3 -m pytest tests/test_guard.py",
    ):
        d = _ev("Bash", {"command": cmd})
        assert d.allow, f"must stay allowed: {cmd} — {d.reason}"


def test_worktree_denial_tells_the_agent_what_to_do_instead():
    """A denial with no alternative is retried until the attempt dies."""
    d = _ev("Bash", {"command": "git stash"})
    # the property, stated to the agent in the words it must act on
    assert "overwrite or discard working-tree content you did not create" in d.reason
    assert "Write/Edit" in d.reason
    assert "blocker report" in d.reason


def test_checkout_operand_that_exists_on_disk_is_a_pathspec(tmp_path):
    """The load-bearing half of pathspec detection, and the one a lexical rule
    cannot get right from spelling alone: `git checkout notes` is a branch
    switch if `notes` is a ref and a WIPE if `notes` is a file on disk. An
    operand with no slash and no extension is treated as a ref UNLESS it exists
    — which is exactly the case where being wrong destroys work.

    Existence is resolved against the ``cwd`` the caller passes (the session's
    worktree, in production) — deliberately NO chdir here: the guard runs in
    the orchestrator process, whose own cwd is a different repo entirely.
    """
    (tmp_path / "notes").write_text("someone else's work\n")
    (tmp_path / "vendored").mkdir()

    def ev(cmd):
        return guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                              never_push_to=PROTECTED, cwd=str(tmp_path))

    for cmd in ("git checkout notes", "git checkout vendored"):
        d = ev(cmd)
        assert not d.allow, f"must be blocked, it exists on disk: {cmd}"
        assert "working-tree" in d.reason

    # Same spelling, nothing of that name in the worktree: a ref, and it runs.
    for cmd in ("git checkout release-notes", "git checkout topic"):
        assert ev(cmd).allow, cmd


def test_checkout_existence_resolves_against_worktree_not_orchestrator_cwd(
        tmp_path, monkeypatch):
    """`evaluate` runs in the ORCHESTRATOR process; the command runs in the
    SESSION's worktree (the SDK subprocess cwd). A check against the
    orchestrator's cwd is inert in production — it answers about the wrong
    directory in both directions. Here the two disagree both ways and the
    verdict must follow the worktree every time.
    """
    worktree = tmp_path / "session-wt"
    worktree.mkdir()
    (worktree / "vendored").mkdir()
    orch = tmp_path / "orchestrator-cwd"
    orch.mkdir()
    (orch / "notes").write_text("exists only where the command does NOT run\n")
    monkeypatch.chdir(orch)  # the orchestrator's cwd, which must NOT matter

    def ev(cmd):
        return guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                              never_push_to=PROTECTED, cwd=str(worktree))

    # Exists in the worktree, absent from the orchestrator cwd: a WIPE — deny.
    assert not os.path.exists("vendored")
    d = ev("git checkout vendored")
    assert not d.allow, "worktree file invisible: existence resolved in wrong cwd"
    assert "working-tree" in d.reason

    # Exists only in the orchestrator cwd, absent from the worktree: a ref.
    assert os.path.exists("notes")
    assert ev("git checkout notes").allow, \
        "orchestrator-cwd file leaked into the session's verdict"


def test_bare_checkout_without_a_known_cwd_is_conservatively_denied():
    """No ``cwd``, no existence answer — and a wrong 'it's a ref' answer
    destroys work while a wrong 'it's a path' answer costs one denial. So a
    bare operand is denied, and the forms that are safe by GRAMMAR (branch
    creation, detach) stay allowed without any cwd at all.
    """
    def ev(cmd):
        return guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                              never_push_to=PROTECTED)  # no cwd

    for cmd in ("git checkout notes", "git checkout feature/x"):
        d = ev(cmd)
        assert not d.allow, f"must be denied without a cwd to resolve it: {cmd}"
        assert "working-tree" in d.reason
    for cmd in ("git checkout -b feature/x", "git checkout --detach",
                "git checkout --track origin/topic"):
        assert ev(cmd).allow, cmd


# --------------------------------------------------------------------------- #
# merge/rebase/cherry-pick/revert/am/pull — the resume/abort machinery.
#
# These five verbs (plus pull, which drives merge or rebase) were first put on
# `_GIT_WORKTREE_SAFE` wholesale, justified as "refuse to run on a dirty tree".
# PROVEN false for their wind-back forms: after a conflicted merge,
#   git add survivor.txt && git merge --abort
# deletes survivor.txt from disk — `--abort` restores the pre-merge state with
# `reset --hard` semantics, staged uncommitted files included. Same class:
# `rebase --abort/--skip`, `cherry-pick --abort/--skip`, `revert --abort`,
# `am --abort/--skip`, and `--autostash` (rebase/merge/pull), whose stash-pop
# can drop or conflict uncommitted work. Asserted as PAIRS like everything
# else here: the wind-back form denied, the operation itself still allowed.
# --------------------------------------------------------------------------- #

_SEQUENCER_PAIRS = [
    # the proven counterexample first
    ("git merge --abort", "git merge origin/dev"),
    ("git merge --autostash origin/dev", "git merge --no-ff origin/dev"),
    ("git rebase --abort", "git rebase origin/main"),
    ("git rebase --skip", "git rebase --continue"),
    ("git rebase --autostash origin/main", "git rebase main"),
    ("git cherry-pick --abort", "git cherry-pick abc123"),
    ("git cherry-pick --skip", "git cherry-pick --continue"),
    ("git revert --abort", "git revert abc123"),
    ("git revert --skip", "git revert --continue"),
    ("git am --abort", "git am --continue"),
    ("git am --skip", "git am patch.mbox"),
    ("git pull --rebase --autostash", "git pull --rebase"),
    ("git pull --autostash", "git pull origin dev"),
    # git accepts unique option abbreviations, so the denial must be a prefix
    # match: `--abor` performs the proven counterexample one character shorter.
    ("git merge --abor", "git merge origin/dev"),
    ("git rebase --sk", "git rebase --strategy=ours origin/main"),
    ("git rebase --au origin/main", "git rebase --autosquash origin/main"),
    ("git cherry-pick --ab", "git cherry-pick abc123"),
]


def test_sequencer_abort_skip_autostash_denied_and_plain_forms_allowed():
    for bad, good in _SEQUENCER_PAIRS:
        d = _ev("Bash", {"command": bad})
        assert not d.allow, f"must be blocked: {bad}"
        assert guard._git_worktree_denial(bad, _WT) is not None, \
            f"new rule missed: {bad}"
        assert "working-tree" in d.reason
        g = _ev("Bash", {"command": good})
        assert g.allow, f"must stay allowed: {good} — {g.reason}"
        assert guard._git_worktree_denial(good, _WT) is None, \
            f"new rule over-blocks: {good}"


def test_gits_own_global_options_do_not_break_legitimate_commands():
    """`-C <dir>` / `-c k=v` are git's options, not the subcommand. Failing to
    skip them makes the rule read `/repo` as the subcommand — which default-deny
    then refuses, so this misreading is invisible in the deny direction and
    only shows up as the coder's own reads being blocked.
    """
    for cmd in ("git -C /repo status", "git -C /repo log --oneline",
                "git -c user.name=x commit -m y", "git --git-dir=/r/.git log",
                "git --work-tree=/r status", "git -c core.pager=cat diff"):
        d = _ev("Bash", {"command": cmd})
        assert d.allow, f"must stay allowed: {cmd} — {d.reason}"
    # ...and the same options must not launder the destructive form either.
    for cmd in ("git -C /repo stash", "git -c user.name=x stash pop",
                "git --git-dir=/r/.git restore ."):
        assert not _ev("Bash", {"command": cmd}).allow, cmd


# --------------------------------------------------------------------------- #
# Installing into the shared developer venv (defence in depth under the
# env-level containment seeded by `ClaudeBackend._options`). Fake checkouts
# only — never the real repo venv.
# --------------------------------------------------------------------------- #

def _fake_primary_checkout(tmp_path):
    primary = tmp_path / "primary"
    (primary / "src" / "no_human").mkdir(parents=True)
    (primary / "src" / "no_human" / "__init__.py").write_text("")
    (primary / ".venv" / "bin").mkdir(parents=True)
    (primary / ".venv" / "bin" / "python").write_text("")
    (primary / ".venv" / "bin" / "pip").write_text("")
    return primary


def _fake_worktree(tmp_path):
    worktree = tmp_path / "worktree"
    (worktree / ".venv" / "bin").mkdir(parents=True)
    (worktree / ".venv" / "bin" / "python").write_text("")
    (worktree / ".venv" / "bin" / "pip").write_text("")
    return worktree


def test_installing_into_the_primary_venv_is_refused(tmp_path, monkeypatch):
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    bypasses = [
        f"{primary}/.venv/bin/pip install foo",
        f"VIRTUAL_ENV={primary}/.venv pip install -e .",
        f"source {primary}/.venv/bin/activate && uv pip install -e .",
        f"uv pip install --python {primary}/.venv/bin/python -e .",
        f"cd {primary} && uv sync",
        # A separator INSIDE a quoted nested-shell payload used to shred the
        # segment before anything looked at it (attempt 2's failure).
        f'sh -c "{primary}/.venv/bin/pip install foo && echo ok"',
        f'bash -lc "cd {primary} && uv sync"',
        # A wrapper flag `_strip_wrappers` cannot parse used to strand the
        # recovery scan, which only knew `git`/shell-runners (attempt 2's
        # second failure).
        f"env -i {primary}/.venv/bin/pip install foo",
        f"sudo -H {primary}/.venv/bin/pip install foo",
        # A leading duration/flag on `timeout`/`nice` used to make argv[0] of
        # `rest` the duration, not the install verb (attempt 2's third
        # failure).
        f"timeout 300 {primary}/.venv/bin/pip install foo",
        f"nice -n 10 {primary}/.venv/bin/pip install foo",
        # uv's own project selectors, which do exactly what `cd <p> && uv
        # sync` does (attempt 2's fourth failure).
        f"uv sync --project {primary}",
        f"uv sync --directory {primary}",
    ]
    for cmd in bypasses:
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=str(worktree))
        assert d.allow is False, f"must be refused: {cmd}"

    # `python -m pip install -e <primary>` run FROM the primary checkout: no
    # explicit venv is named, so the target is resolved from cwd.
    d = guard.evaluate("Bash", {"command": f"python -m pip install -e {primary}"},
                       forbidden_paths=FORBIDDEN, never_push_to=PROTECTED,
                       cwd=str(primary))
    assert d.allow is False


def test_the_refusal_names_the_worktree_venv_alternative(tmp_path, monkeypatch):
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    d = guard.evaluate("Bash", {"command": f"{primary}/.venv/bin/pip install foo"},
                       forbidden_paths=FORBIDDEN, never_push_to=PROTECTED,
                       cwd=str(worktree))
    assert d.allow is False
    assert str(worktree) in d.reason
    assert ".venv" in d.reason
    assert "--python" in d.reason or "run the command from" in d.reason
    assert str(primary / ".venv") in d.reason


def test_installs_into_the_worktree_own_venv_are_allowed(tmp_path, monkeypatch):
    """Negative control: the guard is not a blanket install ban."""
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    allowed = [
        "uv pip install -e .",
        "pip install -r requirements.txt",
        "uv sync",
        f"{worktree}/.venv/bin/pip install foo",
        f"uv pip install --python {worktree}/.venv/bin/python foo",
    ]
    for cmd in allowed:
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=str(worktree))
        assert d.allow is True, f"must stay allowed: {cmd} — {d.reason}"


def test_read_only_and_informational_package_commands_stay_allowed(tmp_path, monkeypatch):
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    allowed = [
        "pip list",
        "pip show x",
        "uv lock",
        "uv venv .venv",
        "uv run pytest",
        'echo "then run pip install -e ."',
    ]
    for cmd in allowed:
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=str(worktree))
        assert d.allow is True, f"must stay allowed: {cmd} — {d.reason}"


def test_unknown_cwd_and_unresolvable_targets_are_refused(tmp_path, monkeypatch):
    primary = _fake_primary_checkout(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    # No cwd at all: an install can't be shown to land anywhere safe.
    d = guard.evaluate("Bash", {"command": "pip install -e ."},
                       forbidden_paths=FORBIDDEN, never_push_to=PROTECTED, cwd=None)
    assert d.allow is False

    # The target is shell expansion — cannot be established either way.
    d = guard.evaluate("Bash", {"command": "VIRTUAL_ENV=$TARGET pip install -e ."},
                       forbidden_paths=FORBIDDEN, never_push_to=PROTECTED, cwd=None)
    assert d.allow is False

    # A packaged/non-editable install: nothing to protect, never a false
    # denial. `sys.prefix` is also neutralised here so this test is not at
    # the mercy of whatever venv happens to be running pytest.
    other = tmp_path / "not-a-venv"
    other.mkdir()
    monkeypatch.setattr(guard, "_primary_checkout", lambda: None)
    monkeypatch.setattr(sys, "prefix", str(other))
    d = guard.evaluate("Bash", {"command": "pip install -e ."},
                       forbidden_paths=FORBIDDEN, never_push_to=PROTECTED,
                       cwd=str(other))
    assert d.allow is True


def test_the_venv_refusal_applies_in_readonly_too(tmp_path, monkeypatch):
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    d = guard.evaluate("Bash", {"command": f"{primary}/.venv/bin/pip install foo"},
                       forbidden_paths=FORBIDDEN, never_push_to=PROTECTED,
                       cwd=str(worktree), readonly=True)
    assert d.allow is False
    assert str(primary / ".venv") in d.reason


def test_protected_venvs_excludes_anything_under_the_session_cwd(tmp_path, monkeypatch):
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    protected = guard._protected_venvs(str(worktree))
    assert (primary / ".venv").resolve() in protected
    assert (worktree / ".venv").resolve() not in protected


# --------------------------------------------------------------------------- #
# Punctuation-run separators. `shlex` in `punctuation_chars` mode emits a
# maximal RUN of punctuation as a SINGLE token ('\n\n', ';\n', '&\n', '||\n'),
# not the individual operators `_INSTALL_SEP_TOKENS` used to check for by
# exact string membership. That let a blank line or a punctuation run glue an
# unrelated prefix command and the real install into one segment whose
# argv[0] was the prefix — `_pkg_install_match` never saw the install.
# --------------------------------------------------------------------------- #

#: This module must be the one under test, in THIS worktree — not a stale
#: editable install resolving elsewhere (`worktree pytest tests the WRONG
#: tree`, a proven failure mode for this repo).
_GUARD_FILE = Path(guard.__file__).resolve()
_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_guard_under_test_is_this_worktrees_copy():
    assert _GUARD_FILE == (_REPO_ROOT / "src" / "no_human" / "agent" / "guard.py").resolve()


def test_reviewer_proven_separator_run_bypasses_are_refused(tmp_path, monkeypatch):
    """Each of these four commands allowed the install through on the
    pre-fix guard (reviewer-proven, review FAIL 2026-08-11)."""
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    bypasses = [
        f"cd {primary}\n\nuv sync",
        f"cd {primary};\nuv sync",
        f"true &\n{primary}/.venv/bin/pip install foo",
        f"echo start\n\n{primary}/.venv/bin/pip install -e {primary}",
    ]
    for cmd in bypasses:
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=str(worktree))
        assert d.allow is False, f"must be refused: {cmd!r}"


def test_installs_into_the_worktrees_own_venv_stay_allowed_across_separators(tmp_path, monkeypatch):
    """Negative control: the punctuation-run fix must not turn into a
    blanket refusal — an install that lands in the task worktree's own
    .venv is still allowed, even across a punctuation-run separator."""
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    allowed = [
        f"cd {worktree}\n\nuv sync",
        f"true &\n{worktree}/.venv/bin/pip install foo",
        "echo start\n\nuv sync",
    ]
    for cmd in allowed:
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=str(worktree))
        assert d.allow is True, f"must stay allowed: {cmd!r} — {d.reason}"


@pytest.mark.parametrize("sep", [";\n", "&\n", "\n\n", "||\n"])
def test_separator_runs_between_a_benign_prefix_and_an_install_are_refused(tmp_path, monkeypatch, sep):
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    cmd = f"echo hello{sep}{primary}/.venv/bin/pip install foo"
    d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                       never_push_to=PROTECTED, cwd=str(worktree))
    assert d.allow is False, f"must be refused: {cmd!r}"


def test_a_trailing_comment_does_not_swallow_the_newline_separator(tmp_path, monkeypatch):
    """Review FAIL 2026-08-11: shlex's default `#` commenter reads through
    end-of-line via its own `readline()`, consuming the `\\n` that should end
    the segment — so a trailing comment glued the benign first line to the
    install on the next line into one segment and the install slipped
    through. Both reviewer-proven repros must be refused."""
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    bypasses = [
        f"echo x #\n{primary}/.venv/bin/pip install foo",
        f"true #comment\ncd {primary} && uv sync",
    ]
    for cmd in bypasses:
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=str(worktree))
        assert d.allow is False, f"must be refused: {cmd!r}"


# --------------------------------------------------------------------------- #
# Grouping punctuation (`(){}`) as a segment boundary. Round 4 (review FAIL
# 2026-08-11) proved `_INSTALL_PUNCTUATION` split `(`/`)` into their own
# tokens but the (then) boundary set only recognised `;&|\n`, so a `(`/`)`
# token glued onto the next command's argv[0] instead of ending a segment —
# `(cd <primary> && uv sync)` tokenised as `[['(', 'cd', '<primary>'],
# ['uv', 'sync', ')']]`, swallowing the `cd`. Fix: a token is a boundary
# when it consists ENTIRELY of `_INSTALL_PUNCTUATION` characters, not a
# hand-picked subset — so `(`, `)`, `{`, `}`, and any run mixing them with
# redirects/separators (`;>`, `)&`, `;(`) all end a segment.
# --------------------------------------------------------------------------- #

def test_reviewer_proven_grouping_punctuation_bypasses_are_refused(tmp_path, monkeypatch):
    """Each of these four commands allowed the install through on the
    pre-fix guard (reviewer-proven, review FAIL 2026-08-11, round 4)."""
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    bypasses = [
        f"(cd {primary} && uv sync)",
        f"(cd {primary}; uv sync)",
        f"{{ cd {primary}; uv sync; }}",
        f"( {primary}/.venv/bin/pip install foo )",
    ]
    for cmd in bypasses:
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=str(worktree))
        assert d.allow is False, f"must be refused: {cmd!r}"


def test_installs_into_the_worktrees_own_venv_stay_allowed_inside_groups(tmp_path, monkeypatch):
    """Negative control: the grouping-punctuation fix must not turn into a
    blanket refusal — an install that lands in the task worktree's own
    .venv is still allowed, even wrapped in a subshell/group."""
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    allowed = [
        "uv pip install -e .",
        f"{worktree}/.venv/bin/pip install foo",
        f"(cd {worktree} && uv sync)",
        f"( {worktree}/.venv/bin/pip install foo )",
        f"{{ cd {worktree}; uv sync; }}",
    ]
    for cmd in allowed:
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=str(worktree))
        assert d.allow is True, f"must stay allowed: {cmd!r} — {d.reason}"


def test_parens_inside_a_quoted_argument_do_not_over_block(tmp_path, monkeypatch):
    """Over-block check: classifying an all-punctuation TOKEN as a boundary
    must not reach into a quoted payload. A quoted argument that merely
    CONTAINS parens (mixed with letters/digits/spaces) is never itself an
    all-punctuation token, so it must survive as one argv entry and the
    command must be judged on its own merits — an install into the
    worktree's own venv with a parenthetical version string stays allowed,
    and an unrelated echo with a parenthetical stays allowed too."""
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    allowed = [
        'echo "(hello world)" && uv sync',
        f'{worktree}/.venv/bin/pip install "pkg==1.0 (stable)"',
    ]
    for cmd in allowed:
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=str(worktree))
        assert d.allow is True, f"must stay allowed: {cmd!r} — {d.reason}"


# --------------------------------------------------------------------------- #
# Shell reserved-word heads (`then`/`do`/`else`/`elif`/…). `_segment_tokens`
# splits a compound command's condition from its BODY on `;`/`\n`, so
# `if true; then <install>; fi` and `while false; do <install>; done` each
# produce a segment whose argv[0] is a bash keyword, not the install verb —
# `then <install...>` / `do <install...>`. Neither `then` nor `do` is a
# package manager or a `_WRAPPERS` entry, so the pre-fix guard never looked
# past them and the install inside the compound-command body slipped through
# (review rounds 2026-08-11 and this ticket; reviewer-proven repros: `if
# true; then P/.venv/bin/pip install foo; fi` and `while false; do
# P/.venv/bin/pip install foo; done`, both `allow=True` pre-fix).
# --------------------------------------------------------------------------- #

def test_reviewer_proven_shell_keyword_bypasses_are_refused(tmp_path, monkeypatch):
    """The exact two repros the reviewer ran against this branch's fixtures
    (cwd=worktree, `_primary_checkout` monkeypatched to a fake primary),
    both `allow=True` pre-fix."""
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    bypasses = [
        f"if true; then {primary}/.venv/bin/pip install foo; fi",
        f"while false; do {primary}/.venv/bin/pip install foo; done",
    ]
    for cmd in bypasses:
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=str(worktree))
        assert d.allow is False, f"must be refused: {cmd!r}"


def test_further_keyword_headed_bodies_are_refused(tmp_path, monkeypatch):
    """Same class, other reserved-word heads the shell grammar allows in
    command position: `else`, `elif`+`then`, `until`/`do`, `for`/`do`, and a
    `case` pattern body. The keyword set is closed by the shell grammar, not
    enumerated by example, so each of these must be caught by the same
    mechanism as the two reviewer repros above."""
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    bypasses = [
        f"if false; then echo x; else {primary}/.venv/bin/pip install foo; fi",
        f"if false; then echo x; elif true; then {primary}/.venv/bin/pip install foo; fi",
        f"until false; do {primary}/.venv/bin/pip install foo; done",
        f"for x in 1; do {primary}/.venv/bin/pip install foo; done",
        f"case $x in a) {primary}/.venv/bin/pip install foo;; esac",
    ]
    for cmd in bypasses:
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=str(worktree))
        assert d.allow is False, f"must be refused: {cmd!r}"


def test_keyword_headed_installs_into_the_worktrees_own_venv_stay_allowed(tmp_path, monkeypatch):
    """Negative control: keyword-stripping must not turn into a blanket
    refusal — an install that lands in the task worktree's own `.venv`
    stays allowed even when it sits in a compound-command body."""
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    allowed = [
        f"if true; then {worktree}/.venv/bin/pip install foo; fi",
        f"while false; do {worktree}/.venv/bin/pip install foo; done",
        "if true; then uv sync; fi",
        "while false; do uv sync; done",
    ]
    for cmd in allowed:
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=str(worktree))
        assert d.allow is True, f"must stay allowed: {cmd!r} — {d.reason}"


def test_a_literal_argument_named_then_does_not_trigger_keyword_stripping(tmp_path, monkeypatch):
    """Both-direction control: `_strip_shell_keywords` only ever removes
    from the HEAD of a segment. A reserved word appearing later in a
    segment — an ordinary argument, e.g. `echo then pip` — must not be
    stripped or mistaken for a compound-command head."""
    primary = _fake_primary_checkout(tmp_path)
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(guard, "_primary_checkout", lambda: primary)

    allowed = [
        "echo then pip",
        "echo do install foo",
        "echo we do not run pip install here",
    ]
    for cmd in allowed:
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=str(worktree))
        assert d.allow is True, f"must stay allowed: {cmd!r} — {d.reason}"
