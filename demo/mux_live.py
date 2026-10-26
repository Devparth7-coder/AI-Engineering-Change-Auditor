"""Mux the recorded live walkthrough (webm, no audio) with the per-chapter narration.

Chapter start offsets come from demo/record/markers.json, which is written by
record_walkthrough.py from real wall-clock time as each narration clip is spoken.
That keeps the voice-over locked to what is actually on screen instead of being
laid over a slideshow with guessed timings.

Usage: python3 demo/mux_live.py [output.mp4]
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
RECORD = DEMO / "record"
AUDIO = DEMO / "audio"
MARKERS = RECORD / "markers.json"
DEFAULT_OUT = DEMO / "ai-engineering-change-auditor-live-demo.mp4"

FPS = 25
CRF = 20


def probe_duration(ffmpeg: str, path: Path) -> float:
    """Read `Duration:` from ffmpeg's stderr (ffprobe is not available here)."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True
    )
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            stamp = line.split(",", 1)[0].split(":", 1)[1].strip()
            hours, minutes, seconds = stamp.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    raise RuntimeError(f"could not read duration of {path}")


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    if not MARKERS.exists():
        print(f"missing {MARKERS} — run demo/record_walkthrough.py first", file=sys.stderr)
        return 2

    markers = json.loads(MARKERS.read_text())
    video = Path(markers["video"])
    chapters = markers["chapters"]
    if not video.exists():
        print(f"missing recording {video}", file=sys.stderr)
        return 2

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    video_seconds = probe_duration(ffmpeg, video)

    clips: list[tuple[Path, float]] = []
    for chapter in chapters:
        clip = AUDIO / chapter["clip"]
        if not clip.exists():
            print(f"missing narration {clip}", file=sys.stderr)
            return 2
        clips.append((clip, float(chapter["start"])))

    cmd: list[str] = [ffmpeg, "-hide_banner", "-y", "-i", str(video)]
    for clip, _ in clips:
        cmd += ["-i", str(clip)]

    parts = []
    for index, (_, start) in enumerate(clips, start=1):
        delay_ms = int(round(start * 1000))
        # adelay needs one value per channel; `all=1` covers any channel layout.
        parts.append(f"[{index}:a]adelay={delay_ms}:all=1[a{index}]")
    mix_inputs = "".join(f"[a{index}]" for index in range(1, len(clips) + 1))
    tail = max(0.0, video_seconds - (clips[-1][1] + probe_duration(ffmpeg, clips[-1][0])))
    parts.append(
        f"{mix_inputs}amix=inputs={len(clips)}:duration=longest:normalize=0,"
        f"apad=whole_dur={video_seconds + 0.5:.3f},"
        f"afade=t=in:st=0:d=0.35,"
        f"afade=t=out:st={video_seconds - min(tail, 1.5) - 0.2:.3f}:d={min(tail, 1.5) + 0.2:.3f},"
        "loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
    )

    cmd += [
        "-filter_complex",
        ";".join(parts),
        "-map",
        "0:v",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(CRF),
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(FPS),
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        "-shortest",
        str(out),
    ]

    print(f"video {video.name} = {video_seconds:.2f}s, {len(clips)} narration chapters")
    for chapter in chapters:
        print(
            f"  ch{chapter['index']:>2}  t={chapter['start']:>7.2f}s  "
            f"{chapter['clip']:<13} ({chapter['narration_seconds']:.2f}s)"
        )

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr[-4000:], file=sys.stderr)
        return proc.returncode

    final = probe_duration(ffmpeg, out)
    size_mb = out.stat().st_size / (1024 * 1024)
    print(
        f"\nwrote {out.relative_to(ROOT)}  duration={final:.2f}s "
        f"({int(final // 60)}:{final % 60:04.1f})  size={size_mb:.1f} MB"
    )
    if final > 180:
        print("WARNING: longer than the 3-minute limit", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
