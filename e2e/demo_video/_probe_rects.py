"""Measure the drawer rects each beat points at, off a live board.

Not part of the recording. Run it after a fixture or layout change and paste
the numbers into `record.py:GUI_FOCUS` — a spotlight rect that was eyeballed is
a spotlight that drifts the next time a section grows a row.

    PYTHONPATH=src python -m e2e.demo_video._probe_rects
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from . import fixture as fx
from . import gui_frames as g

OUT = Path("/tmp/nh-demo-video/probe")

# (label to click, time to be at, selectors to measure)
BEATS = [
    ("Spec", "spec", 7.5,
     ['[data-testid="spec-tab"]', ".slideover .so-section.open"]),
    ("System", "system", 12.0,
     [".slideover .fx-board", ".slideover .so-section.open",
      ".slideover .fx-model", ".slideover .fx-role-row",
      ".slideover .sys-summary", ".slideover .fx-banner"]),
    ("Diff", "diff", 16.5,
     ['[data-testid="diff-view"]', ".slideover .so-section.open",
      ".diff-pre .diff-line", ".so-diff-wrap"]),
    ("Review", "review", 21.0,
     [".slideover .so-checklist", ".slideover .so-section.open",
      ".slideover .so-actions", ".ci-title",
      ".slideover .so-section.open .so-section-label-row"]),
]

RECT = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  return {x: r.x, y: r.y, w: r.width, h: r.height,
          font: cs.fontSize, lh: cs.lineHeight, n: document.querySelectorAll(sel).length};
}"""


async def main() -> None:
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    srv = g.serve_dist()
    world = g.World()
    report: dict = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": g.VIEW_W, "height": g.VIEW_H},
            device_scale_factor=g.DSF, reduced_motion="reduce")
        await ctx.add_init_script("localStorage.setItem('nh-theme','dark')")
        await ctx.add_init_script(g.WS_SHIM)
        await ctx.add_init_script(g.SSE_SHIM)
        await ctx.clock.set_fixed_time(fx.BROWSER_NOW)
        await ctx.route("**/api/**", world.route)
        page = await ctx.new_page()

        for label, pin, t, sels in BEATS:
            world.t = t
            await page.goto(f"http://127.0.0.1:{g.PORT}/", wait_until="networkidle")
            await page.wait_for_selector(".task-card")
            await page.evaluate("p => window.__nhPush(p)",
                                {"tasks": fx.board_at(t),
                                 "worker": {"running": True, "inflight": 2}})
            await page.locator(".task-card", has_text=fx.HERO_TITLE).first.click()
            await page.wait_for_selector(".slideover .so-accordion")
            for frame in fx.events_at(t):
                await page.evaluate("e => window.__nhStream(e)", frame)
            head = page.locator(".slideover .so-section-header",
                                has_text=label).first
            if await head.get_attribute("aria-expanded") != "true":
                await head.click()
            await page.wait_for_selector(sels[0])
            # The recorder pins on EVERY frame, so it converges; one shot from
            # a cold drawer does not (the section's own scrollIntoView is still
            # settling under it). Five, to measure what the clip will show.
            for _ in range(5):
                await g.settle(page)
                await page.evaluate(g.PIN_SECTION, g.PIN_BY_BEAT[pin])
            await g.settle(page)
            got = {}
            for sel in sels + [".slideover", ".slideover .so-body"]:
                got[sel] = await page.evaluate(RECT, sel)
            report[label] = got
            await page.screenshot(path=str(OUT / f"{label.lower()}.png"))
        await browser.close()
    srv.shutdown()
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
