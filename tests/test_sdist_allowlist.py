"""The sdist is a membership list — nothing ships that is not named.

Without `only-include`, hatchling packages everything under the project root
that is not ignored, and on a development machine that includes UNTRACKED
files: a byte-scan of a freshly built sdist (2026-07-31) found local scratch
notes and the private half of the vendor-term corpus inside the tarball. No
scanner can enumerate every way a local file can be sensitive, so the fix is
not a better denylist — it is that the sdist's contents are decided by an
explicit allowlist. One honest caveat: an allowlist of DIRECTORIES admits what
is inside them, so untracked junk UNDER an allowed root (an editor swap file,
a stray module) still ships unless its pattern is excluded — that hole is
narrowed by the junk-pattern excludes and covered below, not closed by theory.

The fast test pins the declaration; the slow test builds the real tarball and
checks the actual member list both ways (nothing outside the allowlist, and
the essentials present), then builds a wheel FROM that sdist to prove the
narrowed sdist is still a sufficient build source.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_sdist_is_an_explicit_allowlist():
    """The declaration. Deleting `only-include` reverts to tree-sweeping."""
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    sdist = cfg["tool"]["hatch"]["build"]["targets"]["sdist"]

    assert sdist["only-include"] == ["src/no_human"]
    # The wheel's ci_gate carve-out is target-scoped, so the sdist needs its
    # own — without it the sdist ships what the wheel exists to withhold (D1).
    assert "src/no_human/ci_gate" in sdist["exclude"]
    # The employer half of the term inventory: forbidden in ANY artifact, and
    # vendor_terms.py carries an empty-fallback import for its absence.
    wheel = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]
    for target in (sdist, wheel):
        assert "src/no_human/eval/_vendor_terms_private.py" in target["exclude"]
        assert "**/*.swp" in target["exclude"]
    # The board rides via force-include so its absence fails the build loudly.
    assert sdist["force-include"] == {"web/dist": "web/dist"}


# Paths hatchling itself always adds to an sdist, plus the two roots we name.
# A member is allowed iff it is one of these files or under one of these
# directories — set membership, not pattern matching.
_ALLOWED_FILES = {"PKG-INFO", "pyproject.toml", "README.md", "LICENSE", ".gitignore"}
_ALLOWED_DIRS = ("src/no_human/", "web/dist/")


@pytest.mark.slow
def test_sdist_members_are_exactly_the_allowlist(tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH — cannot build the sdist")
    if not (REPO_ROOT / "web" / "dist" / "index.html").is_file():
        pytest.skip("web/dist is not built in this checkout")

    dist = tmp_path / "dist"
    build = subprocess.run(["uv", "build", "--sdist", "-o", str(dist)],
                           cwd=REPO_ROOT, capture_output=True, text=True,
                           timeout=600)
    assert build.returncode == 0, f"uv build failed:\n{build.stderr}"
    (sdist_path,) = dist.glob("*.tar.gz")

    with tarfile.open(sdist_path) as tar:
        members = [m.name.split("/", 1)[1] for m in tar.getmembers()
                   if m.isfile()]

    # Direction 1 — the leak direction: every member must be approved.
    strays = [m for m in members
              if m not in _ALLOWED_FILES
              and not any(m.startswith(d) for d in _ALLOWED_DIRS)]
    assert not strays, f"sdist ships files outside the allowlist: {strays[:10]}"

    # ci_gate is under an allowed dir, so prefix membership alone would let it
    # back in; it is excluded by name and must stay out.
    gated = [m for m in members if m.startswith("src/no_human/ci_gate")]
    assert not gated, f"sdist ships ci_gate: {gated[:5]}"

    # The private term inventory is under the allowed root, so prefix
    # membership alone would ship it; it is excluded by name and reviewed out
    # (2026-07-31: it was decoded straight out of a built sdist).
    assert "src/no_human/eval/_vendor_terms_private.py" not in members
    # Its public sibling must keep shipping — the empty-fallback design
    # depends on vendor_terms.py itself being present.
    assert "src/no_human/eval/vendor_terms.py" in members

    # Direction 2 — the sufficiency direction: an allowlist that drops the
    # product would also pass direction 1.
    assert "pyproject.toml" in members
    assert "PKG-INFO" in members
    assert "src/no_human/__init__.py" in members
    assert "web/dist/index.html" in members

    _assert_wheel_builds_from_sdist(sdist_path, tmp_path)


def _assert_wheel_builds_from_sdist(sdist_path: Path, tmp_path: Path) -> None:
    """The narrowed sdist must still be a sufficient wheel source.

    `pip install` falls back to exactly this path when it cannot use a wheel,
    so an sdist that satisfies the membership check but cannot build the
    product is a different shipping failure, not a success.
    """
    src = tmp_path / "from-sdist"
    src.mkdir()
    with tarfile.open(sdist_path) as tar:
        tar.extractall(src, filter="data")
    (proj,) = src.iterdir()

    out = tmp_path / "wheel-from-sdist"
    build = subprocess.run(["uv", "build", "--wheel", "-o", str(out)],
                           cwd=proj, capture_output=True, text=True,
                           timeout=600)
    assert build.returncode == 0, f"wheel from sdist failed:\n{build.stderr}"
    (wheel_path,) = out.glob("*.whl")

    names = zipfile.ZipFile(wheel_path).namelist()
    assert "no_human/web_dist/index.html" in names, (
        "the wheel built from the sdist lost the board")
    assert not [n for n in names if n.startswith("no_human/ci_gate")], (
        "the wheel built from the sdist ships ci_gate")
    assert "no_human/eval/_vendor_terms_private.py" not in names, (
        "the wheel built from the sdist ships the private term inventory")
    junk = [n for n in names if n.endswith((".swp", ".orig", ".rej"))]
    assert not junk, f"editor junk in the wheel: {junk}"

    # The planted-junk hole, tested hermetically: junk dropped into the
    # extracted sdist tree (which this test owns) must not reach the wheel.
    (proj / "src" / "no_human" / "core" / ".orchestrator.py.swp").write_text("junk")
    out2 = tmp_path / "wheel-with-junk"
    rebuild = subprocess.run(["uv", "build", "--wheel", "-o", str(out2)],
                             cwd=proj, capture_output=True, text=True,
                             timeout=600)
    assert rebuild.returncode == 0, rebuild.stderr
    (wheel2,) = out2.glob("*.whl")
    names2 = zipfile.ZipFile(wheel2).namelist()
    assert not [n for n in names2 if n.endswith(".swp")], (
        "a planted swap file under the allowed root reached the wheel")

    # And the guard the exclusion leans on: vendor_terms must import and keep
    # a non-empty term list with the private supplement ABSENT. A pure wheel
    # is zip-importable, so this runs against the artifact itself.
    probe = subprocess.run(
        [sys.executable, "-c",
         "import no_human.eval.vendor_terms as vt; "
         "assert vt.BANNED_TERMS, 'guard disarmed'; "
         "print('fallback-ok', len(vt.BANNED_TERMS))"],
        capture_output=True, text=True, timeout=120,
        env={"PYTHONPATH": str(wheel_path)},
    )
    assert probe.returncode == 0, (
        f"vendor_terms failed without the supplement:\n{probe.stderr}")
    assert "fallback-ok" in probe.stdout
