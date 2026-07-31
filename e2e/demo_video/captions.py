"""The caption bar, rendered once per beat as a 1600x60 PNG.

The clips are silent and they autoplay, so nothing narrates them. The caption
bar is the narration: which of the five steps is on screen, in words, at all
times. It is a strip UNDER the app rather than an overlay ON it, because both
surfaces use the bottom of the screen for what the last beat ends on (the shell's
prompt line, the drawer's approve bar) and a caption that covers the payoff is
worse than no caption.

Why a rendered PNG and not ffmpeg's `drawtext`: the ffmpeg on this machine is
built without libfreetype (`-h filter=drawtext` -> "Unknown filter"), so there
is no text filter to use. Chromium is already a dependency of both harnesses,
and going through it buys the product's OWN typography — the strips link
`web/dist`'s built stylesheet, so DM Sans and IBM Plex Mono here are the same
self-hosted woff2 files the app and the marketing site serve, not a lookalike.

One strip per beat, cut (not cross-faded) on the beat boundary: a caption that
dissolves while the spotlight is also gliding gives a viewer two moving things
to track, and the words are the one thing that should simply BE there.

    python -m e2e.demo_video.captions /tmp/bars
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

from .camera import APP_W, BAR_H, CAPTIONS

DIST = Path(__file__).resolve().parents[2] / "web" / "dist"
PORT = 4713

#: Straight from the marketing site's `:root` (index.html), which lifted them
#: from the product's `web/src/styles.css`. The bar's background is the site's
#: page colour, so the strip reads as the figure's own caption rather than as a
#: fifth pane of the app.
CSS = """
html,body{margin:0;padding:0;background:#0F1117;}
#bar{
  width:%(w)spx; height:%(h)spx; box-sizing:border-box;
  display:flex; align-items:center; gap:20px;
  padding:0 36px; background:#0F1117;
  border-top:1px solid #2E3241;
  -webkit-font-smoothing:antialiased;
}
#step{
  font-family:"IBM Plex Mono",monospace; font-weight:500; font-size:22px;
  letter-spacing:.12em; color:#4C9AFF; flex:none;
}
#rule{width:1px; height:22px; background:#2E3241; flex:none}
#text{
  font-family:"DM Sans",sans-serif; font-weight:500; font-size:25px;
  letter-spacing:-.008em; color:#E8ECF2; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis;
}
#dots{display:flex; gap:7px; margin-left:auto; flex:none}
#dots i{width:30px; height:3px; border-radius:2px; background:#2E3241}
#dots i.on{background:#4C9AFF}
"""

#: The progress indicator has one segment per beat, built from CAPTIONS rather
#: than hard-coded: it was four `<i>`s in the markup, and going to five beats
#: would otherwise have shipped a bar that says "5 / 5" above four segments
#: with the fourth lit — a progress indicator that is wrong about the progress.
PAGE = """
<link rel="stylesheet" href="/assets/%(css)s">
<style>%(inline)s</style>
<div id="bar">
  <span id="step"></span><span id="rule"></span><span id="text"></span>
  <span id="dots">%(dots)s</span>
</div>
"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass


def _serve() -> socketserver.TCPServer:
    handler = functools.partial(_Handler, directory=str(DIST))
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _stylesheet() -> str:
    sheets = sorted((DIST / "assets").glob("*.css"))
    if not sheets:
        raise SystemExit(f"no built stylesheet under {DIST}/assets — "
                         "run `npm run build` in web/")
    return sheets[0].name


async def main(out_dir: Path) -> list[Path]:
    from playwright.async_api import async_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    srv = _serve()
    made: list[Path] = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(
                viewport={"width": APP_W, "height": BAR_H}, device_scale_factor=1)
            await page.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
            await page.set_content(
                PAGE % {"css": _stylesheet(),
                        "inline": CSS % {"w": APP_W, "h": BAR_H},
                        "dots": "<i></i>" * len(CAPTIONS)},
                wait_until="networkidle")
            # A strip screenshotted before the woff2 lands renders in the
            # fallback face and looks nothing like the site. The faces are
            # unicode-range subsetted, so ask for them BY the text that will be
            # drawn — `document.fonts.ready` alone resolves happily while every
            # face is still `unloaded`, because an empty span requests nothing.
            sample = " ".join(s + t for s, t in CAPTIONS)
            loaded = await page.evaluate(
                """async (sample) => {
                    const want = [['500 25px "DM Sans"', sample],
                                  ['500 22px "IBM Plex Mono"', sample]];
                    const got = [];
                    for (const [font, text] of want) {
                        const faces = await document.fonts.load(font, text);
                        got.push(faces.length > 0 && document.fonts.check(font, text));
                    }
                    await document.fonts.ready;
                    return got;
                }""", sample)
            for family, ok in zip(("DM Sans", "IBM Plex Mono"), loaded):
                if not ok:
                    raise SystemExit(f"{family} did not load for the caption bar")
            for i, (step, text) in enumerate(CAPTIONS):
                await page.evaluate(
                    """([step, text, i]) => {
                        document.getElementById('step').textContent = step;
                        document.getElementById('text').textContent = text;
                        document.querySelectorAll('#dots i').forEach((d, n) =>
                            d.classList.toggle('on', n === i));
                    }""", [step, text, i])
                dst = out_dir / f"bar{i}.png"
                await page.locator("#bar").screenshot(path=str(dst))
                made.append(dst)
            await browser.close()
    finally:
        srv.shutdown()
    print(f"[bars] {len(made)} caption strips -> {out_dir}")
    return made


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/bars")))
