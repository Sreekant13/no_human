"""Render the narration to one audio track, cut to the timing map.

    python -m e2e.demo_video.voiceover /tmp/nh-sprint-demo/audio

Each narration line is rendered SEPARATELY with macOS ``say`` and placed at
its own `Line.start` on a silent 78.000 s canvas, so the audio is frame-locked
to the same clock the recorders step — a single continuous render would drift
against the screen within three beats and there would be no way to pull it
back. Before assembling, every rendered line is measured (ffprobe) and the
build REFUSES to continue if any line overruns its window in the timing map:
audio racing the screen is caught here, at build time, never on camera.

The voice: ``say -v ?`` is enumerated at build time and an Enhanced/Premium
voice is preferred if one is installed. On a machine with only the compact
voices (this one, at the time of writing), the pick falls back through
PREFERRED_COMPACT — Samantha first. A compact voice is serviceable and the
track is still the deliverable that proves the sync; the SCRIPT and the
timing map are the durable artifact, and a professional VO (or a better TTS)
can be dropped onto the same map without touching anything else.

Outputs, in the given directory:

    line00.aiff …    one file per narration line (32-bit float @ 22050)
    narration.aiff   the whole script in one continuous say render — the
                     reference read, NOT used for muxing (no timing)
    narration.m4a    THE track: every line at its timing-map offset, mono
                     AAC — this is what compose.py muxes over both videos

The sample format is BEF32, not the LEF32 the first draft asked for:
measured on this machine, `say -o x.aiff --data-format=LEF32@22050` fails
with "Opening output file failed: fmt?" — AIFF is a big-endian container and
`say` refuses little-endian floats into it (LEF32 is accepted into .caf).
Big-endian float at the same 22050 Hz is the same audio; ffmpeg reads both.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from . import narration as nr

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"

#: The order to fall back through when no Enhanced/Premium voice is
#: installed. Samantha is the most natural of the en compact set; Daniel and
#: Karen are the best of the non-US accents; Fred and Albert are legacy
#: last resorts that at least always exist.
PREFERRED_COMPACT = ("Samantha", "Daniel", "Karen", "Moira", "Tessa",
                     "Fred", "Albert")

#: Words per minute handed to `say -r`. The default (~175) reads slightly
#: rushed against a screen; 165 is measured to fit every window with air.
RATE = 165

#: Seconds of slack a rendered line must leave inside its window. A line that
#: fits with 0.00 s spare collides with the next line's first syllable.
FIT_MARGIN = 0.10


def parse_voices(say_output: str) -> list[tuple[str, str]]:
    """``say -v ?`` output -> [(voice name, locale), ...].

    The name column can contain spaces and parentheses ("Eddy (English
    (UK))"); the locale is the first ``xx_XX`` token after it.
    """
    voices = []
    for line in say_output.splitlines():
        m = re.match(r"^(.*?)\s{2,}([a-z]{2}_[A-Z]{2})\s", line)
        if m:
            voices.append((m.group(1).strip(), m.group(2)))
    return voices


def pick_voice(voices: list[tuple[str, str]]) -> str:
    """The best installed English voice, by tier.

    Tier 1: any en_* voice marked Enhanced or Premium (Siri-class installs
    surface this way), preferring en_US, then the PREFERRED_COMPACT base name
    order. Tier 2: the compact fallbacks. A machine with no English voice at
    all is not a machine this demo can be built on.
    """
    english = [(n, l) for n, l in voices if l.startswith("en_")]
    if not english:
        raise SystemExit("no English voice installed; `say -v ?` lists none")

    def base(name: str) -> str:
        return name.split("(")[0].strip()

    premium = [(n, l) for n, l in english
               if "(Enhanced)" in n or "(Premium)" in n]
    if premium:
        premium.sort(key=lambda v: (
            v[1] != "en_US",
            _index_of(base(v[0]), PREFERRED_COMPACT),
            v[0]))
        return premium[0][0]
    for want in PREFERRED_COMPACT:
        for name, _locale in english:
            if base(name) == want:
                return name
    return english[0][0]


def _index_of(item: str, seq: tuple[str, ...]) -> int:
    return seq.index(item) if item in seq else len(seq)


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        check=True, capture_output=True, text=True)
    return float(out.stdout.strip())


def check_fit(durations: list[float]) -> None:
    """Refuse any line that overruns its timing-map window. The message names
    the line, because the fix is an edit to narration.py, not a retry."""
    problems = []
    for (line, window), dur in zip(nr.line_windows(), durations):
        if dur > window - FIT_MARGIN:
            problems.append(
                f"at {line.start:.2f}s: rendered {dur:.2f}s into a "
                f"{window:.2f}s window — {line.text[:60]!r}")
    if problems:
        raise SystemExit("NARRATION OVERRUNS ITS MAP:\n  "
                         + "\n  ".join(problems))


def mix_args(line_files: list[Path], out_m4a: Path) -> list[str]:
    """The ffmpeg invocation that places each line at its `Line.start` and
    emits the single mono AAC track. Pure, so tests can hold it still."""
    assert len(line_files) == len(nr.LINES)
    args: list[str] = [FFMPEG, "-y", "-loglevel", "error"]
    for f in line_files:
        args += ["-i", str(f)]
    delayed = "".join(
        f"[{i}:a]adelay={int(round(line.start * 1000))}:all=1[a{i}];"
        for i, line in enumerate(nr.LINES))
    joins = "".join(f"[a{i}]" for i in range(len(nr.LINES)))
    graph = (f"{delayed}{joins}amix=inputs={len(nr.LINES)}:normalize=0,"
             f"apad[mix]")
    args += ["-filter_complex", graph, "-map", "[mix]",
             "-t", f"{nr.DURATION:.3f}", "-ar", "44100", "-ac", "1",
             "-c:a", "aac", "-b:a", "96k", str(out_m4a)]
    return args


#: A local neural voice, if one is reachable. macOS `say` on a machine with
#: only COMPACT voices installed is the robot-sounding read this file used to
#: ship — formant synthesis, flat prosody, audibly not a person. Kokoro is an
#: 82M-parameter neural TTS (Apache-2.0, so it may ship commercially), runs
#: locally with no API key and no account, and is what an earlier product video
#: on this machine was narrated with.
#:
#: DETECTED, never required. There is no new declared dependency: if the CLI is
#: absent, or node is too old, the build falls straight back to `say` and says
#: so. That keeps the recorder runnable on a bare checkout, which is the whole
#: reason it lives in this repo.
#: Chosen by MEASUREMENT, not by ear-guessing. Across the five candidates
#: reading the same sample, pitch-motion (variation in zero-crossing rate over
#: voiced frames, a cheap prosody proxy) ranked: af_sky 1.86, af_nova 1.80,
#: bm_george 1.71, am_michael 1.43, am_adam 1.28. The first shipped read used
#: am_adam — the FLATTEST of the five — and the operator's verdict was that it
#: sounded robotic. Flat pitch is what "robotic" means acoustically, so the
#: measurement and the complaint agree.
#:
#: Speed dropped from 1.06 to 1.0: pushing a neural voice faster compresses the
#: prosody that makes it sound human, which is the opposite of what is wanted
#: here. The narration still fits its map — the pacing test and the render's own
#: measured-overrun check both enforce that, and they refuse rather than warn.
KOKORO_VOICE = "af_sky"
KOKORO_SPEED = "1.0"


def kokoro_cmd(text: str, dst: Path, voice: str = KOKORO_VOICE) -> list[str]:
    """The hyperframes invocation. Pure, so a test can hold the shape still."""
    return ["npx", "--yes", "hyperframes", "tts", text,
            "-o", str(dst), "-v", voice, "-s", KOKORO_SPEED]


def kokoro_available() -> bool:
    """True when the neural voice can actually render — probed, not assumed.

    Runs the real thing on a one-word script rather than checking for a binary
    on PATH: `npx` exists on almost every machine and will happily report
    success while the package underneath refuses (hyperframes needs node >= 22,
    and node 20 is what a stock install has). The only honest test is a render.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "probe.wav"
        try:
            r = subprocess.run(kokoro_cmd("Ready.", probe), capture_output=True,
                               text=True, timeout=180)
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0 and probe.is_file() and probe.stat().st_size > 1024


def engine_id() -> str:
    """Which renderer will actually be used, as a cache key component.

    The engine is DETECTED at build time, so the same script can produce two
    completely different reads on two machines. A cache keyed on the script
    alone would call those identical.
    """
    return f"kokoro:{KOKORO_VOICE}:{KOKORO_SPEED}" if kokoro_available() else "say"


def build(out_dir: Path, voice: str | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    if voice is None:
        listing = subprocess.run(["say", "-v", "?"], check=True,
                                 capture_output=True, text=True).stdout
        voice = pick_voice(parse_voices(listing))
    print(f"[vo] voice: {voice}  rate: {RATE} wpm")

    use_kokoro = kokoro_available()
    print(f"[vo] engine: {'kokoro (neural)' if use_kokoro else 'say (formant)'}")
    if not use_kokoro:
        print("[vo] NOTE: falling back to `say`. On a machine with only compact "
              "voices this reads as a robot. Install node >= 22 and the neural "
              "voice renders instead — no API key, no account.")

    line_files: list[Path] = []
    durations: list[float] = []
    for i, line in enumerate(nr.LINES):
        if use_kokoro:
            dst = out_dir / f"line{i:02d}.wav"
            subprocess.run(kokoro_cmd(line.tts_text, dst), check=True,
                           capture_output=True)
        else:
            dst = out_dir / f"line{i:02d}.aiff"
            subprocess.run(
                ["say", "-v", voice, "-r", str(RATE), "-o", str(dst),
                 "--data-format=BEF32@22050", line.tts_text], check=True)
        line_files.append(dst)
        durations.append(probe_duration(dst))
    check_fit(durations)

    # The reference read of the whole script, per the brief — one continuous
    # render, for listening to the copy without the gaps.
    subprocess.run(
        ["say", "-v", voice, "-r", str(RATE), "-o",
         str(out_dir / "narration.aiff"), "--data-format=BEF32@22050",
         " ".join(line.tts_text for line in nr.LINES)], check=True)

    out_m4a = out_dir / "narration.m4a"
    subprocess.run(mix_args(line_files, out_m4a), check=True)
    total = probe_duration(out_m4a)
    print(f"[vo] {len(line_files)} lines -> {out_m4a}  ({total:.3f}s)")
    if abs(total - nr.DURATION) > 0.1:
        raise SystemExit(f"track is {total:.3f}s, map says {nr.DURATION}")
    return out_m4a


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else
               "/tmp/nh-sprint-demo/audio"))
