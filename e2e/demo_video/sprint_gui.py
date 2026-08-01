"""Record the React board for the narrated sprint demo, one frame at a time.

Same harness discipline as ``gui_frames.py`` (the 26 s cut's recorder): the
built SPA is served over plain HTTP, every ``**/api/**`` request is answered
from ``sprint.py`` at the frame's own timestamp, the board's WebSocket and
EventSource are the same recorder-driven shims, and the world advances one
frame at a time — wall time never enters the output.

Every scripted step's time is derived from ``narration.py``: the recorder
WAITS ON THE TIMING MAP (the step fires when the frame clock reaches the
beat, and the fixture's status changes are cut to the same map), so the
narrated line and the screen can never race each other, in either direction.

    PYTHONPATH=src python -m e2e.demo_video.sprint_gui /tmp/nh-sprint-demo/frames-gui

What the clip shows, beat by beat (see narration.BEATS for the words):

    hook      the quiet board — one leftover PR in Review
    handoff   five cards land in Working, Jira keys in the titles; the lane
              collapses at four, and the script clicks "Show 1 more" exactly
              the way a person would — five tickets is the claim, five cards
              is the proof
    parallel  statuses tick on every card; the drawer opens on the webhook
              fix: its Spec (the plan), then the live System lanes
    gate      the Diff section, then the Review checklist with its cited
              file:line evidence; behind the drawer the refactor bounces
    payoff    the drawer closes on five cards in Review with PR links, then
              reopens on the hero and Approve is clicked
    close     Escape; the whole board holds to the fade
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from . import narration as nr
from . import sprint as sp
from .gui_frames import (DIST, PIN_BY_BEAT, PIN_SECTION, SSE_SHIM, WS_SHIM,
                         serve_dist, settle)

#: Capture geometry is gui_frames' — same viewport, same device scale, so the
#: sprint clip and the 26 s clip stay interchangeable on the site.
from .gui_frames import DSF, VIEW_H, VIEW_W  # noqa: F401  (re-exported)

# --------------------------------------------------------------------------- #
# The schedule — every number here is anchored to a narration beat             #
# --------------------------------------------------------------------------- #

#: Click "Show 1 more" in the Working lane once all five cards exist. The
#: last ticket is created at 14.40; this is after it and before the parallel
#: beat's first line ("Five agents start") needs five visible cards.
EXPAND_LANE_AT = 15.60

#: The drawer, on the hero. Opens while the plan line is being spoken
#: (21.60-28.25), holds through System / Diff / Review, closes for the wide
#: shot the refactor-bounce and five-PR lines are spoken over, reopens for
#: the Approve click, closes again for the close.
#:
#: DIFF_AT is after the trail's `commit` frame (41.60) and the hero reaching
#: awaiting_approval (43.00) — the product commits, opens the PR and then the
#: drawer's Diff section has something real to serve; showing a diff before
#: the commit exists would put a screen on camera the shipped product cannot
#: produce (the same discipline as fixture.py's beat 4).
OPEN_DRAWER_AT = 24.00
SPEC_AT = 24.50
SYSTEM_AT = 30.20
DIFF_AT = 44.00
REVIEW_AT = 47.40
CLOSE_DRAWER_AT = 49.80                    # wide: the bounce, then five PRs
REOPEN_DRAWER_AT = 62.60                   # "that click stays yours"
CLOSE2_AT = sp.CLOSE + 0.60                # 70.60 — the whole board, held

#: Put the board's POLLING readouts on the frame clock, the last place wall
#: time was still leaking into the picture.
#:
#: The board pushes task rows over the WebSocket shim, so the lanes and the
#: "N running" counter are already frame-locked. The header's drain chip is
#: not: it is fed by `App.jsx`'s `setInterval(poll, 10000)`, which runs on
#: REAL time while the recorder's clock advances a frame at a time. A capture
#: takes minutes of wall time to render 78 s of clip, so that poll fires at
#: arbitrary, run-dependent points on the fixture's timeline — and the value
#: it caught can be seconds of clip-time stale. Measured on the first render:
#: at t=57.0 the chip read "1/5 workers busy" while the fixture (and the
#: header's own "0 running", pushed over the socket) said nothing was running.
#: Two contradictory numbers in the same header row, in the payoff shot.
#:
#: So: intercept the long intervals instead of letting them run. Every
#: registered callback is parked in a list and fired by `__nhTick()` — which
#: the recorder calls on the frames where the fixture actually changes, with
#: `world.t` already set to that frame. `__nhIdle()` reports whether any
#: fetch is still in flight so the recorder can wait for the answer to land
#: before it screenshots. `__nhPollCount()` is how `main` checks that anything
#: was parked at all — the shim is one lost race away from a silent no-op, and
#: a silent no-op here looks exactly like the bug it fixes. Short intervals are
#: left alone: they are animation, not data, and stealing them would freeze
#: spinners mid-clip.
POLL_SHIM = """
(() => {
  const realSetInterval = window.setInterval.bind(window);
  const realClearInterval = window.clearInterval.bind(window);
  const polls = [];
  window.setInterval = (fn, ms, ...rest) => {
    if (typeof fn === 'function' && ms >= 1000) {
      polls.push(fn);
      return -polls.length;            // negative ids are the recorder's
    }
    return realSetInterval(fn, ms, ...rest);
  };
  window.clearInterval = (id) => {
    if (typeof id === 'number' && id < 0) { polls[-id - 1] = null; return; }
    realClearInterval(id);
  };
  let pending = 0;
  const realFetch = window.fetch.bind(window);
  window.fetch = (...a) => {
    pending += 1;
    let p;
    try { p = realFetch(...a); } catch (e) { pending -= 1; throw e; }
    return p.finally(() => { pending -= 1; });
  };
  window.__nhIdle = () => pending === 0;
  window.__nhTick = () => { for (const fn of polls) if (fn) fn(); };
  window.__nhPollCount = () => polls.length;
})();
"""

#: How long to wait for a ticked poll to come back before giving up. The API
#: is answered in-process by `World.route`; a second is a hang, not slowness.
POLL_TIMEOUT_MS = 5_000


def chip_text_at(t: float) -> str:
    """The counts `drainChip.js` must be showing at second ``t``.

    Deliberately only the counts, not the whole chip string: this is a mirror
    of someone else's formatter, and a mirror is a liability in proportion to
    how much it copies. `drainChip.js` builds its text from
    ```${workers_busy}/${max_workers} workers busy`` — that fragment is the
    part that was wrong on camera, and it is the part worth asserting.
    """
    health = sp.queue_health_at(t)
    return f"{health['workers_busy']}/{health['max_workers']} workers busy"


#: Which drawer section is pinned at time ``t`` — None outside the drawer's
#: two open windows. Same pin mechanism (and the same measured anchors) as
#: gui_frames.PIN_BY_BEAT: the sections are the same sections.
def pin_at(t: float):
    if OPEN_DRAWER_AT + 0.4 <= t < CLOSE_DRAWER_AT:
        if t >= REVIEW_AT:
            return PIN_BY_BEAT["review"]
        if t >= DIFF_AT:
            return PIN_BY_BEAT["diff"]
        if t >= SYSTEM_AT:
            return PIN_BY_BEAT["system"]
        return PIN_BY_BEAT["spec"]
    if REOPEN_DRAWER_AT + 0.4 <= t < CLOSE2_AT:
        return PIN_BY_BEAT["review"]
    return None


class World:
    """The API as of frame ``t`` — sprint.py's board, served."""

    def __init__(self) -> None:
        self.t = 0.0

    async def route(self, route):
        url = route.request.url
        path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]

        def j(body):
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps(body))

        if route.request.method != "GET":
            return await j({"ok": True,
                            "message": "Approval recorded. You merge the PR in "
                                       "your git host - the agent never merges."})

        if path == "/api/tasks":
            return await j(sp.board_at(self.t))
        m = re.match(r"^/api/tasks/([^/]+)(/.*)?$", path)
        if m:
            tail = m.group(2) or ""
            if tail.startswith("/events/stream"):
                return await route.fulfill(status=204, body="")
            if tail.startswith("/events"):
                return await j(sp.events_at(self.t))
            if tail.startswith("/diff"):
                return await route.fulfill(status=200,
                                           content_type="text/plain",
                                           body=sp.DIFF)
            if tail.startswith("/attachments"):
                return await j([])
            if m.group(1) == sp.HERO_ID:
                return await j(sp.hero_detail(self.t))
            for row in sp.board_at(self.t):
                if row["id"] == m.group(1):
                    return await j(dict(row, attempts=[],
                                        acceptance_criteria=[]))
            return await j({})
        if path.startswith("/api/onboarding"):
            return await j({"completed": True})
        if path == "/api/config":
            return await j({"human": {"name": "Alex"},
                            "llm": {"auth_mode": "subscription"},
                            "onboarding": {"completed": True}})
        if path.startswith("/api/projects"):
            return await j([{"id": "p1", "name": sp.REPO_NAME,
                             "repo_paths": [sp.REPO_PATH]}])
        if path == "/api/auth/status":
            return await j({"auth_mode": "subscription", "profile": "personal",
                            "logged_in": True})
        if path == "/api/worker/status":
            return await j({"running": True,
                            "inflight": sp.inflight_at(self.t),
                            "max_workers": 5})
        if path == "/api/queue/health":
            return await j(sp.queue_health_at(self.t))
        if path.startswith("/api/repos") or path.startswith("/api/fs/"):
            return await j([])
        if path in ("/api/rules", "/api/skills", "/api/learnings"):
            return await j([])
        return await j({})


def _board_signature(t: float):
    """What has to change for a WS push to be due: any row's status,
    revision stamp or approval. Pushing on the signature and not the frame
    index means nothing is pushed twice and every change is pushed once."""
    return tuple((r["id"], r["status"], r["updated_at"],
                  bool(r["approved_at"])) for r in sp.board_at(t))


def build_script(page):
    """(time, coroutine-factory) steps. Every time is a narration anchor."""
    async def expand_working_lane():
        await page.locator(".lane-working .lane-more").first.click()

    async def open_drawer():
        await page.locator(".task-card", has_text=sp.HERO_KEY).first.click()
        await page.wait_for_selector(".slideover .so-accordion")

    def open_section(label, ready, pin):
        async def _do():
            head = page.locator(".slideover .so-section-header",
                                has_text=label).first
            if await head.get_attribute("aria-expanded") != "true":
                await head.click()
            await page.wait_for_selector(ready)
            await page.evaluate(PIN_SECTION, PIN_BY_BEAT[pin])
        return _do

    async def close_drawer():
        await page.keyboard.press("Escape")
        await page.wait_for_selector(".slideover", state="detached")

    async def approve():
        await page.locator(".slideover .so-actions .btn-approve").first.click()

    async def noop():
        return None

    return sorted([
        (EXPAND_LANE_AT, expand_working_lane),
        (OPEN_DRAWER_AT, open_drawer),
        (SPEC_AT, open_section("Spec", '[data-testid="spec-tab"]', "spec")),
        (SYSTEM_AT, open_section("System", ".slideover .fx-board", "system")),
        (DIFF_AT, open_section("Diff", '[data-testid="diff-view"]', "diff")),
        (REVIEW_AT,
         open_section("Review", ".slideover .so-checklist", "review")),
        (CLOSE_DRAWER_AT, close_drawer),
        (REOPEN_DRAWER_AT, open_drawer),
        (REOPEN_DRAWER_AT + 0.30,
         open_section("Review", ".slideover .so-checklist", "review")),
        (sp.APPROVE_AT, approve),
        (CLOSE2_AT, close_drawer),
        (nr.DURATION - 0.1, noop),
    ], key=lambda s: s[0])


async def _repoll(page, t: float) -> None:
    """Fire the parked polls for frame ``t``, wait for the answers, and refuse
    to keep recording if the header still shows the wrong number.

    The wait is the point. Ticking a poll and screenshotting in the same
    breath would put the PREVIOUS frame's counts on camera and call it
    frame-locked; and a silent one-frame lag is exactly the kind of defect
    that survives to a published video. So the recorder blocks until the chip
    agrees with the fixture, and dies naming the frame if it never does.
    """
    await page.evaluate("() => window.__nhTick()")
    await page.wait_for_function("() => window.__nhIdle()",
                                 timeout=POLL_TIMEOUT_MS)
    await settle(page)
    want = chip_text_at(t)
    try:
        await page.wait_for_function(
            "want => { const el = document.querySelector('.drain-chip');"
            "          return !!el && el.textContent.includes(want); }",
            arg=want, timeout=POLL_TIMEOUT_MS)
    except Exception:
        chip = await page.evaluate(
            "() => (document.querySelector('.drain-chip') || {}).textContent")
        raise SystemExit(
            f"[sprint-gui] at t={t:.2f}s the header chip reads {chip!r}, "
            f"the fixture says {want!r} — the board is not on the frame clock")


async def main(out_dir: Path) -> None:
    from playwright.async_api import async_playwright

    if not (DIST / "index.html").is_file():
        raise SystemExit(f"no built SPA at {DIST} — run `npm run build` in web/")
    out_dir.mkdir(parents=True, exist_ok=True)
    srv = serve_dist()
    world = World()

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": VIEW_W, "height": VIEW_H},
            device_scale_factor=DSF, reduced_motion="reduce")
        await ctx.add_init_script("localStorage.setItem('nh-theme','dark')")
        await ctx.add_init_script(WS_SHIM)
        await ctx.add_init_script(SSE_SHIM)
        await ctx.clock.set_fixed_time(sp.BROWSER_NOW)
        # AFTER the clock, and that order is load-bearing: `set_fixed_time`
        # installs Playwright's own timer instrumentation, which replaces
        # `window.setInterval` with its own wrapper. Registered before it,
        # POLL_SHIM's wrapper is the one that gets replaced — measured: the
        # board's poll went to Playwright's clock, `__nhTick()` had nothing
        # parked to fire, and the chip sat on its mount-time value all clip.
        await ctx.add_init_script(POLL_SHIM)
        await ctx.route("**/api/**", world.route)
        page = await ctx.new_page()
        await page.goto("http://127.0.0.1:4711/", wait_until="networkidle")
        await page.wait_for_selector(".task-card")
        # The board has mounted, so its polls have been registered. If none
        # were parked, POLL_SHIM lost the race for `window.setInterval` again
        # and every readout below is back on wall time — say so here rather
        # than let 2340 frames render around a stale number.
        parked = await page.evaluate("() => window.__nhPollCount()")
        if parked < 1:
            raise SystemExit(
                "[sprint-gui] POLL_SHIM parked no intervals — something else "
                "owns window.setInterval and the readouts are on wall time")

        script = build_script(page)
        cursor = 0
        pushed = None
        streamed = 0
        for frame in range(nr.N_FRAMES):
            world.t = nr.t_of(frame)
            while cursor < len(script) and script[cursor][0] <= world.t:
                await script[cursor][1]()
                cursor += 1
            sig = _board_signature(world.t)
            if sig != pushed:
                pushed = sig
                await page.evaluate(
                    "p => window.__nhPush(p)",
                    {"tasks": sp.board_at(world.t),
                     "worker": {"running": True,
                                "inflight": sp.inflight_at(world.t)}})
                # The polled readouts, re-fetched on this frame's clock. The
                # queue's health is a pure function of the rows' statuses, so
                # a board signature that moved is exactly when the chip's
                # number can have moved too.
                await _repoll(page, world.t)
            while (streamed < len(sp.EVENT_TRAIL)
                   and sp.EVENT_TRAIL[streamed][0] <= world.t):
                await page.evaluate("e => window.__nhStream(e)",
                                    sp.EVENT_TRAIL[streamed][1])
                streamed += 1
            await settle(page)
            pin = pin_at(world.t)
            if pin:
                await page.evaluate(PIN_SECTION, pin)
                await settle(page)
            await page.screenshot(path=str(out_dir / f"f{frame:04d}.png"))
        await browser.close()
    srv.shutdown()
    print(f"[sprint-gui] {nr.N_FRAMES} frames -> {out_dir}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1
                          else "/tmp/nh-sprint-demo/frames-gui")))
