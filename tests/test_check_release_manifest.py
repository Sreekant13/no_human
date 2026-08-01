"""`scripts/check_release_manifest.py` — the shipped file-inventory verifier.

RELEASE_MANIFEST.txt pins every released file's sha256 like a lockfile for the
tree, and this script is what a checkout of the release verifies itself with.
It must hold BOTH directions (a tracked file with no row, a row with no file)
and the pin itself (a row whose hash no longer matches), because each is a
different way a published tree stops being the reviewed tree.

Driven end to end through the CLI on throwaway repos, never on this repository:
in the working repo the tree legitimately differs from a release, which is why
the CI job that runs the script is scoped to the released tree. These tests
must pass identically in the private repo and in the export.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(
    subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent, text=True,
    ).strip()
)
SCRIPT = REPO_ROOT / "scripts" / "check_release_manifest.py"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "README.md").write_text("hello\n")
    (repo / "pkg" / "a.py").write_text("A = 1\n")
    subprocess.check_call(["git", "init", "--quiet"], cwd=str(repo))
    subprocess.check_call(["git", "add", "-A"], cwd=str(repo))
    return repo


def write_manifest(repo: Path) -> None:
    proc = run("--root", str(repo), "--write")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_matching_tree_passes(tmp_path):
    repo = make_repo(tmp_path)
    write_manifest(repo)
    proc = run("--root", str(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK: 2 file(s) match" in proc.stdout


def test_a_changed_file_fails_on_its_hash(tmp_path):
    """The content pin. Same path, same row, different bytes: the row must be
    regenerated (and therefore reviewed in a diff) or the check stays red."""
    repo = make_repo(tmp_path)
    write_manifest(repo)
    (repo / "pkg" / "a.py").write_text("A = 2\n")
    proc = run("--root", str(repo))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "pkg/a.py: content differs from the manifest" in proc.stderr


def test_a_tracked_file_missing_from_the_manifest_warns_and_fails_under_strict(
    tmp_path,
):
    """Split from "fails" (2026-08-02), and the reason is the exit code's signal.

    An unlisted path and a hash mismatch used to share exit 1. In this working
    repository ~93 paths are legitimately unlisted, so the script was ALREADY 1
    before any change: a deliberately bad merge resolution produced a real
    mismatch and the exit code did not move. Only reading the body revealed it,
    which is not a gate.

    So the classes are separated, and BOTH halves are pinned here: the unlisted
    path is still reported by name (it is not being hidden), it no longer sets
    the exit code by itself, and `--strict` — what CI runs on the released tree,
    where completeness is a hard invariant — still fails on it exactly as
    before.
    """
    repo = make_repo(tmp_path)
    write_manifest(repo)
    (repo / "pkg" / "new.py").write_text("N = 1\n")
    subprocess.check_call(["git", "add", "pkg/new.py"], cwd=str(repo))

    proc = run("--root", str(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "pkg/new.py: tracked but not listed" in proc.stderr
    assert "WARN" in proc.stderr

    strict = run("--root", str(repo), "--strict")
    assert strict.returncode == 1, strict.stdout + strict.stderr
    assert "pkg/new.py: tracked but not listed" in strict.stderr


def test_a_hash_mismatch_still_fails_when_unlisted_paths_are_present(tmp_path):
    """The defect this split exists to fix, pinned as a test.

    An unlisted path and a changed file at the same time: before the split both
    produced exit 1 and the mismatch was invisible to any automated caller. The
    unlisted path must now warn, the mismatch must still fail, and the mismatch
    must be the thing that decides the exit code.
    """
    repo = make_repo(tmp_path)
    write_manifest(repo)
    # An unlisted path — on its own, only a warning.
    (repo / "pkg" / "new.py").write_text("N = 1\n")
    subprocess.check_call(["git", "add", "pkg/new.py"], cwd=str(repo))
    assert run("--root", str(repo)).returncode == 0

    # ...and now a real mismatch on top of it.
    (repo / "pkg" / "a.py").write_text("A = 2\n")
    proc = run("--root", str(repo))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "pkg/a.py: content differs from the manifest" in proc.stderr
    assert "pkg/new.py: tracked but not listed" in proc.stderr


def test_a_listed_file_missing_from_the_tree_fails(tmp_path):
    repo = make_repo(tmp_path)
    write_manifest(repo)
    # -f: the fixture stages without committing, and `git rm` refuses a
    # staged-only file otherwise.
    subprocess.check_call(["git", "rm", "--quiet", "-f", "pkg/a.py"], cwd=str(repo))
    proc = run("--root", str(repo))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "pkg/a.py: listed but not in the tree" in proc.stderr


def test_a_manifest_listing_itself_fails(tmp_path):
    """It cannot pin its own content — a self-row is always stale or always
    wrong, and either way it teaches readers to expect a mismatch."""
    repo = make_repo(tmp_path)
    write_manifest(repo)
    manifest = repo / "RELEASE_MANIFEST.txt"
    manifest.write_text(manifest.read_text()
                        + "0" * 64 + "  RELEASE_MANIFEST.txt\n")
    proc = run("--root", str(repo))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "lists itself" in proc.stderr


def test_a_malformed_row_is_an_error_not_a_skip(tmp_path):
    """A row the parser cannot read is a pin that silently stopped pinning."""
    repo = make_repo(tmp_path)
    write_manifest(repo)
    manifest = repo / "RELEASE_MANIFEST.txt"
    manifest.write_text(manifest.read_text() + "not-a-hash  pkg/a.py2\n")
    proc = run("--root", str(repo))
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "cannot parse" in proc.stderr


def test_write_omits_the_manifest_itself_and_sorts_rows(tmp_path):
    repo = make_repo(tmp_path)
    write_manifest(repo)
    subprocess.check_call(["git", "add", "RELEASE_MANIFEST.txt"], cwd=str(repo))
    write_manifest(repo)   # second pass, with the manifest now tracked
    rows = [l for l in (repo / "RELEASE_MANIFEST.txt").read_text().splitlines()
            if l and not l.startswith("#")]
    paths = [r.split("  ", 1)[1] for r in rows]
    assert "RELEASE_MANIFEST.txt" not in paths
    assert paths == sorted(paths)
    proc = run("--root", str(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr
