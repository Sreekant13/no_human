"""Frames -> the two deliverable files, with the spotlight and caption applied.

Capture happens once at full resolution; what a viewer's eye is pointed at is
decided HERE, per beat, and can be retuned without re-recording anything. That
separation is the reason a framing fix costs seconds rather than a re-record,
and it survives the move from crops to spotlights unchanged.

Per frame, one ffmpeg invocation:

    capture (2560x1440)
      -> scale to 1600x900 (lanczos)
      -> a copy multiplied to DIM_LUMA
      -> a mask: black, one white rectangle, blurred by SPOT_BLUR
      -> maskedmerge: the bright original inside the rectangle, dimmed outside
      -> pad to 1600x960 and overlay the beat's caption strip in the new band

`drawtext` is not available (this ffmpeg has no libfreetype), which is why the
caption arrives as a pre-rendered PNG — see captions.py.

Everything is even-sided (yuv420p refuses odd dimensions), fades in and out to
pure black so the loop seam is invisible, and carries no audio track at all.
"""

from __future__ import annotations

import concurrent.futures
import subprocess
from pathlib import Path

from .camera import (APP_H, APP_W, DIM_LUMA, FADE_IN_END, FADE_OUT_END,
                     FADE_OUT_START, FPS, N_FRAMES, OUT_H, OUT_W, SPOT_BLUR,
                     beat_index, focus_at, to_output, t_of)

FFMPEG = "/opt/homebrew/bin/ffmpeg"

#: bytes — per-file ceilings, enforced by record.py.
#:
#: BUDGET, restated 2026-07-31 when the clips went full-frame. The old numbers
#: (900 KB webm / 1200 KB mp4 / 4 MiB total) were sized for 12.000 s of
#: 1200x676. The delivered clips are now 1600x960 — 1.89x the pixels — so ~3.2x
#: the bits at equal quality. Holding the old ceiling could only be paid for out
#: of quality or duration, and both of those are the point of the change, so the
#: ceiling moved instead.
#:
#: RAISED AGAIN 2026-07-31 for the five-beat cut, but by LESS than the duration
#: grew. 20.000 s became 26.000 s (1.30x) because a fifth beat was added and
#: the payoff beat lengthened; at the same encoder settings the terminal clip
#: came out 2,613,716 / 3,033,500 — over both old ceilings. The first response
#: was not to raise them but to spend the encoder's own headroom, which is free:
#: `-cpu-used 1 -> 0` (VP9) and `-preset slow -> veryslow` (x264) are slower to
#: encode and identical to watch, and they gave back 3.7% and 8.7%. The
#: remainder is what a longer clip genuinely costs. The alternative was a
#: higher crf on the clip whose entire value is small monospace text, which is
#: precisely the wrong place to buy bytes.
#:
#: Per second, the budget therefore TIGHTENED: 120 -> 108 KB/s (webm) and
#: 130 -> 115 KB/s (mp4).
#:
#: What a visitor actually pays: the page declares webm AND mp4 as <source>
#: alternates and a browser fetches exactly ONE of them, and `preload="none"`
#: means nothing is fetched at all until the demo section scrolls into view. So
#: the real cost is two files, ~4.5 MB worst case at these ceilings (a Safari
#: reader taking both mp4s) — and the measured pair is under. MAX_TOTAL covers
#: all six artefacts on disk (4 videos + 2 posters); it is UNCHANGED, because
#: the measured total still fits it, and it is a repo-size guard rather than a
#: visitor cost.
MAX_WEBM = 2_800_000
MAX_MP4 = 3_000_000
MAX_TOTAL = 9 * 1024 * 1024


def _composite_one(args):
    src, dst, rect, bar = args
    x, y, w, h = rect
    graph = (
        f"[0:v]scale={APP_W}:{APP_H}:flags=lanczos,format=gbrp,split=3[a][b][m];"
        f"[a]colorchannelmixer=rr={DIM_LUMA}:gg={DIM_LUMA}:bb={DIM_LUMA}[dim];"
        f"[m]drawbox=x=0:y=0:w={APP_W}:h={APP_H}:color=black:t=fill,"
        f"drawbox=x={x}:y={y}:w={w}:h={h}:color=white:t=fill,"
        f"gblur=sigma={SPOT_BLUR},format=gbrp[mask];"
        f"[dim][b][mask]maskedmerge,format=rgb24,"
        f"pad={OUT_W}:{OUT_H}:0:0:color=black[app];"
        f"[app][1:v]overlay=0:{APP_H}[out]"
    )
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(src), "-i", str(bar),
         "-filter_complex", graph, "-map", "[out]", "-frames:v", "1", str(dst)],
        check=True)


def compose(frames_dir: Path, stage_dir: Path, keyframes, dims,
            bars: list[Path]) -> None:
    """Turn a directory of raw captures into the delivered 1600x960 frames.

    ``dims`` is the capture resolution (w, h) the spotlight rects are expressed
    in; ``bars`` is one caption strip per beat, in beat order.
    """
    stage_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for i in range(N_FRAMES):
        t = t_of(i)
        rect = to_output(focus_at(keyframes, t), *dims)
        jobs.append((frames_dir / f"f{i:04d}.png", stage_dir / f"f{i:04d}.png",
                     rect, bars[beat_index(t)]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(_composite_one, jobs))


_FADE = (f"fade=t=in:st=0:d={FADE_IN_END}:color=black,"
         f"fade=t=out:st={FADE_OUT_START}:"
         f"d={FADE_OUT_END - FADE_OUT_START}:color=black")


def encode(stage_dir: Path, out_base: Path, *, webm_crf: int = 34,
           mp4_crf: int = 24) -> dict[str, int]:
    src = ["-framerate", str(FPS), "-i", str(stage_dir / "f%04d.png")]
    webm = out_base.with_suffix(".webm")
    mp4 = out_base.with_suffix(".mp4")

    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", *src, "-vf", _FADE,
         "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(webm_crf),
         "-pix_fmt", "yuv420p", "-g", str(FPS), "-row-mt", "1",
         # cpu-used 0, not 1: slower to encode, byte-identical to watch, and
         # 3.7% smaller on the terminal clip. This runs by hand, rarely.
         "-deadline", "good", "-cpu-used", "0", "-an", str(webm)], check=True)

    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", *src, "-vf", _FADE,
         # veryslow, not slow: same reasoning, 8.7% smaller.
         "-c:v", "libx264", "-crf", str(mp4_crf), "-preset", "veryslow",
         "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p",
         "-g", str(FPS), "-movflags", "+faststart", "-an", str(mp4)],
        check=True)

    return {"webm": webm.stat().st_size, "mp4": mp4.stat().st_size}


def poster(stage_dir: Path, out_jpg: Path, frame: int) -> int:
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-i", str(stage_dir / f"f{frame:04d}.png"),
         "-q:v", "4", str(out_jpg)], check=True)
    return out_jpg.stat().st_size
