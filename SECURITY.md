# Security Policy

## Supported versions

no_human is pre-1.0 and ships from `main`. Only the latest commit on `main` is
supported. Fixes land there. Older tags and forks do not get backports.

| Version | Supported |
|---|---|
| `main` (latest) | Yes |
| Anything older | No |

## Reporting a vulnerability

Do not open a public issue for a security problem.

Preferred: use GitHub's private vulnerability reporting. Go to the **Security**
tab of this repository and choose **Report a vulnerability**. That opens a
private advisory visible only to you and the maintainer.

Alternate: email `security@getnohuman.com`.

Please include:

- What the issue is, and what an attacker gets out of it.
- The steps to reproduce, or a proof of concept.
- The commit SHA you tested.
- Your operating system, Python version, and how you installed no_human.

## What to expect

- Acknowledgement within 3 business days.
- An initial assessment, with a severity call and a rough fix timeline, within
  10 business days.
- Updates on the advisory thread until it is closed.
- Credit in the advisory and the release notes, unless you ask otherwise.

This is a single-maintainer project. Those are targets, not a contractual SLA.

There is no bug bounty. No payment is offered for reports.

## Scope

In scope:

- The `nh` CLI, the orchestrator, the local API server, and the web board.
- The desktop shell in `desktop/`.
- Anything that lets code or a prompt escape the guarantees in
  [`docs/security.md`](docs/security.md). Those are the properties the project
  treats as correctness requirements, and a break in one of them is a security
  bug rather than a feature request:
  - A run that bills a credential other than the single configured one.
  - A path that merges a pull request, or that pushes to a branch listed in
    `never_push_to`.
  - A way to read or write a credential inside the repository working tree.
  - A way to defeat the tamper guard so a change with fewer tests reaches the
    reviewer.
  - A way to make the reviewer pass on evidence it did not verify.

Out of scope:

- Vulnerabilities in Claude, the Claude Agent SDK, or the Anthropic API. Report
  those to Anthropic.
- Vulnerabilities in third-party dependencies with no exploitable path through
  no_human. Report those upstream. Tell us if the path through no_human is real.
- The consequences of running no_human against a repository or a machine you do
  not control. It executes tools against your local working tree by design.
- Anything that requires an attacker to already have shell access as the user
  running `nh`.

## Handling of your report

Reports and any attached material are treated as confidential until a fix ships
or you agree to disclose. If a report includes a credential, it is destroyed
after triage, and you should rotate it immediately regardless.
