"""Every npm install in this repo must resolve from the PUBLIC registry.

THE BUG THIS EXISTS FOR. `~/.npmrc` on the maintainer's machine carried a
`registry=` line pointing at their former employer's internal mirror. That is
user-level config: it applies to every `npm install` that user ever runs, in
every repo, forever. One install rewrote eight `resolved` URLs in
`package-lock.json` to that host, committing an internal infrastructure
hostname into a repo that is about to be published. Reverting the URLs fixed
the symptom and nothing else — the cause lives outside the repo and re-applies
on the next install.

The fix is a project-level `.npmrc`, which npm's config precedence (CLI > env >
project > user > global > builtin) places ABOVE the user file. These tests hold
that fix in place.

WHY ONE `.npmrc` PER DIRECTORY AND NOT ONE AT THE ROOT. npm reads its project
config from the CURRENT WORKING DIRECTORY and does not walk up to a parent.
Measured on 2026-07-31 with a root-only `.npmrc` in place::

    $ (cd web && npm config get registry)
    https://npm.<internal-mirror>/repository/npm-all/

so the root file protected nothing that mattered. With `web/.npmrc` and
`desktop/.npmrc` added, all three directories report
``https://registry.npmjs.org/``. ``test_every_npm_directory_pins_the_public_registry``
therefore keys on the directory of each ``package.json``, not on the repo root:
that co-location IS the mechanism, and a future ``foo/package.json`` with no
sibling ``.npmrc`` is exactly the reopened hole.

The second test is the backstop for the case this file cannot prevent — an
install run before the `.npmrc` landed, or with an explicit `--registry` flag.
It reads what the lockfiles actually SAY, which is the artifact that ships.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(
    subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        text=True,
    ).strip()
)

PUBLIC_REGISTRY = "https://registry.npmjs.org/"

# The one host an npm artifact in this repo may name. An allowlist of exactly
# one, and the direction is deliberate: a new host is a RED test that someone
# has to look at and justify, whereas a denylist of known-bad hosts would have
# to have anticipated the specific internal mirror before it ever appeared.
PUBLIC_REGISTRY_HOSTS = frozenset({"registry.npmjs.org"})

_REGISTRY_LINE = re.compile(r"^\s*registry\s*=\s*(\S+)\s*$", re.MULTILINE)
_URL_HOST = re.compile(r"https?://([^/\s\"']+)")

# Keys whose value is "where this package's bytes come from". These are the
# only URLs in a lockfile that a registry redirect rewrites.
_DOWNLOAD_KEYS = frozenset({"resolved", "tarball"})

# Any URL that LOOKS like a package tarball, whatever key holds it. A first
# draft of this test scanned every URL in the file and went red on 12 hosts —
# all of them `funding` entries (opencollective, tidelift, patreon, an author's
# homepage), which are package metadata and have nothing to do with where bytes
# are fetched from. Scanning everything is not a stricter test, it is a test
# that gets deleted. But keying ONLY on a field name is how a scanner ends up
# green on a shape nobody told it about, so the shape below runs alongside the
# key list and catches a download URL that arrives under some other name.
_TARBALL_URL = re.compile(r"https?://[^\s\"']+\.tgz\b")


def _tracked() -> list[str]:
    out = subprocess.check_output(["git", "ls-files"], cwd=REPO_ROOT, text=True)
    return [line for line in out.splitlines() if line]


def _npm_dirs() -> list[str]:
    """Repo-relative directories that run npm: every tracked `package.json`'s
    directory, plus the repo root (where a contributor may plausibly type
    `npm install` by mistake, and where a stray lockfile would then appear).

    Derived from git rather than from a hand-written list, so a new npm package
    added anywhere in the tree is covered the moment it is committed.
    """
    dirs = {""}
    for rel in _tracked():
        p = Path(rel)
        if p.name == "package.json" and "node_modules" not in p.parts:
            dirs.add(p.parent.as_posix() if p.parent != Path(".") else "")
    return sorted(dirs)


def _lockfiles() -> list[str]:
    return [rel for rel in _tracked()
            if Path(rel).name == "package-lock.json"
            and "node_modules" not in Path(rel).parts]


def test_every_npm_directory_pins_the_public_registry():
    """A tracked `.npmrc` beside every `package.json`, pinning the public host.

    Tracked, not merely present: an untracked `.npmrc` protects this working
    copy and no clone, no CI job and no contributor.
    """
    tracked = set(_tracked())
    problems: list[str] = []
    for d in _npm_dirs():
        rel = f"{d}/.npmrc" if d else ".npmrc"
        if rel not in tracked:
            problems.append(
                f"{rel}: missing (or untracked) — npm run from {d or '<repo root>'} "
                "would inherit the user's ~/.npmrc registry")
            continue
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        except OSError as e:
            # Tracked but unreadable is a violation, not a crash: the failure
            # must name the file, not a stack trace inside pathlib.
            problems.append(f"{rel}: tracked but unreadable ({e.__class__.__name__})")
            continue
        found = _REGISTRY_LINE.findall(text)
        if not found:
            problems.append(f"{rel}: no `registry=` line, so it overrides nothing")
        elif found[-1] != PUBLIC_REGISTRY:
            # Last wins in an ini file, so the last line is the effective one.
            problems.append(f"{rel}: pins {found[-1]!r}, expected {PUBLIC_REGISTRY!r}")
    assert not problems, (
        "npm directories not pinned to the public registry:\n  "
        + "\n  ".join(problems)
        + "\n\nnpm reads project config from the CURRENT directory and does not "
          "walk up, so each directory that runs npm needs its own file."
    )


def _download_urls(node, out: list[str]) -> list[str]:
    """Every "where the bytes come from" URL in a parsed lockfile."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _DOWNLOAD_KEYS and isinstance(v, str) and v.startswith("http"):
                out.append(v)
            else:
                _download_urls(v, out)
    elif isinstance(node, list):
        for v in node:
            _download_urls(v, out)
    return out


def test_no_lockfile_references_a_non_public_registry():
    """What the committed artifact actually says.

    Two independent passes over the same file — see `_DOWNLOAD_KEYS` and
    `_TARBALL_URL` for why it is both a key list and a shape, and why it is not
    simply "every URL".
    """
    violations: list[str] = []
    for rel in _lockfiles():
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        urls = _download_urls(json.loads(text), [])
        urls += _TARBALL_URL.findall(text)
        for url in sorted(set(urls)):
            host = _URL_HOST.match(url)
            if host and host.group(1) not in PUBLIC_REGISTRY_HOSTS:
                violations.append(f"{rel}: {host.group(1)}  ({url})")
    assert not violations, (
        f"{len(violations)} non-public registry host(s) in a committed lockfile:\n  "
        + "\n  ".join(violations)
        + "\n\nThis is what a user-level ~/.npmrc `registry=` line does to a "
          "lockfile. Re-resolve with the public registry (the sibling .npmrc "
          "now pins it) rather than editing the URLs by hand."
    )


def test_the_lockfile_scan_is_scanning_something():
    """An empty corpus makes the check above vacuously green.

    The 2026-07-30 export review found a guard whose scope had quietly drifted
    to zero files and reported success; this is the one-line version of not
    letting that happen here.
    """
    locks = _lockfiles()
    assert locks, "no package-lock.json found — the registry scan covers nothing"
    for rel in locks:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        found = _download_urls(json.loads(text), [])
        assert found, (
            f"{rel} yields no download URLs — the host check cannot fail on it, "
            "so the lockfile format changed and _DOWNLOAD_KEYS is stale")
