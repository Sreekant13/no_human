"""Record the React board, one frame at a time.

Same discipline as the shell recorder: this is not a screen capture. The built
SPA (`web/dist`) is served over plain HTTP, every `**/api/**` request is
answered from ``fixture.py`` at the frame's own timestamp, and the recorder
steps the world forward one frame at a time and screenshots. Wall-clock timing
never enters the output, so two runs produce the same 780 frames.

Serving `web/dist` and stubbing the API with `page.route` is the pattern the
shipped browser tests already use (`web/e2e/composer.mjs`, `web/e2e/drawer.mjs`)
— this is those harnesses with a camera bolted on, not a new way of driving the
board. It also means no scheduler, no database and no LLM: filing a task on the
real server would call the intake grill, which is neither free nor repeatable.

    python -m e2e.demo_video.gui_frames /tmp/frames-gui

Capture is 1280x720 CSS logical at deviceScaleFactor 2 -> 2560x1440 device px,
which is the resolution the spotlight rects in `record.py` are expressed in.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import re
import socketserver
import sys
import threading
from pathlib import Path

from . import fixture as fx
from .camera import BEAT_2, BEAT_3, BEAT_4, BEAT_5, N_FRAMES, t_of

#: Scroll the drawer's body so that ``sel`` — an element of whichever accordion
#: section is open — sits ``gap`` px below the top of the scroller.
#:
#: Re-applied on EVERY drawer frame rather than once: SlideOver.jsx runs its own
#: `scrollIntoView` on the section header when the detail fetch resolves, and
#: whether that landed before or after a one-shot scroll of ours varied run to
#: run — two frames out of 360 showed a different scroll position. Applied every
#: frame it is idempotent, and the race has nowhere to land. (It also needs to
#: be: measured cold, one application lands ~40 px off, and only converges
#: because the capture loop keeps re-running it.)
#:
#: It used to be a constant, targeting `.so-section-label-row` at 8 px — the
#: Review body's first sub-head. That only ever worked for one section: Spec's
#: label rows are its "Files to Change" heading, and System and Diff have none
#: at all, so three of the four sections this cut visits would not have
#: scrolled anywhere.
PIN_SECTION = """([sel, gap]) => {
    const body = document.querySelector('.slideover .so-body');
    const el = document.querySelector('.slideover .so-section.open ' + sel);
    if (!body || !el) return;
    body.scrollTop += el.getBoundingClientRect().top
                    - body.getBoundingClientRect().top - gap;
}"""

#: (selector, gap) per beat: which element of the open section sits ``gap`` px
#: below the top of the drawer's scroller.
#:
#: One anchor for all four would have been tidier and wrong. The scroller is
#: ~316 CSS px tall at this viewport (the drawer is 662, and the summary strip
#: above the accordion takes 274 of it), and each of the four sections is a
#: different shape inside it — so "which 316 px" is a different answer each
#: time, and the wrong answer is a beat that shows a section's title and none
#: of the thing the caption just promised:
#:
#:   Spec    the plan is 320 px of five headings and fits almost exactly, so
#:           the anchor is the BODY: all five (files, approach, test plan, out
#:           of scope, verification) or nothing worth calling a plan.
#:   System  the "Running — <stage>" banner plus the 263 px agent board is
#:           312 px and fits with 3 px to spare, so the anchor is the BANNER —
#:           it says a stage is live, which is the beat, and it is the one
#:           element that has to be whole or clipped rather than half-drawn.
#:           (Anchoring the section header instead left 6 px of the banner's
#:           bottom border showing above the fold, which reads as a rendering
#:           fault; anchoring the board itself hid the banner entirely and
#:           spent the room on the "Events / Duration / Attempt" footer.)
#:   Diff    30 lines at 22 px is 660 px and 14 fit. The 14 that matter are the
#:           ones the ticket is about, so the anchor is the pre block pulled up
#:           one line: `--- a/calc.py` through `+    return a * b`, the whole
#:           `mul()` hunk with the file it is in.
#:   Review  the checklist alone is 520 px. Anchoring its label row puts the
#:           PASSED verdict at the top and the four cited rows under it, which
#:           is the framing the four-beat cut was accepted with.
PIN_BY_BEAT = {
    "spec":   ('[data-testid="spec-tab"]', 4),
    "system": (".fx-banner", 16),
    # -22 is exactly one line: it clips `diff --git a/calc.py b/calc.py` off the
    # top so the scroller's first row starts CLEAN rather than on the bottom
    # 8 px of a line of text, and lands `--- a/calc.py` through
    # `+    return a * b` — all fourteen rows that fit — inside the frame. The
    # 8 px of `diff --git a/test_calc.py` left showing at the bottom is not a
    # defect: there IS more below, and a diff that ends flush would say there
    # is not.
    "diff":   (".diff-pre", -22),
    "review": (".so-section-label-row", 8),
}

DIST = Path(__file__).resolve().parents[2] / "web" / "dist"
PORT = 4711

# 1280x720 CSS at deviceScaleFactor 2. Three things pin it:
#
#  * 16:9, so the delivered 1600x900 app area is the capture scaled by 0.625
#    with nothing letterboxed and nothing stretched. The previous 960x675 was
#    1.42:1 and only ever worked because every beat was cropped out of it.
#  * The full frame is the shot now, so the window has to be a window a person
#    would actually use. 1280x720 is a laptop; 960x675 is a screenshot crop.
#  * It is the same pixel grid the terminal clip captures at, so the two clips
#    downscale by the same factor and their type comes out the same weight.
#
# There is deliberately no second viewport. Beats 3-4 used to narrow the window
# to 690 px so a crop could magnify the drawer; with no crop that move would be
# a live window resize on camera — the whole board reflowing mid-clip — which is
# the opposite of "a human can keep track". At 1280 the drawer is
# `min(1280, 94vw)` = 1203 CSS px and its review block reads at full size.
VIEW_W, VIEW_H = 1280, 720
DSF = 2
DEV_W, DEV_H = VIEW_W * DSF, VIEW_H * DSF   # 2560 x 1440

# The composer is a real multi-step wizard (App.jsx NewTaskModal): step 1 is the
# form, step 2 is the grill, step 3 is the refined spec with "Create Task".
# Beat 1 walks all three so the board tells the SAME story as the shell, whose
# beat 1 is that same grill exchange.
#
# 24 cps, not the 42 of the 12-second cut: a beat is 4.6 s now, and typing that
# outruns the beat it lives in is the thing the pacing change exists to fix.
SCRIPT_TYPE_START = 0.81
SCRIPT_TYPE_CPS = 24.0


WS_SHIM = """
(() => {
  class RecorderSocket {
    constructor(url) {
      this.url = url; this.readyState = 1;
      window.__nhSockets = (window.__nhSockets || []).concat(this);
      queueMicrotask(() => { if (this.onopen) this.onopen({}); });
    }
    send() {} close() { this.readyState = 3; }
  }
  RecorderSocket.OPEN = 1;
  window.WebSocket = RecorderSocket;
  window.__nhPush = (payload) => {
    const text = JSON.stringify(payload);
    for (const s of (window.__nhSockets || [])) {
      if (s.onmessage) s.onmessage({ data: text });
    }
  };
})();
"""

#: The same trick for EventSource, and for the same reason.
#:
#: `SlideOver.jsx:SystemTab` seeds itself with ONE `GET .../events` and then
#: takes every later frame off an SSE stream (`api.js:connectTaskSSE`) — it does
#: not poll while the task is active. Under `page.route` there is no stream: an
#: EventSource pointed at a fulfilled JSON response fires `onerror`, and the
#: native reconnect then re-fires it every few seconds forever. The visible
#: result is an agent board frozen at whatever the seed fetch happened to
#: return, which for beat 3 is the whole beat.
#:
#: So: a fake EventSource the recorder pushes through, driving the app's REAL
#: `es.onmessage -> setEvents([...prev, evt])` path — the same shape of shim the
#: board's WebSocket already uses, and for the same reason. `onerror` is never
#: fired, so nothing reconnects and nothing is dropped.
SSE_SHIM = """
(() => {
  class RecorderEventSource {
    constructor(url) {
      this.url = url; this.readyState = 1;
      window.__nhStreams = (window.__nhStreams || []).concat(this);
      queueMicrotask(() => { if (this.onopen) this.onopen({}); });
    }
    close() {
      this.readyState = 2;
      window.__nhStreams = (window.__nhStreams || []).filter(s => s !== this);
    }
  }
  RecorderEventSource.CONNECTING = 0;
  RecorderEventSource.OPEN = 1;
  RecorderEventSource.CLOSED = 2;
  window.EventSource = RecorderEventSource;
  window.__nhStream = (frame) => {
    const data = JSON.stringify(frame);
    for (const s of (window.__nhStreams || [])) {
      if (s.onmessage) s.onmessage({ data });
    }
  };
})();
"""


def _pin_at(t: float):
    """Which (selector, gap) the capture loop pins on at time ``t``.

    Applied on EVERY frame from beat 2 on, not once per section: the section
    bodies fetch and grow after they mount, and a one-shot scroll then leaves
    the beat looking at where the content used to be.
    """
    if t >= BEAT_5:
        return PIN_BY_BEAT["review"]
    if t >= BEAT_4:
        return PIN_BY_BEAT["diff"]
    if t >= BEAT_3:
        return PIN_BY_BEAT["system"]
    if t >= BEAT_2:
        return PIN_BY_BEAT["spec"]
    return None


async def settle(page) -> None:
    """Wait for a commit and a paint. Two rAFs: the first fires after the
    current commit, the second after that frame has been painted."""
    await page.evaluate(
        "() => new Promise(r => requestAnimationFrame("
        "() => requestAnimationFrame(r)))")


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def send_head(self):
        # SPA fallback: any unknown path serves index.html.
        path = self.translate_path(self.path)
        if not Path(path).is_file():
            self.path = "/index.html"
        return super().send_head()


def serve_dist() -> socketserver.TCPServer:
    handler = functools.partial(_Handler, directory=str(DIST))
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class World:
    """The API, as of frame ``t``. One object so a route handler and the
    recorder cannot disagree about what second it is."""

    def __init__(self) -> None:
        self.t = 0.0

    async def route(self, route):
        url = route.request.url
        path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]

        def j(body):
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps(body))

        if route.request.method != "GET":
            if path == "/api/grill/stream":
                return await route.fulfill(
                    status=200, content_type="text/event-stream",
                    body="".join(f"data: {json.dumps(f)}\n\n"
                                 for f in self._grill(route)))
            if path == "/api/tasks":
                return await j({"id": fx.HERO_ID})
            return await j({"ok": True,
                            "message": "Approval recorded. You merge the PR in "
                                       "your git host - the agent never merges."})

        if path == "/api/tasks":
            return await j(fx.board_at(self.t))
        m = re.match(r"^/api/tasks/([^/]+)(/.*)?$", path)
        if m:
            tail = m.group(2) or ""
            if tail.startswith("/events/stream"):
                # Never reached: SSE_SHIM replaces EventSource, so the app
                # opens no stream. Answered anyway so a future change that
                # drops the shim fails loudly rather than silently freezing
                # the agent board.
                return await route.fulfill(status=204, body="")
            if tail.startswith("/events"):
                return await j(fx.events_at(self.t))
            if tail.startswith("/diff"):
                # PLAIN TEXT, not JSON. `api/app.py:get_diff` is a
                # `PlainTextResponse` and `api.js:fetchDiff` reads `r.text()`,
                # so the JSON body this used to return would have rendered on
                # camera as `{"diff": "diff --git a/calc.py ..."}` — one long
                # escaped line in the Diff section. Nothing caught it because
                # no beat had ever opened that section.
                return await route.fulfill(status=200, content_type="text/plain",
                                           body=fx.DIFF)
            if tail.startswith("/attachments"):
                return await j([])
            if m.group(1) == fx.HERO_ID:
                return await j(fx.hero_detail(self.t))
            for row in fx.BACKDROP:
                if row["id"] == m.group(1):
                    return await j(dict(row, attempts=[], acceptance_criteria=[]))
            return await j({})
        if path.startswith("/api/onboarding"):
            # NOTE the exact path: the app calls /api/onboarding/STATUS, and an
            # answer of {} reads as completed:false — which is how the first
            # run of this harness recorded 360 frames of the marketing splash
            # instead of the board.
            return await j({"completed": True})
        if path == "/api/config":
            return await j({"human": {"name": "Eyal"},
                            "llm": {"auth_mode": "subscription"},
                            "onboarding": {"completed": True}})
        if path.startswith("/api/projects"):
            return await j([{"id": "p1", "name": "calc",
                             "repo_paths": [fx.REPO_PATH]}])
        if path == "/api/auth/status":
            return await j({"auth_mode": "subscription", "profile": "personal",
                            "logged_in": True})
        if path == "/api/worker/status":
            return await j({"running": True, "inflight": 2, "max_workers": 4})
        if path == "/api/queue/health":
            return await j({"ok": True, "depth": 0})
        if path.startswith("/api/repos") or path.startswith("/api/fs/"):
            return await j([])
        if path in ("/api/rules", "/api/skills", "/api/learnings"):
            return await j([])
        if path == "/api/metrics":
            return await j({})
        return await j({})

    @staticmethod
    def _grill(route):
        try:
            body = json.loads(route.request.post_data or "{}")
        except ValueError:
            body = {}
        first = not (body.get("qa_history") or [])
        frames = list(fx.GRILL_ROUND_1 if first else fx.GRILL_ROUND_2)
        return frames + [{"kind": "done", "text": "stream ended"}]


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
            device_scale_factor=DSF,
            # Reduced motion is the only way to make entrance animations,
            # the card pulse and the accordion slide deterministic per frame.
            # Motion in this video comes from state changes and the camera,
            # which is also what the brief asks for: cut, do not ramp.
            reduced_motion="reduce",
        )
        await ctx.add_init_script("localStorage.setItem('nh-theme','dark')")
        # `GET /api/tasks` is fetched ONCE on mount and once after a create
        # (App.jsx:610, 926) — every later board change arrives over the
        # WebSocket as {tasks:[...]} -> dispatch({type:"sync"}). With no socket
        # the card froze on whatever status it was created with, and the
        # sidebar read "Reconnecting…" for the whole clip. So: a fake socket
        # the recorder pushes through, which drives the app's REAL reducer
        # path and also lets `wsLive` go true, exactly as against a live server.
        await ctx.add_init_script(WS_SHIM)
        await ctx.add_init_script(SSE_SHIM)
        # Pin Date.now(). Board.jsx `relativeTime` is the only clock either
        # surface has and it floors to whole minutes, so it cannot tick inside
        # 12 s — but it CAN drift between recordings. Pinned, the hero reads
        # "<1m" and the backdrop reads 9m / 21m on every run.
        await ctx.clock.set_fixed_time(fx.BROWSER_NOW)
        await ctx.route("**/api/**", world.route)
        page = await ctx.new_page()
        await page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
        await page.wait_for_selector(".task-card")

        script = _build_script(page)
        cursor = 0
        pushed = -1
        streamed = 0
        for frame in range(N_FRAMES):
            world.t = t_of(frame)
            while cursor < len(script) and script[cursor][0] <= world.t:
                await script[cursor][1]()
                cursor += 1
            # Push the board on every stage boundary, in the WS frame shape the
            # server sends. Stage index, not frame index, so nothing is pushed
            # twice and the card advances exactly when the fixture says.
            stage = (sum(1 for start, *_ in fx.STAGES if world.t >= start),
                     bool(fx.approved_at(world.t)))
            if stage != pushed:
                pushed = stage
                await page.evaluate(
                    "p => window.__nhPush(p)",
                    {"tasks": fx.board_at(world.t),
                     "worker": {"running": True, "inflight": 2}})
            # ...and push every event frame that has come due, one at a time,
            # through the app's own SSE handler. Indexed by position in the
            # trail, not by frame, so nothing is delivered twice — the System
            # view appends blindly and a repeat would double an agent's count.
            while (streamed < len(fx.EVENT_TRAIL)
                   and fx.EVENT_TRAIL[streamed][0] <= world.t):
                await page.evaluate("e => window.__nhStream(e)",
                                    fx.EVENT_TRAIL[streamed][1])
                streamed += 1
            # Let React commit AND the compositor paint before the shutter.
            # Without this, the single frame straight after a state change
            # (the spec dialog at 2.63 s, the drawer at 5.55 s) caught a
            # half-painted DOM and differed between two runs of one script.
            await settle(page)
            pin = _pin_at(world.t)
            if pin:
                # After the settle, so the drawer's own scrollIntoView has
                # already fired and this is the last write to scrollTop.
                await page.evaluate(PIN_SECTION, pin)
                await settle(page)
            await page.screenshot(path=str(out_dir / f"f{frame:04d}.png"))
        await browser.close()
    srv.shutdown()
    print(f"[gui] frames -> {out_dir}")


def _build_script(page):
    """(time, coroutine-factory) steps. Mirrors cli_frames.build_script beat
    for beat: type the request, run the grill, create; then the drawer, one
    accordion section per beat — the plan, the agents, the diff, the review —
    and approve."""
    dialog = page.locator('[role="dialog"]')

    async def open_composer():
        await page.keyboard.press("n")
        await page.wait_for_selector('[role="dialog"][aria-label="New task"]')
        await page.locator('[role="dialog"] textarea').first.focus()

    def type_char(ch):
        async def _do():
            await page.keyboard.type(ch)
        return _do

    async def submit_form():
        await dialog.locator('button[type="submit"]').first.click()

    async def answer_grill():
        # The shell's beat 1 cuts here too: the suggestion is taken whole
        # rather than typed, so the same 0.4 s carries the same moment.
        await page.locator(".grill-suggestion", has_text=fx.GRILL_ANSWER
                           ).first.click()
        await page.locator('[role="dialog"] .btn-approve').first.click()

    async def create_task():
        await page.locator('[role="dialog"][aria-label="Refined spec"] '
                           '.btn-approve').first.click()

    async def open_drawer():
        await page.locator(".task-card", has_text=fx.HERO_TITLE).first.click()
        # Wait for the ACCORDION, not just the drawer shell: `.slideover`
        # exists before its detail fetch resolves, and screenshotting in that
        # window was the one frame (of 360) that differed between two runs.
        await page.wait_for_selector(".slideover .so-accordion")

    def open_section(label, ready, pin):
        """Click one accordion header and wait for its body to actually mount.

        The accordion is single-open (`openSection` is one key, SlideOver.jsx),
        so opening one closes the last — there is never a beat where two
        sections compete for the top of the scroller. `ready` is a selector
        that only exists once the section's body has rendered: the sections
        fetch on open (Diff's text, System's event seed) and screenshotting
        between the click and the mount catches an empty box.
        """
        async def _do():
            head = page.locator(".slideover .so-section-header",
                                has_text=label).first
            # A header click TOGGLES (`setOpenSection(isOpen ? null : key)`), so
            # clicking the section that is already open closes it and the beat
            # lands on an empty accordion. The drawer auto-opens one section on
            # first load (`defaultOpenSection`: System while in flight, Review
            # once awaiting approval), which is exactly two of the four this
            # script asks for — so this guard is load-bearing, not defensive.
            if await head.get_attribute("aria-expanded") != "true":
                await head.click()
            await page.wait_for_selector(ready)
            await page.evaluate(PIN_SECTION, PIN_BY_BEAT[pin])
        return _do

    async def approve():
        await page.locator(".slideover .so-actions .btn-approve").first.click()

    async def noop():
        return None

    steps = [(0.64, open_composer)]
    for i, ch in enumerate(fx.HERO_PROMPT):
        steps.append((SCRIPT_TYPE_START + i / SCRIPT_TYPE_CPS, type_char(ch)))
    steps += [
        (2.51, submit_form),      # -> grill loading, then the question
        (4.17, answer_grill),     # -> refined spec
        (4.88, create_task),      # -> the id exists, on both surfaces
        # The drawer opens BEFORE beat 2, on a task that is still planning, so
        # `defaultOpenSection` gives us System (an in-flight task's default) and
        # not a section this script has to fight. The spotlight is still on the
        # whole frame here; beat 2's drift starts at 5.20.
        (5.16, open_drawer),
        # beat 2 — the plan. The Spec body only exists from `implementing`
        # (5.58), which lands mid-drift, so the click waits for it.
        (BEAT_2 + 0.42,
         open_section("Spec", '[data-testid="spec-tab"]', "spec")),
        # beat 3 — the agents. `.fx-board` is the five-stage lane grid.
        (BEAT_3 + 0.04,
         open_section("System", ".slideover .fx-board", "system")),
        # beat 4 — the diff.
        (BEAT_4 + 0.04,
         open_section("Diff", '[data-testid="diff-view"]', "diff")),
        # beat 5 — the review evidence, then the approval.
        (BEAT_5 + 0.04,
         open_section("Review", ".slideover .so-checklist", "review")),
        (fx.APPROVE_AT, approve),
        (25.9, noop),
    ]
    return sorted(steps, key=lambda s: s[0])


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/frames-gui")))
