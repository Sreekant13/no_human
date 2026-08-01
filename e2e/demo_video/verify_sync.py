"""Prove — on the ENCODED mp4, not on the build inputs — that the narration
lands where the timing map says it does.

    PYTHONPATH=src python -m e2e.demo_video.verify_sync \\
        --out /tmp/nh-sprint-demo/out --work /tmp/nh-sprint-demo

Everything else in this package asserts intent: the map is self-consistent,
the recorders derive their steps from it, the ffmpeg arguments name the right
offsets. None of that survives contact with a muxer. A priming delay, an
`-shortest` truncation, a stale audio file left in the work dir, an hstack fed
yesterday's frames — every one of those ships an artifact whose audio and
picture disagree while every unit test stays green. This module reads the
finished file back and refuses that class of lie.

Two independent proofs, both on the decoded artifact:

AUDIO — where the voice actually starts
    ffmpeg's `silencedetect` segments the muxed track; the end of each silence
    is the second a sentence begins. The check is bidirectional, which is the
    part that matters: every narration line must have an onset at its
    `Line.start`, AND every onset must belong to a line. One-way would pass a
    track with an extra sentence bleeding over the next beat.

CAPTION — which beat the strip claims is on screen
    Each frame carries the beat's caption strip in its bottom `BAR_H` rows,
    and `compose.render_bars` already wrote those strips to disk. So the
    claimed beat is recoverable: crop the band, compare it to all six strips,
    and take the nearest. Nearest — not equal — because H.264 at crf 24 does
    not return the bytes it was given; the margin between the right strip and
    the runner-up is reported so "nearest" cannot quietly degrade into "noise".

    READ THE LIMIT OF THIS CHECK, because the first version of this module
    overstated it: `compose.py` BURNS the strip in from the same beat table
    that is then checked against, so the band is correct by construction on
    any file compose produced. On its own it proves the caption pipeline, not
    the picture. It was claimed here to refuse "an hstack fed yesterday's
    frames"; it would not have, and an app area 20 s out of step with a
    correct strip passed it.

APP AREA — what is actually on screen above the strip
    So the app region is checked against the capture it was made from: the
    SOURCE frames, the one reference the encoder cannot manufacture. At each
    sampled instant the mp4's top `APP_H` rows are compared with the source
    frame at that instant AND with the source frames at a range of offsets
    around it.

    WHAT IS ASSERTED, in the code's own terms: offset zero must fall INSIDE
    the resolution band — the set of offsets whose distance is within
    `NOISE_BAND` of the best one. NOT that offset zero wins the argmin. The
    difference is the whole check: on a screen that is not moving every
    candidate ties within the encode's own jitter and the argmin is decided by
    noise, so asserting it made the GENUINE shipped cut fail 52 times. What is
    real is offset zero falling OUT of the band — the picture on screen is then
    measurably better explained by some other moment.

    THAT BAND IS THE RESOLUTION, AND IT IS OFTEN WIDE. Measured on the shipped
    cut: only 12 of 38 samples on the gui pane (22 of 38 on cli) resolve a
    single 2.0 s step; on the rest the screen is static enough that a 2 s slip
    is indistinguishable and would NOT be caught at that instant. The widest
    band measured is -2.0..+6.0 s. A slip is caught because it falls outside
    the band at ENOUGH instants, not because every instant catches it.

    WHAT IS ACTUALLY SAMPLED, stated as a number rather than as an adjective,
    because the two previous versions of this docstring both claimed more than
    the code did. `GRID_STEP`-second spacing over the whole clip — with the
    shipped 78 s cut and a 2.0 s step that is 38 instants per pane.

    FRAMES BETWEEN THE SAMPLES ARE NOT INSPECTED. A file that is correct at
    the sampled instants and wrong everywhere else PASSES. That is not a
    hypothetical: a cut rebuilt so that the sampled instants carry their own
    frames and everything between them is reversed passes this check with 2150
    of 2340 frames — 91.9% — playing backwards. (That adversary was built and
    measured but is not in the tree, so this paragraph is a recorded
    measurement, not a reproducible one — no path is cited here because a
    shipped file pointing at a file nobody can open is the defect two earlier
    commits in this package already had to fix.) The once-per-beat
    version this replaced needed only six forged instants (98.7% wrong);
    38 is a higher bar, not a closed door. Reducing GRID_STEP raises the bar
    again and never closes it either. What the grid DOES close is the uniform
    shift: a 3-second slip with correct captions passed the six-instant
    version and now fails 25 of 38 samples.

    RESOLUTION IS MEASURED, NOT ASSERTED. How far the picture must slip before
    this notices depends on how much the screen is moving, which varies across
    the clip and is not something prose can settle. So each sample also reports
    its RESOLUTION BAND: the offsets whose distance is within `NOISE_BAND` of
    the best one, i.e. the shifts that are indistinguishable AT THAT INSTANT.
    A band of "0.0" means a one-step shift would have been caught there; a band
    of "-4.0..+6.0" means the screen is static enough that a six-second slip
    would not be. Read the printed bands, not this paragraph.

    If the frames directory is gone the check cannot run, and it fails rather
    than passing — see `check_app_area`.

For the side-by-side cut both panes are read separately and must name the same
beat at the same second. That is the operator's third requirement — one audio
track over both surfaces — checked as a property of the shipped file.

Nothing here runs in CI: it needs ffmpeg and a rendered mp4. The pure pieces
(argument builders, the log parser, the onset matcher, the image metric) are
unit-tested; `main` is the thin runner that feeds them a real file.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import narration as nr
from .camera import APP_H, APP_W, BAR_H

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"

#: silencedetect's floor. The track is `say` output mixed onto digital
#: silence, so the gap between lines is true zero and the threshold only has
#: to clear the AAC encoder's noise floor; -50 dBFS is far above it and far
#: below speech.
NOISE_DB = -50

#: A gap must last this long to count as a gap. Below ~0.25 s it starts
#: catching the stops inside a sentence ("Pick five. Hand them...") and every
#: line reports several onsets. LINE_GAP (0.35) is the map's own minimum
#: silence between lines, so anything shorter is by construction intra-line.
MIN_SILENCE = 0.30

#: How far an onset may sit from its line's start. The mixer places lines with
#: `adelay` at millisecond resolution and AAC adds encoder delay, so a few tens
#: of ms is expected; a tenth of a second is not, and would be audible against
#: a cut.
ONSET_TOL = 0.05

#: An onset reported within this of the end of the stream is silencedetect
#: closing its books at EOF, not a sentence starting.
EOF_EPS = 0.05

#: Where each surface's caption band lives in the frame. The side-by-side cut
#: is two 1600-wide panes hstacked, so its panes are the same crop at two
#: offsets — which is exactly why it can be checked for agreement.
PANES: dict[str, tuple[tuple[str, int], ...]] = {
    "gui": (("gui", 0),),
    "cli": (("cli", 0),),
    "sxs": (("gui", 0), ("cli", APP_W)),
}


# --------------------------------------------------------------------------- #
# Audio: where the voice actually starts                                       #
# --------------------------------------------------------------------------- #

def silence_args(mp4: Path, noise_db: int = NOISE_DB,
                 min_silence: float = MIN_SILENCE) -> list[str]:
    """Decode the muxed audio through silencedetect and throw the samples away.

    `-hide_banner -nostats` and NOT `-v error`: silencedetect reports at info
    level, so quieting ffmpeg the usual way silently returns an empty log and
    an empty log would read as "no speech anywhere" rather than as a mistake.
    """
    return [FFMPEG, "-hide_banner", "-nostats", "-i", str(mp4),
            "-map", "0:a",
            "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
            "-f", "null", "-"]


_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")


def speech_onsets(log: str, duration: float) -> tuple[float, ...]:
    """Every second at which speech begins, read off silencedetect's log.

    The end of a silence is the start of a sound. The final silence runs to
    end-of-stream and its reported end is the stream duration, not a sentence
    — that one is dropped, which is why `duration` is a parameter rather than
    something inferred from the log.
    """
    ends = [float(m) for m in _SILENCE_END.findall(log)]
    return tuple(t for t in ends if t < duration - EOF_EPS)


@dataclass(frozen=True)
class OnsetMatch:
    line: nr.Line
    onset: float | None

    @property
    def delta(self) -> float | None:
        return None if self.onset is None else self.onset - self.line.start


def match_onsets(onsets: tuple[float, ...], tol: float = ONSET_TOL
                 ) -> tuple[tuple[OnsetMatch, ...], tuple[float, ...]]:
    """Pair each narration line with the onset at its start.

    Returns ``(matches, orphans)``: one match per line (``onset=None`` when
    nothing started there) and every onset that belonged to no line. Both
    halves are failures — a missing onset is a line that never got spoken, an
    orphan is speech the map does not account for, which is how a line that
    overruns into the next beat shows up.
    """
    unclaimed = list(onsets)
    matches = []
    for line in nr.LINES:
        near = [t for t in unclaimed if abs(t - line.start) <= tol]
        best = min(near, key=lambda t: abs(t - line.start)) if near else None
        if best is not None:
            unclaimed.remove(best)
        matches.append(OnsetMatch(line, best))
    return tuple(matches), tuple(unclaimed)


# --------------------------------------------------------------------------- #
# Picture: which beat is on screen                                             #
# --------------------------------------------------------------------------- #

def band_args(src: Path, t: float, x: int) -> list[str]:
    """One frame's caption band at second ``t``, as raw RGB on stdout.

    `-ss` before `-i` so ffmpeg seeks rather than decoding 57 seconds of video
    to read 60 rows of it; it still lands on the exact timestamp.
    """
    return [FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(src),
            "-vf", f"crop={APP_W}:{BAR_H}:{x}:{APP_H}",
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]


def png_args(src: Path) -> list[str]:
    """A caption strip PNG as raw RGB on stdout, same geometry as `band_args`
    so the two are directly comparable."""
    return [FFMPEG, "-v", "error", "-i", str(src),
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]


#: The app region is compared at a thumbnail size. 1600x900 of RGB is 4.3 MB
#: per image and this module compares every sample against a dozen candidate
#: offsets, in pure Python; at 160x90 the same comparison is 43 KB.
THUMB_W, THUMB_H = 160, 90

#: Seconds between app-area samples. Every instant on this grid is checked;
#: nothing between them is. `grid()` starts one step in and stops one step
#: short, so 2.0 s over the 78 s cut is 38 samples per pane (not 39 — the
#: endpoints are dropped) and about 90 s of wall time for all three clips:
#: small enough to run on every cut, dense enough that the "correct only at
#: the probes" attack has to forge 38 instants per pane instead of six.
GRID_STEP = 2.0

#: How far either side of a sample the offset search looks, in GRID_STEP units.
#: +-3 steps = +-6 s at the default step. Wide enough to name the actual slip
#: in the failure message ("the capture from 57.00s") rather than just saying
#: "not this one".
SEARCH_STEPS = 3

#: Two candidate offsets whose distances differ by less than this are not
#: distinguishable at that instant. Measured on the shipped cut: a genuine
#: match sits at distance ~1.5-2.1 and the encode's own frame-to-frame jitter
#: is well under 0.2, so 0.25 is above the noise and far below the ~4-12
#: separation a real content change produces.
NOISE_BAND = 0.25


def app_args(src: Path, t: float, x: int) -> list[str]:
    """The APP area (everything above the caption strip) of one pane at second
    ``t``, thumbnailed, as raw RGB on stdout."""
    return [FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(src),
            "-vf", (f"crop={APP_W}:{APP_H}:{x}:0,"
                    f"scale={THUMB_W}:{THUMB_H}:flags=bilinear"),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]


def source_frame_args(png: Path) -> list[str]:
    """A raw capture frame, thumbnailed to match `app_args`.

    The capture is 2560x1440 and `compose.composite_args` scales it into
    1600x900 before the strip is padded on; both sides land at THUMB_W x
    THUMB_H, so the aspect is preserved and the two are comparable.
    """
    return [FFMPEG, "-v", "error", "-i", str(png),
            "-vf", f"scale={THUMB_W}:{THUMB_H}:flags=bilinear",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]


def mean_abs_diff(a: bytes, b: bytes) -> float:
    """Mean absolute per-channel difference, 0-255. Pure Python on ~288 KB is
    a few milliseconds and costs no dependency; numpy would buy nothing here
    and this package ships with none."""
    if len(a) != len(b):
        raise ValueError(f"size mismatch: {len(a)} vs {len(b)}")
    if not a:
        raise ValueError("empty image")
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def best_bar(band: bytes, bars: list[bytes]) -> tuple[int, float, float]:
    """Which caption strip this band is, plus ``(distance, margin)``.

    The margin is the runner-up's distance minus the winner's. It is returned,
    printed and asserted on because an argmin with no margin is a coin flip
    wearing a verdict's clothes.
    """
    scored = sorted((mean_abs_diff(band, bar), i) for i, bar in enumerate(bars))
    (best, idx), (second, _) = scored[0], scored[1]
    return idx, best, second - best


# --------------------------------------------------------------------------- #
# Runner                                                                       #
# --------------------------------------------------------------------------- #

def _run(args: list[str]) -> bytes:
    out = subprocess.run(args, check=True, capture_output=True)
    return out.stdout


def probe_duration(mp4: Path, stream: str) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", stream,
         "-show_entries", "stream=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(mp4)],
        check=True, capture_output=True, text=True)
    return float(out.stdout.strip())


def grid(duration: float = nr.DURATION, step: float = GRID_STEP
         ) -> tuple[float, ...]:
    """The instants the app area is sampled at.

    Starts one step in and stops one step short so every sample has real
    source frames on both sides to search against, and so neither end lands
    inside a fade (where the picture is a black mix and matches everything).
    """
    n = int(duration // step)
    return tuple(round(i * step, 3) for i in range(1, n))


def offset_scan(sample: bytes, refs: dict[float, bytes], t: float,
                step: float = GRID_STEP, steps: int = SEARCH_STEPS,
                noise: float = NOISE_BAND
                ) -> tuple[float, float, tuple[float, ...]]:
    """Which time-shift best explains this frame, and which shifts are ties.

    Returns ``(best_offset, best_distance, indistinguishable_offsets)``. The
    third value is the honest resolution AT THIS INSTANT — every offset whose
    distance is within ``noise`` of the winner. On a screen that is not moving
    that band is wide, and the caller prints it instead of pretending the
    check is sharper than the content allows.
    """
    scored = []
    for k in range(-steps, steps + 1):
        ref = refs.get(round(t + k * step, 3))
        if ref is not None:
            scored.append((mean_abs_diff(sample, ref), k * step))
    if not scored:
        raise ValueError(f"no reference frames around {t}")
    best_dist, best_off = min(scored)
    band = tuple(off for dist, off in sorted(scored, key=lambda s: s[1])
                 if dist - best_dist <= noise)
    return best_off, best_dist, band


def _band_str(band: tuple[float, ...]) -> str:
    if len(band) == 1:
        return f"{band[0]:+.1f}"
    return f"{min(band):+.1f}..{max(band):+.1f}"


def check_app_area(mp4: Path, work: Path, panes: tuple[tuple[str, int], ...],
                   sample_at: tuple[float, ...]) -> list[str]:
    """Match the app region against the capture it was cut from, on the grid.

    The reference set is the SOURCE frames, so this is the check that looks at
    the screen rather than at the caption compose burned in from the same beat
    table.

    For every sampled instant, offset zero must fall inside the RESOLUTION BAND
    — the offsets whose distance is within `NOISE_BAND` of the best one. It is
    NOT required to win the argmin, and saying so would overstate the check:
    where the screen is static the band is wide and a slip inside it is
    invisible at that instant (on the shipped cut, 26 of 38 gui samples cannot
    resolve one 2.0 s step). The band is printed per pane so the reader gets
    the measured resolution rather than this paragraph's adjective.
    """
    problems: list[str] = []
    for pane, x in panes:
        frames = work / f"frames-{pane}"
        if not frames.is_dir():
            print(f"    app area: NOT PROVED — no {frames}, the reference "
                  f"capture is gone")
            problems.append(f"{mp4.name}: cannot check the {pane} app area, "
                            f"{frames} is missing")
            continue
        want = sorted({round(t + k * GRID_STEP, 3) for t in sample_at
                       for k in range(-SEARCH_STEPS, SEARCH_STEPS + 1)
                       if 0 <= t + k * GRID_STEP < nr.DURATION})
        refs: dict[float, bytes] = {}
        for t in want:
            png = frames / f"f{int(round(t * nr.FPS)):04d}.png"
            if png.is_file():
                refs[t] = _run(source_frame_args(png))
        missing = [t for t in sample_at if t not in refs]
        if missing:
            problems.append(f"{mp4.name}: {len(missing)} reference frame(s) "
                            f"missing from {frames}, first at {missing[0]}s")
            continue

        bad, sharp, widest = 0, 0, ()
        for t in sample_at:
            off, dist, band = offset_scan(_run(app_args(mp4, t, x)), refs, t)
            if len(band) > len(widest):
                widest = band
            if band == (0.0,):
                sharp += 1
            # The assertion is that offset ZERO IS IN THE BAND, not that it
            # wins the argmin. On a screen that is not moving every candidate
            # ties within the noise and the argmin is then decided by encoder
            # jitter — asserting it made the GENUINE cut fail 52 times. Zero
            # falling OUT of the band is the real signal: the frame on screen
            # is measurably better explained by some other moment.
            if 0.0 not in band:
                bad += 1
                problems.append(
                    f"{mp4.name}: at {t:.2f}s the {pane} app area is the "
                    f"capture from {t + off:.2f}s — the picture is out of "
                    f"step with the clock by {off:+.2f}s (dist {dist:.2f}, "
                    f"offset 0 is outside the {_band_str(band)}s band)")
        print(f"    app area: {len(sample_at)} samples every {GRID_STEP}s, "
              f"{pane}: {len(sample_at) - bad} hold offset 0, {bad} shifted; "
              f"{sharp} resolve a {GRID_STEP}s slip, widest band "
              f"{_band_str(widest)}s")
    return problems


def verify(mp4: Path, bars_dir: Path, panes: tuple[tuple[str, int], ...],
           work: Path, min_margin: float = 2.0) -> list[str]:
    """Check one encoded file. Returns the list of failures (empty is a pass)
    and prints the measurements either way — a proof nobody can read the
    numbers of is not a proof."""
    problems: list[str] = []
    print(f"\n=== {mp4.name}")

    v_dur = probe_duration(mp4, "v:0")
    a_dur = probe_duration(mp4, "a:0")
    print(f"  video {v_dur:.3f}s  audio {a_dur:.3f}s  map {nr.DURATION:.3f}s")
    for name, dur in (("video", v_dur), ("audio", a_dur)):
        if abs(dur - nr.DURATION) > 0.05:
            problems.append(f"{mp4.name}: {name} is {dur:.3f}s, "
                            f"map says {nr.DURATION:.3f}s")

    log = subprocess.run(silence_args(mp4), capture_output=True,
                         text=True).stderr
    onsets = speech_onsets(log, a_dur)
    matches, orphans = match_onsets(onsets)
    print(f"  {len(onsets)} speech onsets vs {len(nr.LINES)} narration lines")
    worst = 0.0
    for m in matches:
        if m.onset is None:
            problems.append(f"{mp4.name}: nothing spoken at "
                            f"{m.line.start:.2f}s — {m.line.text[:48]!r}")
            print(f"    {m.line.start:6.2f}s  MISSING")
            continue
        worst = max(worst, abs(m.delta))
        print(f"    {m.line.start:6.2f}s  onset {m.onset:7.3f}s  "
              f"delta {m.delta:+.3f}s  {m.line.text[:44]}")
    for t in orphans:
        problems.append(f"{mp4.name}: speech at {t:.3f}s belongs to no line")
    print(f"  worst onset error {worst:+.3f}s (tolerance {ONSET_TOL}s)")

    bars = [_run(png_args(bars_dir / f"bar{i}.png"))
            for i in range(len(nr.BEATS))]
    for i, beat in enumerate(nr.BEATS):
        t = beat.start + 1.0
        seen = []
        for pane, x in panes:
            idx, dist, margin = best_bar(_run(band_args(mp4, t, x)), bars)
            seen.append(idx)
            flag = "ok " if idx == i else "BAD"
            print(f"    {t:6.2f}s  {pane:3s}  {flag} bar{idx} "
                  f"(want bar{i}, dist {dist:.2f}, margin {margin:.2f})")
            if idx != i:
                problems.append(f"{mp4.name}: at {t:.2f}s the {pane} pane "
                                f"shows bar{idx}, beat {beat.name} is bar{i}")
            elif margin < min_margin:
                problems.append(f"{mp4.name}: at {t:.2f}s the {pane} pane "
                                f"matched bar{i} by only {margin:.2f}")
        if len(set(seen)) > 1:
            problems.append(f"{mp4.name}: at {t:.2f}s the panes disagree: "
                            f"{seen} — the two surfaces are not frame-locked")

    # Not optional. An earlier signature defaulted `work` to None and skipped
    # this silently, which in a module that fails closed everywhere else meant
    # the strongest check was the one easiest to lose.
    problems += check_app_area(mp4, work, panes, grid())
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path,
                    help="directory holding demo-sprint-*.mp4")
    ap.add_argument("--work", default=Path("/tmp/nh-sprint-demo"), type=Path,
                    help="build dir holding bars/bar*.png")
    args = ap.parse_args(argv)

    problems: list[str] = []
    checked = 0
    for name, panes in PANES.items():
        mp4 = args.out / f"demo-sprint-{name}.mp4"
        if not mp4.is_file():
            print(f"[skip] {mp4} not rendered")
            continue
        checked += 1
        problems += verify(mp4, args.work / "bars", panes, args.work)
    if not checked:
        print("no rendered clips found — nothing was proved")
        return 2
    print()
    if problems:
        print(f"FAIL — {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"PASS — {checked} clip(s). Every narration line starts on its map "
          f"second; every beat's caption is the one on screen; and on all "
          f"{len(grid())} sampled instants per pane ({GRID_STEP}s apart) the "
          f"app area's offset 0 falls inside the resolution band against its "
          f"own capture. Read the per-clip lines above for how WIDE that band "
          f"is: where it is wider than one step, a slip that size was not "
          f"resolvable at that instant. Frames between samples are not "
          f"inspected at all.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
