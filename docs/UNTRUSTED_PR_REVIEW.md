# Reviewing untrusted pull requests in a container

The repo is public. The first external PR is **untrusted input aimed at a
machine that holds an OAuth token**: a diff (and everything reachable from
it — test files, fixture data, tool configs, hook scripts) chosen by someone
you have not met, reviewed by an agent that can read files and run commands.
The threat is not hypothetical in this codebase's own history: the review
gate has already caught a prompt-injection channel into the merge gate on a
*friendly* diff. An external PR is that channel with an adversary on the
other end.

This document is the operator profile for reviewing such a PR **without your
credentials in the blast radius**. It is a prerequisite for accepting outside
contributions, not an enhancement.

## What must never be reachable

On the host, a review session can reach — because the product legitimately
uses them everywhere else:

| Secret | Where it lives on the host |
|---|---|
| Claude OAuth token / product env | `~/.no_human/.env` (chmod 600), `~/.claude/` |
| GitHub auth | `gh`'s keyring / `~/.config/gh/hosts.yml` |
| Your other repos and their history | `~/git/**` |
| The product's own DB (task history, learnings) | `~/.no_human/no_human.db` |

The container profile's single job: the review runs with **none of the
above mounted**, and with credentials that can do exactly one thing — read
the PR.

## The profile

```sh
# 1. A throwaway checkout of ONLY the PR, on the host, in an empty dir:
mkdir -p /tmp/untrusted-review && cd /tmp/untrusted-review
git clone --depth 50 https://github.com/no-human-ai/no_human.git repo
cd repo && git fetch origin pull/<PR#>/head:pr-under-review
git checkout pr-under-review

# 2. Review INSIDE a container that gets the checkout and nothing else.
#    - no ~/.claude, no ~/.no_human, no gh auth, no docker socket
#    - network disabled: the reviewer reads code; it does not need egress,
#      and an injected payload's first move is usually a phone-home
docker run --rm -it \
  --network none \
  --cap-drop ALL --security-opt no-new-privileges \
  -v "$PWD":/work:ro \
  -w /work \
  python:3.12-slim bash

# 3. Inside: read-only inspection with plain tools (grep/diff/python -m
#    pyflakes …). If an AGENT review is wanted, provision a SEPARATE
#    throwaway credential for it inside the container — never the operator's
#    own token — and re-enable network for exactly that run.
```

Rules the profile encodes, stated so a future change can be judged against
them:

1. **Read-only mount.** What this actually stops is the CONTAINER writing:
   an agent (or a build step the PR's code triggers) cannot drop a hook script
   or a helper into the checkout that a later host-side command would run. It
   does NOT stop what the diff already carries — step 1 checks the PR out on
   the host — and a diff cannot set repo config such as `core.fsmonitor` at
   all. Files the diff adds under `.githooks/` are inert until something
   points `core.hooksPath` at them, which is the host's decision, not the
   PR's; read the diff before you run anything that would.
2. **No credential volumes.** The container has no `~/.claude`, `~/.no_human`,
   `~/.config/gh`. An injected instruction that says "read the token file"
   finds no file.
3. **No network by default.** Exfiltration needs a channel; deny it. The one
   sanctioned exception is an agent review on a **throwaway** credential.
4. **The verdict crosses the boundary as text you read** — never as a file
   the host executes, never as a command the host runs. (The reviewer's
   output is untrusted content in higher-trust context — the same rule the
   in-product gate applies to its own reviewer's summary.)
5. **Never run the PR's tests on the host** before the review verdict.
   `pytest` executes arbitrary code by design; conftest.py runs at import.

## Honest limits

This is a useful isolation boundary, **but it is still Docker, not a full
VM**: a kernel escape is out of scope of this profile, and a malicious diff
aimed specifically at container escape is met with defense-in-depth
(`--cap-drop ALL`, `no-new-privileges`, no socket mounts), not certainty.
The profile also does nothing about what you *merge* — it protects the
review, not the judgment. The never-merge boundary and the human approval
remain the last gate, unchanged.

## Relationship to the in-product pipeline

The PRODUCT's own coder/reviewer run on the operator's tasks against the
operator's repos — trusted input, full pipeline, unchanged by this document.
This profile exists for the one flow where the input is chosen by an
outsider: an external PR to the public repo. Do not wire external PRs into
`nh` intake; review them here first, merge (or not) as a human, and only
then does the merged code become normal trusted input.
