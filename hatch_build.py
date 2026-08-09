"""The board is REQUIRED for a distributable build and OPTIONAL for a dev one.

`web/dist` is the built React board. `nh start` — the entrypoint README calls
primary — serves it, so a wheel or sdist built without it installs a CLI whose
main command renders nothing. That shipped once, and the fix was to declare the
board with hatchling's `force-include`, which raises `Forced include not found`
when the source is absent: a release cut without `npm run build` FAILS instead
of quietly shipping the broken product.

That guarantee was right; the mechanism was too wide. `force-include` is static
configuration, so it is evaluated for EVERY build target and version — including
the editable wheel that `uv sync` and `pip install -e .` build. `web/dist` is a
gitignored build artifact, so a clean clone of this repo does not have one, and
the release-time guard fired on the developer's first command instead:

    $ git clone … && cd no_human && uv sync
    FileNotFoundError: Forced include not found: <repo>/web/dist
    Call to `hatchling.build.build_editable` failed (exit status: 1)

A clean clone could not be installed, developed, or tested — before collection,
with a message that reads like a broken repository rather than a missing npm
build. (The comment this replaces claimed "nothing in CI builds a wheel, so this
only ever fires on a release build". That was false: `uv sync` reaches the same
code path through `build_editable`.)

So the declaration moves here, where the build VERSION is visible. Hatchling
calls `initialize(version, build_data)` with `version == "editable"` for an
editable install and `"standard"` for everything else, and honours anything a
hook adds to `build_data["force_include"]` (merged in `BuilderConfig.
set_build_data`, read by both `build_standard` and `build_editable_detection`).
That is the whole seam:

  * editable + no board  -> build it automatically when `npm` is on PATH ("cd
    web && npm install && npm run build"). If npm is absent, the build fails,
    or it exits 0 without producing `index.html`, fall back to warning and
    include nothing — the install still succeeds either way.
  * editable + board     -> include it, exactly as before.
  * standard + no board  -> FAIL, with a message that names `npm run build` and
    the directory. The release-time guarantee is unchanged; only its blast
    radius is.
  * standard + board     -> include it, exactly as before.

Presence is judged by `index.html`, not by the directory. `npm run build` writes
INTO an existing directory, so an interrupted or cleaned build leaves the husk
behind — and the old directory-existence check would have force-included that
husk into a release wheel, which is the boardless-CLI bug wearing a directory.
`api/app.py::_resolve_web_dist` already refuses a husk for the same reason.

`migrations/` stays a static `force-include` in `pyproject.toml`: it is TRACKED,
so it is present in every clone and worktree, and a static declaration is the
simpler thing that works.

The hatchling import is guarded so this module can be imported as a plain script
by `tests/test_clean_clone_installable.py`. Hatchling is a BUILD dependency — it
exists only inside the isolated build environment PEP 517 creates, never in this
repo's own venv — so an unguarded import would make the decision logic below
untestable anywhere except inside a real build, which is the slowest and least
specific place to test it.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

try:  # pragma: no cover - the real build environment always has hatchling
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ModuleNotFoundError:  # pragma: no cover - importing this file as a module
    BuildHookInterface = object  # type: ignore[assignment,misc]

# The version string hatchling passes for `pip install -e .` / `uv sync`.
EDITABLE = "editable"

DEFAULT_SOURCE = "web/dist"

NPM = "npm"

# `npm install` on a cold `node_modules` can take a while; this bounds an
# editable install rather than hanging it forever.
NPM_TIMEOUT = 900


class BoardNotBuiltError(RuntimeError):
    """A distributable artifact was built without a board to put in it."""


def _build_it() -> str:
    return "cd web && npm install && npm run build"


def _npm_available() -> bool:
    return shutil.which(NPM) is not None


def try_build_board(
    root: str | Path,
    source: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    """Attempt the editable-install board build named by `_build_it()`.

    Runs `npm install` then `npm run build` in `<root>/<web dir>` (the parent
    of `source`) and returns whether `<root>/<source>/index.html` exists
    afterwards. Never raises: an absent `npm`, a missing `web/package.json`
    (not a real checkout — e.g. a test's bare `tmp_path`), a non-zero exit, a
    crash, or a timeout all just return `False` so the caller can fall back to
    `board_missing_warning`. A 0 exit with no `index.html` is also `False` —
    npm can exit 0 having produced an incomplete build.
    """
    web_dir = Path(root) / Path(source).parent
    if not (web_dir / "package.json").is_file():
        return False
    if not _npm_available():
        return False
    try:
        for argv in (["npm", "install"], ["npm", "run", "build"]):
            result = runner(
                argv, cwd=web_dir, capture_output=True, text=True,
                check=False, timeout=NPM_TIMEOUT,
            )
            if result.returncode != 0:
                return False
    except (OSError, subprocess.SubprocessError):
        return False
    return (Path(root) / source / "index.html").is_file()


def board_missing_warning(index: Path) -> str:
    return (
        f"\nno_human: the web board is not built — {index} is missing.\n"
        f"  This dev install will work, but `nh start` will serve a notice\n"
        f"  instead of the board until you run:\n"
        f"      {_build_it()}\n"
        f"  (The board is a gitignored build artifact, so a fresh clone never\n"
        f"  has one. The CLI — `nh task`, `nh status` — is unaffected.)\n"
    )


def board_missing_error(index: Path, target_name: str) -> str:
    return (
        f"\nno_human: refusing to build a {target_name} without the web board.\n"
        f"\n"
        f"  missing: {index}\n"
        f"\n"
        f"  `nh start` is the documented entrypoint and it serves this board, so\n"
        f"  a {target_name} built without it installs a CLI whose main command\n"
        f"  renders nothing. That has shipped once already, which is why this is\n"
        f"  a build failure and not a warning.\n"
        f"\n"
        f"  Build the board first:\n"
        f"      {_build_it()}\n"
        f"\n"
        f"  A dev install does NOT need it: `uv sync` and `pip install -e .`\n"
        f"  warn and continue.\n"
    )


def plan_board_inclusion(
    root: str | Path,
    source: str,
    target: str,
    version: str,
    target_name: str = "wheel",
    *,
    builder: Callable[[str | Path, str], bool] | None = None,
) -> tuple[dict[str, str], str | None]:
    """Decide what a build of `version` does about the board.

    Returns `(force_include_additions, warning_or_None)` and raises
    `BoardNotBuiltError` when a distributable build has no board to ship. Split
    out of the hook class so it can be tested without hatchling and without
    running a build — the branch that matters (editable vs standard) is one
    string comparison, and it should be provable in milliseconds.

    `builder` is test-injectable and only ever consulted on the editable path
    (default `try_build_board`); the standard/distributable path never calls
    it and always fails closed when the board is absent.
    """
    index = Path(root) / source / "index.html"
    if index.is_file():
        return {source: target}, None
    if version == EDITABLE:
        build = builder or try_build_board
        if build(root, source) and index.is_file():
            return {source: target}, None
        return {}, board_missing_warning(index)
    raise BoardNotBuiltError(board_missing_error(index, target_name))


class BoardBuildHook(BuildHookInterface):  # type: ignore[misc,valid-type]
    """Injects the board's `force-include` entry, or explains its absence."""

    PLUGIN_NAME = "no-human-board"

    def initialize(self, version: str, build_data: dict) -> None:
        source = self.config.get("source", DEFAULT_SOURCE)
        target = self.config.get("target")
        if not isinstance(target, str) or not target:
            msg = (
                "hatch_build.py: the hook needs a `target` in "
                f"[tool.hatch.build.targets.{self.target_name}.hooks.custom] — "
                "the path the board takes inside the artifact."
            )
            raise ValueError(msg)

        additions, warning = plan_board_inclusion(
            self.root, source, target, version, self.target_name
        )
        if warning:
            self.app.display_warning(warning)
        build_data["force_include"].update(additions)
