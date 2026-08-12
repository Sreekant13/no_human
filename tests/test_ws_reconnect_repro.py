"""Repro proof for the 2026-08-12 board-websocket-reconnect incident.

Live incident: the operator's board tab showed two DONE tasks stuck in
'review pr' after a server restart killed the websocket — the SPA kept
rendering its last init snapshot with no reconnect and no re-fetch. The fix
lives entirely in ``web/src/wsReconnect.js`` (exponential backoff, snapshot
re-fetch on reconnect, wholesale state replacement) and
``web/src/connectionBanner.js`` (the stale-data banner), proved by
``web/src/wsReconnect.test.mjs`` and ``web/src/connectionBanner.test.mjs``.

This repo's declared profile is python-pytest, so the reproduction-test gate
(``.no_human/repro_tests.json``) runs pytest node ids, not node ids of its
own — the established way to give the JS-side behaviour a pytest identity is
the same one ``test_lane_conformance.py::test_the_js_implementation_agrees_
on_every_shared_case`` already uses: shell out to ``node --test`` and require
success.

That also makes this an honest fails-before/passes-after proof of the fix
existing at all, not just of one assertion: on the pre-fix tree
``web/src/wsReconnect.js`` and its test do not exist, so ``node --test``
cannot even find the file it's told to run and exits non-zero — the sharpest
possible "fails before" for a capability introduced from nothing.

Node absence FAILS rather than skips, same reasoning as
test_lane_conformance.py: a skip here is indistinguishable from a pass, and
node is already a hard requirement of this repo's web test story.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "web"


def test_ws_reconnect_and_stale_banner_js_suite_passes():
    node = shutil.which("node")
    assert node is not None, (
        "node is not on PATH, so the websocket-reconnect fix cannot be "
        "verified; this suite deliberately fails rather than skips."
    )
    proc = subprocess.run(
        [node, "--test", "src/wsReconnect.test.mjs", "src/connectionBanner.test.mjs"],
        cwd=WEB_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "web/src/wsReconnect.js (exponential backoff + full-snapshot replace "
        "on reconnect) and/or web/src/connectionBanner.js (the stale-data "
        "banner) failed their own node --test suite, or the files are "
        "missing entirely (the incident's fix is absent):\n"
        f"STDOUT:\n{proc.stdout[-4000:]}\nSTDERR:\n{proc.stderr[-2000:]}"
    )
