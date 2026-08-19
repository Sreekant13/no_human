# Changelog

All notable changes to no_human. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.1] — 2026-08-19

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
  (`worker.backend`); reviewer, planner, supervisor and utility tiers stay on
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

[Unreleased]: https://github.com/no-human-ai/no_human/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/no-human-ai/no_human/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/no-human-ai/no_human/releases/tag/v0.1.0
