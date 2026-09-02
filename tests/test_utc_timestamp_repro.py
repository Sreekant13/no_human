"""Repro proof for the 2026-09-01 web-UI UTC-timestamp-skew incident.

Live incident: the web UI's "time ago" readouts and the Stats tasks/day span
read wrong for any viewer whose local timezone is not UTC — a task created 40
minutes ago showed "3h ago" for a viewer in Israel (UTC+3). Root cause: the
server writes SQLite ``datetime('now')`` timestamps as naive UTC strings (no
zone marker, e.g. "2026-09-01 12:00:00"), and ``web/src`` parsed them with
plain ``new Date(s)`` / ``Date.parse(s)`` — which the ES2015+ spec defines as
LOCAL time for a zone-free date-time string, not UTC. The fix lives entirely
in ``web/src/parseTimestamp.js`` (the one shared helper every call site in
``web/src`` now routes through instead of calling ``new Date``/``Date.parse``
directly on a raw API value), proved by ``web/src/parseTimestamp.test.mjs``.

This repo's declared profile is python-pytest, so the reproduction-test gate
(``.no_human/repro_tests.json``) runs pytest node ids, not node ids of its
own — the established way to give the JS-side behaviour a pytest identity is
the same one ``tests/test_ws_reconnect_repro.py`` uses: shell out to
``node --test`` and require success.

Run under ``TZ=Asia/Jerusalem`` (UTC+3, the offset from the live incident) so
this is an honest fails-before/passes-after proof, not just of one assertion:
on the pre-fix tree ``web/src/parseTimestamp.js`` and its test do not exist,
so ``node --test`` cannot even find the file it's told to run and exits
non-zero. On a tree where the file exists but still calls ``new Date(naive)``
directly, the "a naive DB timestamp parses as UTC, not local" and "the
40-minute-old task reads 40m, not 3h" assertions in
``parseTimestamp.test.mjs`` fail specifically under this non-UTC TZ (they
pass trivially under TZ=UTC, which is why the TZ pin here matters) — the
sharpest possible "fails before" for a timezone-dependent bug.

Node absence FAILS rather than skips, same reasoning as
test_ws_reconnect_repro.py: a skip here is indistinguishable from a pass, and
node is already a hard requirement of this repo's web test story.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "web"


def test_web_timestamps_parse_as_utc_under_a_shifted_tz():
    node = shutil.which("node")
    assert node is not None, (
        "node is not on PATH, so the UTC-timestamp-parsing fix cannot be "
        "verified; this suite deliberately fails rather than skips."
    )
    env = dict(os.environ)
    env["TZ"] = "Asia/Jerusalem"
    proc = subprocess.run(
        [node, "--test", "src/parseTimestamp.test.mjs"],
        cwd=WEB_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, (
        "web/src/parseTimestamp.js (the shared UTC-aware timestamp parser) "
        "failed its own node --test suite under TZ=Asia/Jerusalem, or the "
        "file is missing entirely (the incident's fix is absent):\n"
        f"STDOUT:\n{proc.stdout[-4000:]}\nSTDERR:\n{proc.stderr[-2000:]}"
    )
