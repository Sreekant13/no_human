"""Prove the freeze gate on REAL encodes, including the cases it exists for.

Frames are GENERATED HERE as raw PPM and encoded with x264 exactly as the cut
is. An earlier version of this probe built the clips with ffmpeg `-vf`
expressions; the commas inside `mod(t*5,8)` were eaten as filtergraph
separators, every clip came out genuinely static, and the probe cheerfully
reported that the gate caught all four adversaries. It had caught nothing — it
was reading a broken instrument. Generating the pixels removes that whole class
of doubt, and the frames are checked for motion BEFORE they are encoded.

Four 8 s clips over the same busy still background:

  A. moving     a block steps to a new cell every 6 frames — LOCALIZED motion,
                the shape a UI change has.                        want PASS
  B. frozen     the background, unchanged, for the whole clip.    want FAIL
  C. rampmask   a STRONG global luma ramp over the frozen clip.   want FAIL
  D. driftmask  the CHEAPEST global ramp that still silences
                freezedetect — one level per frame. This is the
                one that defeated the first draft of the check.   want FAIL

    PYTHONPATH=src python -m e2e.demo_video._freeze_probe

Takes about a minute. Run it after touching any threshold in
`verify_sync`'s freeze section -- the numbers there were set from this
probe's output, not from reasoning, and D exists because reasoning got
one of them wrong.
"""
import subprocess
from pathlib import Path

from . import verify_sync as vs

FFMPEG = "/opt/homebrew/bin/ffmpeg"
W, H, FPS = 640, 360, 30
DUR = 8.0
N = int(FPS * DUR)
OUT = Path("/tmp/nh-freeze-probe")


def background() -> bytearray:
    """Mid grey with a bright 32 px grid — busy on purpose. A flat near-black
    field hides a luma ramp inside the quantiser, which would make C and D
    weaker adversaries than they are meant to be."""
    buf = bytearray()
    for y in range(H):
        for x in range(W):
            grid = (x % 32 < 2) or (y % 32 < 2)
            buf += bytes((200, 210, 224) if grid else (96, 106, 120))
    return buf


BG = background()


def with_block(frame_i: int) -> bytearray:
    """The background plus a 56 px block that jumps cell to cell."""
    buf = bytearray(BG)
    cell = (frame_i // 6) % 64
    bx, by = 32 + (cell % 8) * 72, 24 + (cell // 8) * 40
    for y in range(by, min(by + 56, H)):
        row = y * W * 3
        for x in range(bx, min(bx + 56, W)):
            buf[row + x * 3:row + x * 3 + 3] = bytes((76, 154, 255))
    return buf


def shifted(offset: int) -> bytearray:
    return bytearray(min(255, max(0, b + offset)) for b in BG)


def clip_frames(kind: str):
    for i in range(N):
        if kind == "moving":
            yield with_block(i)
        elif kind == "frozen":
            yield BG
        elif kind == "rampmask":
            # +-40 levels, a swing nobody could miss.
            yield shifted(int(40 * ((i % 60) / 60.0 * 2 - 1)))
        elif kind == "driftmask":
            # ONE level per frame, sawtooth. 1/255 = 0.0039 of full scale,
            # just over freezedetect's 0.003 floor -- the cheapest silence.
            yield shifted(i % 24)


def build(kind: str) -> Path:
    d = OUT / kind
    d.mkdir(parents=True, exist_ok=True)
    header = f"P6\n{W} {H}\n255\n".encode()
    prev, moved_pairs = None, 0
    for i, buf in enumerate(clip_frames(kind)):
        (d / f"f{i:04d}.ppm").write_bytes(header + bytes(buf))
        if prev is not None and prev != buf:
            moved_pairs += 1
        prev = bytes(buf)
    dst = OUT / f"{kind}.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-framerate", str(FPS),
         "-i", str(d / "f%04d.ppm"), "-c:v", "libx264", "-crf", "20",
         "-preset", "veryfast", "-pix_fmt", "yuv420p", "-g", str(FPS),
         str(dst)], check=True)
    print(f"[build] {kind}: {moved_pairs}/{N - 1} consecutive PRE-ENCODE "
          f"frame pairs differ")
    return dst


def main() -> None:
    OUT.mkdir(exist_ok=True)
    clips = [("A moving", build("moving"), False),
             ("B frozen", build("frozen"), True),
             ("C rampmask", build("rampmask"), True),
             ("D driftmask", build("driftmask"), True)]

    samples = tuple(float(i) for i in range(1, int(DUR) - 2))
    rows = []
    for label, mp4, want_fail in clips:
        print(f"\n=== {label}  ({mp4.name})")
        problems = vs.check_freeze(mp4, DUR, samples)
        failed = bool(problems)
        ok = failed == want_fail
        print(f"  VERDICT: {'FAIL' if failed else 'PASS'}   "
              f"(wanted {'FAIL' if want_fail else 'PASS'})  -> "
              f"{'correct' if ok else '*** WRONG ***'}")
        for p in problems[:2]:
            print(f"    - {p}")
        rows.append((label, failed, want_fail, ok))

    print("\n---- summary ----")
    for label, failed, want, ok in rows:
        print(f"  {label:12s} got {'FAIL' if failed else 'PASS'}  "
              f"want {'FAIL' if want else 'PASS'}  {'OK' if ok else 'WRONG'}")
    print("ALL CORRECT" if all(r[3] for r in rows) else "SOME WRONG")


if __name__ == "__main__":
    main()
