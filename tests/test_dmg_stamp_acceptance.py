"""Tests for packaging/check-update-stamp.sh and its wiring into make-dmg.sh.

Measured on the operator's installed 0.1.8: the DMG was signed, notarized
(externally, after the fact) and stapled — codesign/spctl/stapler all
passed — yet its app.asar carried nhSigning="signed-not-notarized" /
nhCanAutoUpdate=false baked in at build time, so every installer of that
release has "Download now" permanently disabled (only the Open-downloads
browser fallback works). None of the existing checks ask what the app
itself believes about being updatable; this is the gate that does.

Two layers, matching the file's two consumers:
  * the helper (`check-update-stamp.sh`) tested directly against synthetic
    fixture bundles — no real app build, no electron-builder, no asar tool;
  * the make-dmg.sh acceptance BLOCK, extracted and run for real against a
    stubbed `hdiutil` (reusing tests/test_verify_artefact.py's harness), so
    the wiring itself — not just the helper in isolation — is exercised.
    Per that file's header rule: assertions are on OBSERVED subprocess
    behaviour (stdout/stderr/rc), never on make-dmg.sh's source text.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_verify_artefact import (
    BOARD,
    GIT_ENV,
    SCRIPT,
    _digest,
    _dmg_verify_block,
    _q,
    _write_board,
    make_repo,
)

REPO = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = REPO / "packaging" / "check-update-stamp.sh"

# An asar is a binary container whose payload holds package.json as raw
# bytes; check-update-stamp.sh byte-scans, so a synthetic blob with the same
# bytes is a faithful fixture and needs no electron-builder/asar tool.
ASAR_HEADER = b"\x04\x00\x00\x00<not a real asar header>\x00"


def _bundle(tmp_path: Path, name: str, *, payload: bytes | None,
           extra: bytes = b"") -> Path:
    """An app bundle shaped like `<app>.app`, with a synthetic app.asar.

    `payload=None` builds a bundle with NO app.asar at all, for the
    missing/unreadable-input case.
    """
    app_dir = tmp_path / name
    res = app_dir / "Contents" / "Resources"
    res.mkdir(parents=True)
    if payload is not None:
        (res / "app.asar").write_bytes(ASAR_HEADER + payload + extra)
    return app_dir


def _run_check(app_dir: Path, *, env_extra: dict[str, str] | None = None
              ) -> subprocess.CompletedProcess:
    env = {**os.environ}
    env.update(env_extra or {})
    return subprocess.run(["bash", str(CHECK_SCRIPT), str(app_dir)],
                          capture_output=True, text=True, env=env)


STAMP_018 = b'"nhSigning":"signed-not-notarized","nhCanAutoUpdate":false'
STAMP_GOOD = b'"nhSigning":"signed","nhCanAutoUpdate":true'


# --------------------------------------------------------------------------- #
# the helper, in isolation
# --------------------------------------------------------------------------- #

def test_a_signed_not_notarized_stamp_is_refused(tmp_path):
    """The exact 0.1.8 bytes: signed but not notarized, update disabled."""
    app_dir = _bundle(tmp_path, "app018.app", payload=STAMP_018)
    proc = _run_check(app_dir)
    assert proc.returncode != 0
    assert "signed-not-notarized" in proc.stderr
    assert "false" in proc.stderr
    assert "APPLE_API_KEY" in proc.stderr
    assert "APPLE_KEYCHAIN_PROFILE" in proc.stderr
    assert "APPLE_APP_SPECIFIC_PASSWORD" in proc.stderr


def test_can_auto_update_false_alone_is_refused(tmp_path):
    """The two fields are ANDed, not aliased: nhSigning=signed is not enough."""
    app_dir = _bundle(
        tmp_path, "falseonly.app",
        payload=b'"nhSigning":"signed","nhCanAutoUpdate":false')
    proc = _run_check(app_dir)
    assert proc.returncode != 0


def test_a_missing_stamp_is_a_failure_not_a_pass(tmp_path):
    """Neither key present: fail-closed, not a silent pass."""
    app_dir = _bundle(tmp_path, "nostamp.app", payload=b"no stamp keys in here at all")
    proc = _run_check(app_dir)
    assert proc.returncode != 0
    assert "not found" in proc.stderr


def test_an_unreadable_or_absent_asar_is_a_failure(tmp_path):
    """No app.asar at all: distinct exit code from a 'wrong verdict', path named."""
    app_dir = _bundle(tmp_path, "noasar.app", payload=None)
    proc = _run_check(app_dir)
    assert proc.returncode == 2
    assert str(app_dir / "Contents" / "Resources" / "app.asar") in proc.stderr


def test_two_conflicting_values_are_refused(tmp_path):
    """A second, conflicting copy of the stamp is ambiguous, not a coin flip."""
    app_dir = _bundle(
        tmp_path, "conflict.app",
        payload=b'"nhSigning":"signed","nhCanAutoUpdate":true',
        extra=b' ... second copy ... "nhCanAutoUpdate":false')
    proc = _run_check(app_dir)
    assert proc.returncode != 0


def test_a_signed_and_updatable_stamp_passes(tmp_path):
    app_dir = _bundle(tmp_path, "good.app", payload=STAMP_GOOD)
    proc = _run_check(app_dir)
    assert proc.returncode == 0, proc.stderr


def test_a_signed_and_updatable_stamp_passes_with_whitespace(tmp_path):
    """Pins the regex tolerance: electron-builder's JSON formatting may vary."""
    app_dir = _bundle(
        tmp_path, "good-ws.app",
        payload=b'"nhSigning" : "signed" , "nhCanAutoUpdate" : true')
    proc = _run_check(app_dir)
    assert proc.returncode == 0, proc.stderr


def test_the_override_passes_but_shouts(tmp_path):
    app_dir = _bundle(tmp_path, "override.app", payload=STAMP_018)
    proc = _run_check(app_dir, env_extra={"NH_ALLOW_UNNOTARIZED": "1"})
    assert proc.returncode == 0, proc.stderr
    assert "MUST NOT SHIP" in proc.stderr
    assert "do not upload" in proc.stderr
    assert "signed-not-notarized" in proc.stderr


def test_the_override_requires_exactly_1(tmp_path):
    app_dir = _bundle(tmp_path, "override-bad.app", payload=STAMP_018)
    for bad_value in ("0", "yes", "true", ""):
        proc = _run_check(app_dir, env_extra={"NH_ALLOW_UNNOTARIZED": bad_value})
        assert proc.returncode != 0, (
            f"NH_ALLOW_UNNOTARIZED={bad_value!r} must not be an override")


# --------------------------------------------------------------------------- #
# wired into make-dmg.sh's acceptance block
# --------------------------------------------------------------------------- #

def _dmg_harness_with_stamp(tmp_path: Path, tag: str, *, asar_payload: bytes | None,
                            sign_mode: str = "unsigned"
                            ) -> subprocess.CompletedProcess:
    """`_dmg_harness` from tests/test_verify_artefact.py, plus an app.asar.

    Replicated rather than imported-and-reused because the upstream harness
    has no hook to drop an extra file at the stub mountpoint before running
    the block; everything else — the stub hdiutil, the real verify_artefact.py,
    the block extraction — is the same mechanism, on purpose, so this is
    exercising the ACTUAL acceptance block, not a reimplementation of it.
    """
    work = tmp_path / tag
    work.mkdir(parents=True)

    root, sha = make_repo(tmp_path, name=f"root-{tag}")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "verify_artefact.py").write_bytes(SCRIPT.read_bytes())
    venv = root / ".venv" / "bin"
    venv.mkdir(parents=True, exist_ok=True)
    if not (venv / "python").exists():
        (venv / "python").symlink_to(sys.executable)

    (root / "packaging").mkdir(exist_ok=True)
    (root / "packaging" / "check-update-stamp.sh").write_bytes(
        CHECK_SCRIPT.read_bytes())
    (root / "packaging" / "check-update-stamp.sh").chmod(0o755)

    mnt = work / "Volumes" / "no_human"
    app_root = mnt / "no_human.app"
    inner = app_root / "Contents" / "Resources" / "nh-server"
    _write_board(inner / "web" / "dist", BOARD)
    (inner / "BUILD_STAMP").write_text(
        f"commit={sha}\ndirty=no\nboard_sha256={_digest(BOARD)}\n")

    if asar_payload is not None:
        res = app_root / "Contents" / "Resources"
        res.mkdir(parents=True, exist_ok=True)
        (res / "app.asar").write_bytes(ASAR_HEADER + asar_payload)

    bindir = work / "bin"
    bindir.mkdir()
    (bindir / "hdiutil").write_text(
        '#!/bin/sh\n'
        'echo "HDIUTIL $*" >&2\n'
        'if [ "$1" = "attach" ]; then\n'
        '  printf \'/dev/disk9\\tApple_HFS\\t%s\\n\' "$NH_TEST_MNT"\n'
        'fi\n'
        'exit 0\n')
    (bindir / "hdiutil").chmod(0o755)

    text = _dmg_verify_block()
    script = work / "block.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f'ROOT={_q(root)}\nVOL="nh-test-{tag}"\nDMG={_q(work / "x.dmg")}\n'
        f'SIGN_MODE="{sign_mode}"\n'
        'allow_dirty=""\n'
        '[ "${SIGN_MODE}" = "signed" ] || allow_dirty="--allow-dirty"\n'
        + text + "\n")
    (work / "x.dmg").write_bytes(b"not really a dmg")

    env = {**os.environ, **GIT_ENV,
           "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
           "NH_TEST_MNT": str(mnt)}
    return subprocess.run(["bash", str(script)], capture_output=True, text=True,
                          env=env, cwd=str(work))


def test_make_dmg_acceptance_refuses_a_bad_stamp(tmp_path):
    """The acceptance block, not the helper directly, must decide the exit status."""
    proc = _dmg_harness_with_stamp(tmp_path, "stamp-bad", asar_payload=STAMP_018)
    assert proc.returncode == 1
    assert "update stamp" in proc.stderr


def test_make_dmg_acceptance_passes_a_good_stamp(tmp_path):
    """The control: a signed/true stamp does not block an otherwise-clean build."""
    proc = _dmg_harness_with_stamp(tmp_path, "stamp-good", asar_payload=STAMP_GOOD)
    assert proc.returncode == 0, proc.stderr
    assert "OK: " in proc.stdout


def test_make_dmg_detaches_before_failing_on_a_bad_stamp(tmp_path):
    """A stamp failure must not leave /Volumes/no_human mounted for the next build.

    Two distinct "FAIL:" messages appear in stderr: the helper's own (printed
    while still mounted, before it returns to make-dmg.sh) and make-dmg.sh's
    own decision-block message (printed after detach) — the marker below is
    unique to the latter, so this pins detach-before-DECISION specifically,
    not detach-before-the-helper-even-runs.
    """
    proc = _dmg_harness_with_stamp(tmp_path, "stamp-detach", asar_payload=STAMP_018)
    assert proc.returncode == 1
    assert "HDIUTIL detach /dev/disk9" in proc.stderr, (
        "the stamp-failure path never detached the volume it mounted")
    decision_marker = "check-update-stamp.sh rc="
    assert decision_marker in proc.stderr
    assert proc.stderr.index("HDIUTIL detach") < proc.stderr.index(decision_marker), (
        "the volume was still mounted when the pipeline decided to fail")
