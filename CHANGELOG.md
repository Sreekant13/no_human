# Changelog

All notable changes to no_human. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.3] — 2026-08-20

Registry release: the package now carries what the official MCP Registry
needs, plus the loop fixes that had accumulated since 0.1.2.

- `server.json` at the repository root describes the MCP bridge (`nh
  mcp-serve`, two tools, stdio) as the PyPI package `no-human`, and the README
  carries the registry's `mcp-name: io.github.no-human-ai/no_human` ownership
  marker. A manual `publish-mcp-registry.yml` workflow publishes it with OIDC
  — no token in the repository — and refuses unless pyproject, server.json and
  PyPI agree on the version.
- A second console script, `no-human`, is the same entry point as `nh`, so
  `uvx no-human mcp-serve` runs the bridge the way registry clients invoke it.
- The wheel-build refusal when the board is absent now also says the short
  way out: `uv tool install no-human` installs the published wheel, board
  included (public issue #4).

- The wake watcher's PR-conflict rung no longer falls through to an
  expensive coder round when it can't tell what's conflicting: a failed
  conflicting-path enumeration — whether `conflicting_paths()` raises, or
  simply returns no result for an unresolvable ref, the more common case —
  now retries once after a best-effort `git fetch` of the base and branch
  refs, and if it's still unresolved afterward, escalates `NOVEL_UNKNOWN`
  instead of guessing. The failure reason now reaches both `task.context`
  and the persisted event's new `error` field, not only the log.
- The review gate no longer takes a blocking finding's word for it: on the
  gate path, a FAIL with non-critical blocking findings now gets one bounded,
  single-turn refute pass (read-only, ~180s) before it's charged to the
  coder. A finding demotes to advisory only when the refute pass cites its
  own counter-evidence at a file:line that itself passes the existing
  citation-existence check; a goal veto, a `spec_compliance:false` verdict,
  and critical-severity findings can never be demoted. A refute pass that
  times out, errors, or reaches no verdict changes nothing — the FAIL stands
  byte-identical, its tokens folded into the decision like every other
  discarded round.

## [0.1.2] — 2026-08-20

Security and release-infrastructure release: same product, patched runtime,
and a release lane for every platform.

- Dependency security: Electron moves to 38.8.6 (the last 38.x), clearing
  every advisory patched within the current major; js-yaml 4.3.1, fast-uri
  3.1.5, undici 6.28.0, postcss 8.5.26, mcp 1.29.0 and cryptography 50.0.0
  likewise. 21 of the repository's 40 open Dependabot alerts closed by
  measurement, not estimate; 19 remain — 18 gated on the Electron 39 major
  (deliberately deferred to a scheduled release) and one, extract-zip
  (GHSA alert #45, high), with no patched version in existence to move to.
- The Windows job gains the same on-demand release lane the Linux job has
  (`workflow_dispatch` + `windows_release`): build, verify against the tree
  that built it, checksum, 7-day artefact. Ordinary CI runs are untouched.
- Windows bundles now carry the same BUILD_STAMP provenance
  (`commit=/dirty=/board_sha256=`) POSIX builds have had since the stale-DMG
  incident — an absent stamp fails verification rather than passing quietly.

## [0.1.1] — 2026-08-20

Also in this release — reliability, honesty, and cost, measured not asserted
(full suite 8,864/0; funnel 5/5 with every holdout green; reviewer recall
17/19, up from 15/19):

- The eval judge's verdict now survives mid-run emission, a truncated end
  marker, and marker drift — six bench tasks per run were being scored as
  failures because a verdict could not be parsed, not because work was wrong.
- Git lock contention (another process briefly holding `index.lock`) is
  retried with two short backoffs instead of crashing the task; every other
  git failure still fails fast and loud.
- Fix pairs: when a task fails on an error this machine has overcome before,
  the retry is handed what worked — as evidence, never as an instruction.
- A retry that ends byte-identical to its predecessor (same failure, same
  diff) stops the loop and escalates honestly instead of buying the most
  expensive third attempt.
- Judgment-call blockers (ambiguity, novel-unknown, impossible) get exactly
  one supervisor-checked challenge before parking; external blockers are
  honored untouched, and a park is never converted into a fake "done".
- The reviewer carries a maintainability-trajectory lens: does this change
  make the NEXT change harder? Concrete findings only, capped below blocking
  severity.
- `nh bench harvest`: escalated, parked, and failed tasks become bench-spec
  candidates for curation.
- The intake grill's answering pass pays for what the task needs: probe
  budget scales with the question count; prose-only tasks skip filesystem
  probes (assumption-grade answers, clearly marked).
- Onboarding: two checkouts of the same repository are tellable apart —
  colliding names show their full path. (Authored end-to-end by no_human
  from its own board, review PASS, 8,847/0.)
- The stale-data banner no longer eats clicks while disconnected.
- docs: an operator profile for reviewing untrusted external PRs in a
  credential-isolated container.
- This release restores auto-update for installed apps: it ships the ZIP and
  `latest-mac.yml` that `electron-updater` requires (0.1.0's release lacked
  both).

### Added
- CI builds the board-carrying wheel on every run and proves it installs:
  `uv tool install <wheel>` yields an `nh` that finds its board and the Agent
  SDK's bundled `claude` — no Node, no separate CLI install. A release build
  (`workflow_dispatch` with `wheel_release`) keeps the wheel as an artefact.
- A Claude Code plugin at `plugins/no-human/` exposing the MCP bridge's two
  tools (`task_add`, `task_status`).
- A `Publish to PyPI` workflow (`workflow_dispatch` only, typed confirmation)
  that builds the board-carrying wheel and uploads it with PyPI Trusted
  Publishing — no API token anywhere in the repository.
- Version is 0.1.1 across `pyproject.toml`, `desktop/package.json` and
  `web/package.json` (and those lockfiles' root entries), so a built wheel is
  no longer labelled with the released 0.1.0's version.
- `CHANGELOG.md` (this file) and `glama.json`.

### Changed
- README: download buttons, the site's hero loop under the title, install
  leads with the desktop app and names each build's architecture; `nh approve`
  is documented as what it does — it squash-lands the PR as the configured
  operator identity (`git.approve_identity`).
- `CONTRIBUTING.md`, `docs/adapters.md` and the `nh task add --backend` help no
  longer say "a single Claude backend": the coder runs on the Claude Agent SDK
  by default with OpenAI Codex as the sanctioned second backend
  (`worker.backend`); reviewer, planner, supervisor, utility and intake tiers stay on
  Claude.

### Fixed
- The shipped harvest test no longer asserts that the (unshipped) scored corpus
  directory exists, so the public repository's CI runs green.

## [0.1.0] — 2026-08-16

First packaged release. no_human takes a software task end to end — plan,
code, test, adversarial review by a second model, and a pull request with the
evidence it works. A human approves and merges.

### Added
- **macOS** — `no_human-0.1.0.dmg`, signed and notarized (Apple silicon). The
  app bundles the server and the board and runs on your own Claude
  subscription. A `.sha256` ships alongside.
- **Windows** — `no_human-0.1.0-UNSIGNED.exe` (x64 installer, per-user, no
  administrator prompt) and `no_human-0.1.0-UNSIGNED.zip` (portable build of
  the same payload). Unsigned: SmartScreen warns until code signing lands.
  `SHA256SUMS-windows.txt` ships alongside.
- **Linux** — `no_human-0.1.0-linux-amd64.deb` and
  `no_human-0.1.0-linux-x86_64.AppImage` (x64), added to the same release on
  2026-08-18, built by the public repository's CI. `SHA256SUMS-linux.txt`
  ships alongside.

[Unreleased]: https://github.com/no-human-ai/no_human/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/no-human-ai/no_human/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/no-human-ai/no_human/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/no-human-ai/no_human/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/no-human-ai/no_human/releases/tag/v0.1.0
