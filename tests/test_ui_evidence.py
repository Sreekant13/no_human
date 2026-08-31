"""Tests for no_human.testing.ui_evidence.

Two of these tests exist specifically because a prior attempt at this exact
module failed independent review on real Playwright API-signature bugs:

- test_run_press_dispatches_via_keyboard_not_page_press — a "press" step
  must call `page.keyboard.press(key)`, never `page.press(selector, key)`;
  the fake page here has no `press` method at all, so a regression raises
  AttributeError and the step (and therefore the whole run) fails.
- test_manifest_problem_goto_protocol_relative_rejected — a goto value like
  `//evil.com/x` has an empty scheme but a non-empty netloc; it must be
  rejected by the local-host guard rather than silently passing validation
  and later resolving to a remote host via urljoin at runtime.
"""

from __future__ import annotations

import json
import os
import struct

import pytest

from no_human.testing import ui_evidence

# ---------------------------------------------------------------------------
# Fixtures / fake Playwright harness
# ---------------------------------------------------------------------------

VALID = {
    "base_url": "http://127.0.0.1:5173",
    "steps": [
        {"goto": "/"},
        {"wait_for": "#app"},
        {"shot": "loaded"},
        {"click": "#btn"},
        {"fill": "#input", "text": "hello"},
        {"press": "Enter"},
        {"assert_text": "Success"},
    ],
}

# A structurally-real (if not fully spec-legal) 1x1 PNG: signature + a chunk
# header shaped exactly like IHDR so `_png_size` reads width/height back out
# without needing a real image library.
PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\x0d"
    + b"IHDR"
    + struct.pack(">II", 1, 1)
    + b"\x08\x06\x00\x00\x00"
    + b"\x00\x00\x00\x00"
    + b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def write_manifest(repo, data) -> None:
    d = repo / ".no_human"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ui_evidence.json").write_text(json.dumps(data))


class FakeLocator:
    def __init__(self, found: bool):
        self._found = found

    async def count(self):
        return 1 if self._found else 0


class FakeContext:
    def __init__(self, video_out=None):
        self.closed = False
        self.video_out = video_out

    async def close(self):
        self.closed = True
        if self.video_out is not None:
            (self.video_out / "abc123.webm").write_bytes(b"FAKEVIDEO")


class FakeKeyboard:
    def __init__(self):
        self.pressed = []

    def press(self, key):
        self.pressed.append(key)


class ConsoleMsg:
    def __init__(self, type_, text):
        self.type = type_
        self.text = text


class FakePage:
    """No `press` method on purpose — see module docstring."""

    def __init__(
        self, *, fail_action=None, found_text=True, console_msgs=None,
        sleep_s=0.0, video_out=None,
    ):
        self.context = FakeContext(video_out=video_out)
        self.keyboard = FakeKeyboard()
        self.calls = []
        self.fail_action = fail_action
        self.found_text = found_text
        self.console_msgs = console_msgs or []
        self.sleep_s = sleep_s
        self.screenshot_count = 0

    def on(self, event, handler):
        if event == "console":
            for msg in self.console_msgs:
                handler(msg)

    async def _maybe_sleep(self):
        if self.sleep_s:
            import asyncio

            await asyncio.sleep(self.sleep_s)

    async def goto(self, url):
        self.calls.append(("goto", url))
        await self._maybe_sleep()
        if self.fail_action == "goto":
            raise RuntimeError("boom")

    async def wait_for_selector(self, sel):
        self.calls.append(("wait_for", sel))
        await self._maybe_sleep()
        if self.fail_action == "wait_for":
            raise RuntimeError("boom")

    async def click(self, sel):
        self.calls.append(("click", sel))
        await self._maybe_sleep()
        if self.fail_action == "click":
            raise RuntimeError("boom")

    async def fill(self, sel, text):
        self.calls.append(("fill", sel, text))
        await self._maybe_sleep()

    def locator(self, text_sel):
        self.calls.append(("locator", text_sel))
        return FakeLocator(self.found_text)

    async def screenshot(self):
        self.screenshot_count += 1
        return PNG_1x1


def make_launch(page):
    async def _launch(out_dir, viewport):
        return page

    return _launch


# ---------------------------------------------------------------------------
# Validation — manifest_problem
# ---------------------------------------------------------------------------


def test_manifest_problem_none_when_absent(tmp_path):
    assert ui_evidence.manifest_problem(tmp_path) is None


def test_manifest_problem_none_for_valid(tmp_path):
    write_manifest(tmp_path, VALID)
    assert ui_evidence.manifest_problem(tmp_path) is None


def test_manifest_problem_invalid_json(tmp_path):
    d = tmp_path / ".no_human"
    d.mkdir(parents=True)
    (d / "ui_evidence.json").write_text("{not json")
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "not valid JSON" in problem


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits do not restrict root or Windows",
)
def test_manifest_problem_unreadable(tmp_path):
    d = tmp_path / ".no_human"
    d.mkdir(parents=True)
    f = d / "ui_evidence.json"
    f.write_text("{}")
    f.chmod(0o000)
    try:
        problem = ui_evidence.manifest_problem(tmp_path)
        assert problem is not None and "unreadable" in problem
    finally:
        f.chmod(0o644)


def test_manifest_problem_top_level_not_object(tmp_path):
    write_manifest(tmp_path, [1, 2, 3])
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "not an object" in problem


def test_manifest_problem_base_url_missing(tmp_path):
    data = {k: v for k, v in VALID.items() if k != "base_url"}
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "base_url" in problem


def test_manifest_problem_base_url_not_http(tmp_path):
    data = dict(VALID, base_url="ftp://127.0.0.1/")
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "http(s)" in problem


def test_manifest_problem_base_url_remote_host(tmp_path):
    data = dict(VALID, base_url="http://evil.com/")
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "127.0.0.1 or localhost" in problem


def test_manifest_problem_base_url_userinfo_trick(tmp_path):
    # hostname resolves to evil.com even though the string LOOKS local.
    data = dict(VALID, base_url="http://127.0.0.1@evil.com/")
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "127.0.0.1 or localhost" in problem


def test_manifest_problem_viewport_not_dict(tmp_path):
    data = dict(VALID, viewport="big")
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "viewport" in problem


def test_manifest_problem_viewport_bad_width(tmp_path):
    data = dict(VALID, viewport={"width": -1, "height": 800})
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "viewport.width" in problem


def test_manifest_problem_steps_missing(tmp_path):
    data = {k: v for k, v in VALID.items() if k != "steps"}
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "steps" in problem


def test_manifest_problem_steps_empty(tmp_path):
    data = dict(VALID, steps=[])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "empty" in problem


def test_manifest_problem_steps_too_many(tmp_path):
    data = dict(VALID, steps=[{"wait_for": "#x"}] * 41)
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "41" in problem


def test_manifest_problem_step_not_object(tmp_path):
    data = dict(VALID, steps=["goto:/"])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "step 0" in problem


def test_manifest_problem_step_unknown_key(tmp_path):
    data = dict(VALID, steps=[{"goto": "/", "bogus": 1}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "unknown key" in problem


def test_manifest_problem_step_no_action(tmp_path):
    data = dict(VALID, steps=[{"timeout_ms": 100}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "no action key" in problem


def test_manifest_problem_step_multiple_actions(tmp_path):
    data = dict(VALID, steps=[{"goto": "/", "click": "#x"}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "multiple action keys" in problem


def test_manifest_problem_step_value_empty(tmp_path):
    data = dict(VALID, steps=[{"goto": ""}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "non-empty string" in problem


def test_manifest_problem_fill_without_text(tmp_path):
    data = dict(VALID, steps=[{"fill": "#input"}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "fill" in problem and "text" in problem


def test_manifest_problem_text_on_non_fill(tmp_path):
    data = dict(VALID, steps=[{"click": "#btn", "text": "hi"}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "only valid on a" in problem


def test_manifest_problem_goto_protocol_relative_rejected(tmp_path):
    """The prior-attempt review finding: //evil.com/x must be rejected."""
    data = dict(VALID, steps=[{"goto": "//evil.com/x"}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None
    assert "127.0.0.1 or localhost" in problem


def test_manifest_problem_goto_relative_allowed(tmp_path):
    data = dict(VALID, steps=[{"goto": "/dashboard"}])
    write_manifest(tmp_path, data)
    assert ui_evidence.manifest_problem(tmp_path) is None


def test_manifest_problem_goto_absolute_remote_rejected(tmp_path):
    data = dict(VALID, steps=[{"goto": "http://evil.com/x"}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "127.0.0.1 or localhost" in problem


def test_manifest_problem_goto_absolute_local_allowed(tmp_path):
    data = dict(VALID, steps=[{"goto": "http://127.0.0.1:5173/x"}])
    write_manifest(tmp_path, data)
    assert ui_evidence.manifest_problem(tmp_path) is None


def test_manifest_problem_shot_bad_name(tmp_path):
    data = dict(VALID, steps=[{"shot": "Not_Valid!"}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "shot" in problem


def test_manifest_problem_shot_reserved_final(tmp_path):
    data = dict(VALID, steps=[{"shot": "final"}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "reserved" in problem


def test_manifest_problem_shot_duplicate(tmp_path):
    data = dict(VALID, steps=[{"shot": "a"}, {"shot": "a"}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "duplicates" in problem


def test_manifest_problem_timeout_bool_rejected(tmp_path):
    data = dict(VALID, steps=[{"goto": "/", "timeout_ms": True}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "timeout_ms" in problem


def test_manifest_problem_timeout_out_of_range(tmp_path):
    data = dict(VALID, steps=[{"goto": "/", "timeout_ms": 999999}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "timeout_ms" in problem


def test_manifest_problem_too_many_shots(tmp_path):
    steps = [{"shot": f"s{i}"} for i in range(13)]
    data = dict(VALID, steps=steps)
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    assert problem is not None and "13" in problem


# ---------------------------------------------------------------------------
# API — read_manifest
# ---------------------------------------------------------------------------


def test_read_manifest_raises_when_absent(tmp_path):
    with pytest.raises(ValueError, match="no .*manifest"):
        ui_evidence.read_manifest(tmp_path)


def test_read_manifest_raises_with_problem_message(tmp_path):
    data = dict(VALID, base_url="http://evil.com/")
    write_manifest(tmp_path, data)
    with pytest.raises(ValueError, match="127.0.0.1 or localhost"):
        ui_evidence.read_manifest(tmp_path)


def test_read_manifest_returns_parsed_manifest(tmp_path):
    write_manifest(tmp_path, VALID)
    manifest = ui_evidence.read_manifest(tmp_path)
    assert manifest.base_url == VALID["base_url"]
    assert manifest.viewport == {"width": 1280, "height": 800}
    assert len(manifest.steps) == len(VALID["steps"])
    assert manifest.steps[0].action == "goto"
    assert manifest.steps[0].value == "/"
    assert manifest.steps[0].index == 0


def test_manifest_problem_and_read_manifest_agree(tmp_path):
    data = dict(VALID, steps=[{"shot": "final"}])
    write_manifest(tmp_path, data)
    problem = ui_evidence.manifest_problem(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        ui_evidence.read_manifest(tmp_path)
    assert str(excinfo.value) == problem


# ---------------------------------------------------------------------------
# Happy path — run()
# ---------------------------------------------------------------------------


async def test_run_happy_path_ran_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    write_manifest(tmp_path, VALID)
    out_dir = tmp_path / "evidence"
    page = FakePage()
    result = await ui_evidence.run(tmp_path, out_dir, launch=make_launch(page))
    assert result.verdict == "ran"
    assert result.steps_run == result.steps_total == len(VALID["steps"])
    assert result.reason == ""


async def test_run_records_shots_with_png_dims(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    write_manifest(tmp_path, VALID)
    out_dir = tmp_path / "evidence"
    page = FakePage()
    result = await ui_evidence.run(tmp_path, out_dir, launch=make_launch(page))
    named = [s for s in result.shots if s["name"] == "loaded"]
    assert len(named) == 1
    shot = named[0]
    assert shot["width"] == 1 and shot["height"] == 1
    assert shot["path"] == "loaded.png"
    assert (out_dir / "loaded.png").read_bytes() == PNG_1x1
    assert len(shot["sha256"]) == 64


async def test_run_press_dispatches_via_keyboard_not_page_press(tmp_path, monkeypatch):
    """Regression test for the prior review finding — press signature bug."""
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    data = dict(VALID, steps=[{"goto": "/"}, {"press": "Enter"}])
    write_manifest(tmp_path, data)
    out_dir = tmp_path / "evidence"
    page = FakePage()
    assert not hasattr(page, "press")
    result = await ui_evidence.run(tmp_path, out_dir, launch=make_launch(page))
    assert result.verdict == "ran"
    assert page.keyboard.pressed == ["Enter"]


async def test_run_final_shot_always_taken(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    write_manifest(tmp_path, VALID)
    out_dir = tmp_path / "evidence"
    page = FakePage()
    result = await ui_evidence.run(tmp_path, out_dir, launch=make_launch(page))
    names = [s["name"] for s in result.shots]
    assert names[-1] == "final"
    assert (out_dir / "final.png").exists()


async def test_run_video_renamed_to_walk_webm(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    write_manifest(tmp_path, VALID)
    out_dir = tmp_path / "evidence"
    out_dir.mkdir(parents=True)
    page = FakePage(video_out=out_dir)
    result = await ui_evidence.run(tmp_path, out_dir, launch=make_launch(page))
    assert result.video == "walk.webm"
    assert (out_dir / "walk.webm").exists()
    assert (out_dir / "walk.webm").read_bytes() == b"FAKEVIDEO"


# ---------------------------------------------------------------------------
# Failure paths — run()
# ---------------------------------------------------------------------------


async def test_run_step_failure_sets_failed_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    data = dict(VALID, steps=[{"goto": "/"}, {"click": "#missing"}])
    write_manifest(tmp_path, data)
    out_dir = tmp_path / "evidence"
    page = FakePage(fail_action="click")
    result = await ui_evidence.run(tmp_path, out_dir, launch=make_launch(page))
    assert result.verdict == "failed"
    assert result.failed_step == 1
    assert result.failed_action == "click"
    assert "step 1 (click)" in result.reason


async def test_run_step_failure_still_takes_final_shot(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    data = dict(VALID, steps=[{"goto": "/"}, {"click": "#missing"}])
    write_manifest(tmp_path, data)
    out_dir = tmp_path / "evidence"
    page = FakePage(fail_action="click")
    result = await ui_evidence.run(tmp_path, out_dir, launch=make_launch(page))
    assert any(s["name"] == "final" for s in result.shots)


async def test_run_deadline_exceeded_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    write_manifest(tmp_path, VALID)
    out_dir = tmp_path / "evidence"
    page = FakePage()
    result = await ui_evidence.run(
        tmp_path, out_dir, deadline_s=0.0, launch=make_launch(page)
    )
    assert result.verdict == "failed"
    assert result.failed_step == 0
    assert "deadline" in result.reason


async def test_run_step_timeout_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    data = dict(VALID, steps=[
        {"goto": "/"},
        {"wait_for": "#slow", "timeout_ms": 10},
    ])
    write_manifest(tmp_path, data)
    out_dir = tmp_path / "evidence"
    page = FakePage(sleep_s=0.3)
    result = await ui_evidence.run(
        tmp_path, out_dir, deadline_s=30.0, launch=make_launch(page)
    )
    assert result.verdict == "failed"
    assert result.failed_action == "wait_for"
    assert "timed out after 10ms" in result.reason


# ---------------------------------------------------------------------------
# not_run states — run()
# ---------------------------------------------------------------------------


async def test_run_not_run_when_manifest_absent(tmp_path):
    out_dir = tmp_path / "evidence"
    result = await ui_evidence.run(tmp_path, out_dir)
    assert result.verdict == "not_run"
    assert "no manifest" in result.reason


async def test_run_not_run_when_manifest_invalid(tmp_path):
    data = dict(VALID, base_url="http://evil.com/")
    write_manifest(tmp_path, data)
    out_dir = tmp_path / "evidence"
    result = await ui_evidence.run(tmp_path, out_dir)
    assert result.verdict == "not_run"
    assert "127.0.0.1 or localhost" in result.reason


async def test_run_not_run_when_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: False)
    write_manifest(tmp_path, VALID)
    out_dir = tmp_path / "evidence"
    result = await ui_evidence.run(tmp_path, out_dir)
    assert result.verdict == "not_run"
    assert "not reachable" in result.reason


async def test_run_not_run_when_playwright_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    monkeypatch.setattr(ui_evidence, "_import_playwright", lambda: None)
    write_manifest(tmp_path, VALID)
    out_dir = tmp_path / "evidence"
    result = await ui_evidence.run(tmp_path, out_dir)
    assert result.verdict == "not_run"
    assert "playwright not installed" in result.reason


async def test_run_not_run_when_launch_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    write_manifest(tmp_path, VALID)
    out_dir = tmp_path / "evidence"

    async def _bad_launch(out_dir, viewport):
        raise RuntimeError("no browser binary")

    result = await ui_evidence.run(tmp_path, out_dir, launch=_bad_launch)
    assert result.verdict == "not_run"
    assert "browser launch failed" in result.reason


# ---------------------------------------------------------------------------
# Console capture, summary_line, serialisation
# ---------------------------------------------------------------------------


async def test_run_captures_console_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    write_manifest(tmp_path, VALID)
    out_dir = tmp_path / "evidence"
    msgs = [ConsoleMsg("error", "boom 1"), ConsoleMsg("log", "ignored"), ConsoleMsg("error", "boom 2")]
    page = FakePage(console_msgs=msgs)
    result = await ui_evidence.run(tmp_path, out_dir, launch=make_launch(page))
    assert result.console_errors == ["boom 1", "boom 2"]


def test_summary_line_not_run():
    result = ui_evidence.UiEvidenceResult(verdict="not_run", reason="no manifest")
    assert ui_evidence.summary_line(result) == "NOT RUN — no manifest"


def test_summary_line_ran():
    result = ui_evidence.UiEvidenceResult(
        verdict="ran", shots=[{"name": "a"}], video="walk.webm", console_errors=[]
    )
    line = ui_evidence.summary_line(result)
    assert line == "ran — 1 screenshot, video, 0 console errors"


def test_summary_line_failed():
    result = ui_evidence.UiEvidenceResult(
        verdict="failed", shots=[{"name": "final"}], failed_step=2, failed_action="click",
    )
    line = ui_evidence.summary_line(result)
    assert line == "failed at step 2 (click) — 1 screenshot"


def test_as_dict_json_serialisable():
    result = ui_evidence.UiEvidenceResult(
        verdict="ran", shots=[{"name": "a", "sha256": "x" * 64}],
        video="walk.webm", console_errors=["oops"], steps_run=3, steps_total=3,
        duration_s=1.23456, manifest={"base_url": "http://127.0.0.1:1"},
    )
    encoded = json.dumps(result.as_dict())
    decoded = json.loads(encoded)
    assert decoded["verdict"] == "ran"
    assert decoded["duration_s"] == 1.235
    assert decoded["shots"][0]["name"] == "a"


async def test_run_writes_result_json_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    write_manifest(tmp_path, VALID)
    out_dir = tmp_path / "evidence"
    page = FakePage()
    result = await ui_evidence.run(tmp_path, out_dir, launch=make_launch(page))
    on_disk = json.loads((out_dir / "result.json").read_text())
    assert on_disk["verdict"] == result.verdict
    assert on_disk["steps_run"] == result.steps_run
    manifest_on_disk = json.loads((out_dir / "manifest.json").read_text())
    assert manifest_on_disk["base_url"] == VALID["base_url"]
    console_on_disk = json.loads((out_dir / "console.json").read_text())
    assert console_on_disk == {"errors": []}


# ─────────────────── D1.2: out_dir plumbing (default_out_dir) ─────────────── #


def test_default_out_dir_is_fresh_writable_and_named_from_task_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d1 = ui_evidence.default_out_dir("deadbeef12345678")
    d2 = ui_evidence.default_out_dir("deadbeef12345678")
    assert d1.is_dir() and d2.is_dir()
    assert d1 != d2, "two calls for the same task must not collide"
    assert "deadbeef" in d1.name
    (d1 / "probe.txt").write_text("ok")
    assert (d1 / "probe.txt").read_text() == "ok"


async def test_default_out_dir_works_as_runs_out_dir(tmp_path, monkeypatch):
    """`default_out_dir` is a convenience, not a requirement `run()` enforces
    — a caller passing it through must see exactly the same behavior as any
    other `out_dir`."""
    monkeypatch.setattr(ui_evidence, "_reachable", lambda url: True)
    write_manifest(tmp_path, VALID)
    out_dir = ui_evidence.default_out_dir("cafebabe00000000")
    page = FakePage()
    result = await ui_evidence.run(tmp_path, out_dir, launch=make_launch(page))
    assert result.verdict == "ran"
    assert (out_dir / "result.json").is_file()


# ─────────────── the readiness probe never follows a redirect ─────────────── #


def test_reachable_does_not_follow_a_redirect_off_the_probed_server():
    """A dev server that answers `302 Location: <elsewhere>` is still a running
    server — `_reachable` must say True — but the probe must NOT follow the
    redirect: that is the one way a loopback-validated base_url could make
    this process talk to another host. Two loopback servers: the probed one
    redirects to the second; the second must see zero requests."""
    import http.server
    import socketserver
    import threading

    hits = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):  # silence
            pass

    target = socketserver.TCPServer(("127.0.0.1", 0), Target)
    target_port = target.server_address[1]

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target_port}/elsewhere")
            self.end_headers()

        def log_message(self, *a):
            pass

    redirector = socketserver.TCPServer(("127.0.0.1", 0), Redirector)
    threads = [threading.Thread(target=srv.serve_forever, daemon=True)
               for srv in (target, redirector)]
    for t in threads:
        t.start()
    try:
        assert ui_evidence._reachable(
            f"http://127.0.0.1:{redirector.server_address[1]}/") is True
        assert hits == [], f"the probe followed the redirect: {hits}"
    finally:
        for srv in (target, redirector):
            srv.shutdown()
            srv.server_close()
