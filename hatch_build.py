"""Keep the gitignored board included in a release wheel current.

``web/dist`` is a local build artifact, not repository content. Hatchling
force-includes it from the build tree, so presence alone is insufficient: a
developer can otherwise build a wheel with a bundle older than that tree's
``web/src``. The standard build hook therefore rebuilds a *stale* board and
refuses the pre-existing bundle if it cannot make it current. A *missing*
board is unchanged from before this fix: the standard path never invokes npm
on its own initiative and fails immediately, the same release-without-a-build
guard as always — only a present-but-outdated bundle is new territory.

Freshness is a content digest of Vite's inputs, recorded in
``web/.board-stamp.json`` after a successful build. The stamp is deliberately
outside ``web/dist`` so the wheel's board payload is unchanged. It uses bytes
and paths only, never filesystem metadata; a fresh clone has the same verdict.

An sdist-to-wheel tree has ``web/dist`` but intentionally no ``web/src``. It
has nothing local to compare the bundle against, so that path remains a
presence-only include. Editable builds warn rather than fail so ``uv sync``
remains installable; distributable builds fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

try:  # pragma: no cover - hatchling is present in isolated builds
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ModuleNotFoundError:  # pragma: no cover - direct test/CLI import
    BuildHookInterface = object  # type: ignore[assignment,misc]

EDITABLE = "editable"
DEFAULT_SOURCE = "web/dist"
NPM = "npm"
NPM_TIMEOUT = 900
STAMP_NAME = ".board-stamp.json"
STAMP_VERSION = 1
STAMP_ALGORITHM = "sha256"
_SHA256_LENGTH = 64
_VITE_ROOT_FILES = (
    "index.html", "package.json", "package-lock.json", "vite.config.js",
    "tailwind.config.js", "postcss.config.js",
)


class BoardNotBuiltError(RuntimeError):
    """A distributable artifact was built without a board to put in it."""


class BoardStaleError(BoardNotBuiltError):
    """A distributable artifact tried to use a board from different sources."""


def _build_it() -> str:
    return ("cd web && npm install && npm run build && cd .. "
             "&& python3 hatch_build.py --stamp")


def _npm_available() -> bool:
    return shutil.which(NPM) is not None


def _web_dir(root: str | Path, source: str = DEFAULT_SOURCE) -> Path:
    return Path(root) / Path(source).parent


def stamp_path(root: str | Path, source: str = DEFAULT_SOURCE) -> Path:
    return _web_dir(root, source) / STAMP_NAME


def _digest_lines(web: Path) -> list[bytes] | None:
    if not (web / "src").is_dir():
        return None
    files = [web / rel for rel in _VITE_ROOT_FILES if (web / rel).is_file()]
    for directory in (web / "src", web / "public"):
        if directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file()
                         and not path.name.endswith(".test.mjs"))
    return sorted(
        hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii") + b"  "
        + os.fsencode(path.relative_to(web).as_posix())
        for path in files
    )


def source_digest(root: str | Path, source: str = DEFAULT_SOURCE) -> str | None:
    """Hash Vite inputs by bytes and path, or ``None`` when sources are absent."""
    lines = _digest_lines(_web_dir(root, source))
    if lines is None:
        return None
    return hashlib.sha256(b"\n".join(lines) + b"\n").hexdigest()


def read_stamp(root: str | Path, source: str = DEFAULT_SOURCE) -> str | None:
    """Read only a complete, versioned source digest; malformed means stale."""
    try:
        data = json.loads(stamp_path(root, source).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    digest = data.get("digest") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get("version") != STAMP_VERSION
            or data.get("algorithm") != STAMP_ALGORITHM
            or not isinstance(digest, str) or len(digest) != _SHA256_LENGTH):
        return None
    try:
        int(digest, 16)
    except ValueError:
        return None
    return digest


def write_stamp(root: str | Path, digest: str | None = None,
                source: str = DEFAULT_SOURCE) -> Path:
    """Write the stamp last, so an interrupted build is stale rather than fresh."""
    digest = source_digest(root, source) if digest is None else digest
    if digest is None:
        raise ValueError("cannot stamp a board without web/src sources")
    path = stamp_path(root, source)
    path.write_text(json.dumps({"version": STAMP_VERSION, "algorithm": STAMP_ALGORITHM,
                                "digest": digest}, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def board_state(root: str | Path, source: str = DEFAULT_SOURCE) -> tuple[str, str]:
    """Return explicit bundle/source/stamp state and the reason for a refusal."""
    index = Path(root) / source / "index.html"
    digest = source_digest(root, source)
    if not index.is_file():
        return "missing", f"{index} is missing"
    if digest is None:
        return "source-absent", "web/src is absent (sdist build tree)"
    stamped = read_stamp(root, source)
    if stamped != digest:
        actual = "missing or invalid" if stamped is None else stamped
        return "stale", f"source digest is {digest}, stamp is {actual}"
    return "current", f"source digest {digest}"


def try_build_board(
    root: str | Path,
    source: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    """Run npm and write a source stamp when it produces an index.html.

    This is deliberately non-raising for editable installs. The standard path
    wraps its ``False`` result in a release-blocking, actionable exception.
    """
    web_dir = _web_dir(root, source)
    if not (web_dir / "package.json").is_file() or not _npm_available():
        return False
    try:
        for argv in (["npm", "install"], ["npm", "run", "build"]):
            result = runner(argv, cwd=web_dir, capture_output=True, text=True,
                            check=False, timeout=NPM_TIMEOUT)
            if result.returncode != 0:
                return False
    except (OSError, subprocess.SubprocessError):
        return False
    if not (Path(root) / source / "index.html").is_file():
        return False
    digest = source_digest(root, source)
    if digest is not None:
        try:
            write_stamp(root, digest, source)
        except OSError:
            return False
    return True


def board_missing_warning(index: Path) -> str:
    return (
        f"\nno_human: the web board is not built — {index} is missing.\n"
        "  This dev install will work, but `nh start` will serve a notice\n"
        "  instead of the board until you run:\n"
        f"      {_build_it()}\n"
        "  (The board is a gitignored build artifact, so a fresh clone never\n"
        "  has one. The CLI — `nh task`, `nh status` — is unaffected.)\n"
    )


def board_missing_error(index: Path, target_name: str) -> str:
    return (
        f"\nno_human: refusing to build a {target_name} without the web board.\n\n"
        f"  missing: {index}\n\n"
        "  Build the board first:\n"
        f"      {_build_it()}\n"
    )


def board_stale_warning(index: Path, reason: str) -> str:
    return (
        f"\nno_human: the web board at {index} is stale ({reason}).\n"
        "  This editable install will keep using it; rebuild before release:\n"
        f"      {_build_it()}\n"
    )


def board_stale_error(index: Path, target_name: str, reason: str) -> str:
    return (
        f"\nno_human: refusing to build a {target_name} with a stale web board.\n\n"
        f"  stale: {index} — {reason}\n"
        "  The existing bundle was refused, not shipped. npm must be available "
        "to rebuild it:\n"
        f"      {_build_it()}\n"
    )


def rebuild_board_or_raise(
    root: str | Path, source: str, target_name: str, *, stale: bool,
    builder: Callable[[str | Path, str], bool] | None = None,
) -> None:
    """Regenerate a standard-build board and reject it unless it is current."""
    index = Path(root) / source / "index.html"
    previous_state, reason = board_state(root, source)
    build = builder or try_build_board
    if not _npm_available():
        if stale:
            raise BoardStaleError(board_stale_error(index, target_name, reason))
        raise BoardNotBuiltError(board_missing_error(index, target_name))
    if not build(root, source):
        if stale:
            raise BoardStaleError(board_stale_error(index, target_name, reason))
        raise BoardNotBuiltError(board_missing_error(index, target_name))
    state, after = board_state(root, source)
    if state == "current":
        return
    if stale or previous_state == "stale":
        raise BoardStaleError(board_stale_error(index, target_name, after))
    raise BoardNotBuiltError(board_missing_error(index, target_name))


def plan_board_inclusion(
    root: str | Path, source: str, target: str, version: str,
    target_name: str = "wheel", *,
    builder: Callable[[str | Path, str], bool] | None = None,
) -> tuple[dict[str, str], str | None]:
    """Choose inclusion from the explicit missing/current/stale state table."""
    state, reason = board_state(root, source)
    index = Path(root) / source / "index.html"
    if state in {"current", "source-absent"}:
        return {source: target}, None
    if version == EDITABLE:
        if state == "missing":
            build = builder or try_build_board
            if build(root, source) and board_state(root, source)[0] in {"current", "source-absent"}:
                return {source: target}, None
            return {}, board_missing_warning(index)
        return {source: target}, board_stale_warning(index, reason)
    if state == "missing":
        # Unchanged from before this fix: a standard build never shells out to
        # npm on its own initiative when there is no bundle at all — that is
        # a release cut without `npm run build`, and it must fail loudly and
        # immediately, not silently start building one (see
        # `tests/test_worktree_forced_includes.py`, which relies on exactly
        # this to prove a worktree cannot build without provisioning).
        raise BoardNotBuiltError(board_missing_error(index, target_name))
    rebuild_board_or_raise(root, source, target_name, stale=True, builder=builder)
    return {source: target}, None


class BoardBuildHook(BuildHookInterface):  # type: ignore[misc,valid-type]
    """The release gate run by Hatchling for wheel and sdist builds."""

    PLUGIN_NAME = "no-human-board"

    def initialize(self, version: str, build_data: dict) -> None:
        source = self.config.get("source", DEFAULT_SOURCE)
        target = self.config.get("target")
        if not isinstance(target, str) or not target:
            raise ValueError("hatch_build.py hook needs a target path")
        additions, warning = plan_board_inclusion(
            self.root, source, target, version, self.target_name)
        if warning:
            self.app.display_warning(warning)
        build_data["force_include"].update(additions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="check or stamp the local board")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--stamp", action="store_true")
    parser.add_argument("root", nargs="?", default=".", type=Path)
    args = parser.parse_args(argv)
    if args.stamp:
        try:
            path = write_stamp(args.root)
        except (OSError, ValueError) as exc:
            print(f"board stamp failed: {exc}", file=sys.stderr)
            return 1
        print(f"board stamped: {path}")
        return 0
    state, reason = board_state(args.root)
    if state == "current":
        print(f"board current: {reason}")
        return 0
    print(f"board stale: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
