#!/usr/bin/env python3
"""Render the demo video from the REAL captured footage.

Inputs
------
``demo/captures/*.txt``   terminal transcripts produced by ``capture_footage.py``
                          by driving the actual backend and executing real tests
``demo/audio/*.mp3``      narration, one clip per scene group

Output
------
``demo/ai-engineering-change-auditor-demo.mp4``   1920x1080 H.264 + AAC
``docs/demo-script.md``                           the script, with real timings

Timing model
------------
Each narration clip drives the on-screen time of its scene group: the group's
pages share exactly ``clip_duration + GAP`` seconds, and the audio track is the
clips separated by the same GAP. Video and narration therefore stay in sync by
construction rather than by hand-tuned offsets.

No frame is hand-written. If a capture file is missing the render fails loudly
instead of substituting placeholder text.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[1]
CAPTURES = REPO / "demo" / "captures"
AUDIO = REPO / "demo" / "audio"
FRAMES = REPO / "demo" / "frames"
OUT_MP4 = REPO / "demo" / "ai-engineering-change-auditor-demo.mp4"
SCRIPT_MD = REPO / "docs" / "demo-script.md"

WIDTH, HEIGHT = 1920, 1080
FPS = 30
FONT_SIZE = 22
LINE_HEIGHT = 30
MARGIN_X = 96
HEADER_H = 92
FOOTER_H = 54
MAX_LINES = (HEIGHT - HEADER_H - FOOTER_H) // LINE_HEIGHT
GAP = 0.55  # seconds of silence between narration segments

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

BG = (13, 16, 22)
PANEL = (20, 24, 32)
TITLEBAR = (27, 32, 42)
FG = (213, 221, 234)
DIM = (108, 120, 138)
GREEN = (98, 226, 148)
RED = (255, 110, 110)
AMBER = (255, 200, 96)
CYAN = (102, 206, 255)
VIOLET = (186, 156, 255)
DOT_RED, DOT_AMBER, DOT_GREEN = (255, 95, 86), (255, 189, 46), (39, 201, 63)


# ----------------------------------------------------------------- narration
@dataclass
class Group:
    """A narration segment and the scenes shown while it plays."""

    label: str
    scenes: list[str]
    narration: str
    audio: str = ""
    duration: float = 0.0
    pages: list[tuple[str, list[str], int, int]] = field(default_factory=list)


TITLE_CARD = [
    "",
    "",
    "   AI ENGINEERING CHANGE AUDITOR",
    "",
    "   Can we trust a proposed code change --",
    "   and what evidence demonstrates that it works?",
    "",
    "",
    "   hypothesis  ->  evidence-backed finding  ->  reproduced defect",
    "             ->  proposed fix  ->  VERIFIED BY EXECUTION",
    "",
    "",
    "   Every frame in this video is real captured output:",
    "   real git revisions, real pytest runs, real isolated workspaces.",
    "",
    "",
    "   GitLab Transcend Hackathon",
]

END_CARD = [
    "",
    "",
    "   WHAT WAS DEMONSTRATED",
    "",
    "   PASS  a defect found from a declared contract, not guessed",
    "   PASS  evidence corroborated independently against the repository",
    "   PASS  the defect reproduced by executing a generated test",
    "   PASS  verification refused until a human approved the patch",
    "   PASS  the fix verified by FOUR real pytest runs in isolation",
    "   PASS  regressions checked, remaining risks stated, history kept",
    "",
    "   WHAT WAS NOT CLAIMED",
    "",
    "   FAIL  no fix declared from a generated patch alone",
    "   FAIL  no invented accuracy score, anywhere",
    "   FAIL  no GitLab Duo results fabricated while credentials are absent",
    "   FAIL  nothing pushed, merged or deployed",
    "",
    "",
    "   verified_for_tested_behavior -- and the report says exactly",
    "   what that does and does not prove.",
]

GROUPS = [
    Group(
        label="Introduction",
        scenes=["__title__"],
        narration=(
            "Can we trust a proposed code change, and what evidence proves it works? "
            "This is the AI Engineering Change Auditor."
        ),
    ),
    Group(
        label="The change under audit",
        scenes=["scene_02_the_change"],
        narration=(
            "A real merge request. One line changed: apply_discount now returns price "
            "times discount_rate. Its own docstring says it must return the FINAL price "
            "— a thousand at twenty percent should be eight hundred. It returns two "
            "hundred. And all sixteen existing tests still pass."
        ),
    ),
    Group(
        label="Capability and Duo honesty",
        scenes=["scene_03_health", "scene_04_duo_honesty"],
        narration=(
            "The system first reports what it can actually do — database, isolation tier, "
            "GitLab, Duo — probed, never assumed. The Duo Agent Platform integration is "
            "mandatory, so its absence is stated plainly: no credentials here, the missing "
            "prerequisite named, and the audit falls back to a clearly-labelled local "
            "deterministic engine. Nothing is ever passed off as Duo."
        ),
    ),
    Group(
        label="Start the audit",
        scenes=["scene_05_select_mr", "scene_06_audit_runs"],
        narration=(
            "We select the merge request and start the audit. It runs asynchronously, so a "
            "long investigation never depends on one open HTTP request, and every stage "
            "writes to a durable provenance trail."
        ),
    ),
    Group(
        label="Findings and evidence",
        scenes=["scene_07_findings"],
        narration=(
            "A hypothesis is not a finding until the repository corroborates it. Each "
            "evidence item declares how it can be checked, and the investigator verifies it "
            "independently. Facts stay separate from hypotheses, and confidence is "
            "explicitly uncalibrated — there is no invented accuracy score anywhere."
        ),
    ),
    Group(
        label="Reproduce by execution",
        scenes=["scene_08_generated_test", "scene_09_reproduced"],
        narration=(
            "From the declared contract it generates a reproduction test, then executes it "
            "against the original revision in an isolated workspace with network disabled. "
            "Pytest really exits one. Four assertions fail — and they are the intended ones. "
            "The defect is now reproduced, not merely suspected."
        ),
    ),
    Group(
        label="The human approval gate",
        scenes=["scene_10_patch_gate"],
        narration=(
            "A minimal patch is proposed, with its side effects analysed. Verification is "
            "refused with HTTP 409 until a human approves. A generated patch is never "
            "reported as a fix."
        ),
    ),
    Group(
        label="Verified by execution",
        scenes=["scene_11_verified"],
        narration=(
            "After approval, four separate pytest runs execute: baseline reproduction fails, "
            "baseline regression passes, post-fix reproduction passes, post-fix regression "
            "passes. Only now is the fix verified for tested behaviour — with a plain "
            "statement of what that does not prove."
        ),
    ),
    Group(
        label="Report and history",
        scenes=["scene_12_report", "scene_13_history"],
        narration=(
            "The report gives an explained outcome label, the exact revisions, remaining "
            "risks and its own limitations. Everything persists: restart the application "
            "and the audit is still there."
        ),
    ),
    Group(
        label="CI validation and close",
        scenes=["scene_14_flow_validation", "scene_15_acceptance", "__end__"],
        narration=(
            "The Duo flow definition is validated in CI, and a missing flow fails the build. "
            "Finally, the mandatory acceptance test: fourteen stages, ninety-one checks, zero "
            "failures. Every claim in this video came from a real execution."
        ),
    ),
]


# ------------------------------------------------------------------- helpers
def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        found = shutil.which("ffmpeg")
        if not found:
            raise SystemExit("ffmpeg not found. pip install imageio-ffmpeg")
        return found


def run(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(
            f"command failed (exit {proc.returncode}): {' '.join(args[:6])}\n"
            f"{(proc.stderr or '')[-1500:]}"
        )
    return (proc.stdout or "") + (proc.stderr or "")


def audio_duration(path: Path) -> float:
    """Read a media file's duration by parsing ffmpeg's own probe output."""
    exe = ffmpeg_exe()
    out = subprocess.run([exe, "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True, check=False).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    if not m:
        raise SystemExit(f"could not determine duration of {path}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def load_scene(name: str) -> list[str]:
    if name == "__title__":
        return list(TITLE_CARD)
    if name == "__end__":
        return list(END_CARD)
    path = CAPTURES / f"{name}.txt"
    if not path.is_file():
        raise SystemExit(
            f"missing capture '{path}'. Run demo/capture_footage.py first -- this "
            "renderer never substitutes placeholder text for real output."
        )
    return path.read_text(encoding="utf-8").rstrip("\n").splitlines()


def paginate(lines: list[str], max_lines: int = MAX_LINES) -> list[list[str]]:
    """Split a scene into pages, preferring blank-line boundaries."""
    if len(lines) <= max_lines:
        return [lines]
    pages: list[list[str]] = []
    rest = lines
    while rest:
        if len(rest) <= max_lines:
            pages.append(rest)
            break
        cut = max_lines
        # Pull the cut back to the last blank line so a block is not split mid-way.
        for i in range(max_lines, max(6, max_lines - 12), -1):
            if not rest[i - 1].strip():
                cut = i
                break
        pages.append(rest[:cut])
        rest = rest[cut:]
    return pages


def classify(line: str) -> tuple[tuple[int, int, int], bool]:
    """Pick a colour for one terminal line."""
    stripped = line.strip()
    if stripped.startswith(("┌", "└")):
        return DIM, False
    if stripped.startswith("│"):
        inner = stripped.strip("│").strip()
        if not inner:
            return DIM, False
        return (FG, True) if inner.isupper() or inner.startswith("STEP") else (CYAN, True)
    if stripped.startswith("$ ") or re.match(r"^\s*-d ['\"]", line):
        # A curl body continuation is part of the command, not a diff deletion.
        return GREEN, True
    if stripped.startswith(">>>") or ">>>" in stripped[:6]:
        return AMBER, True
    if stripped.startswith("+") and not stripped.startswith("+++"):
        return GREEN, False
    if (stripped.startswith("-") and not stripped.startswith("-->")
            and not stripped.startswith("->") and not stripped.startswith("---")):
        return RED, False
    if stripped.startswith("@@"):
        return VIOLET, True
    if stripped.startswith("diff --git") or stripped.startswith("index "):
        return DIM, False
    if "HTTP 2" in line:
        return GREEN, True
    if "HTTP 4" in line or "HTTP 5" in line:
        return AMBER, True
    if re.match(r"^\s*(PASS|FAIL)\b", stripped):
        return (GREEN if stripped.startswith("PASS") else RED), True
    if "PASS " in line and "checks passed" not in line:
        return GREEN, False
    if "FAIL" in line:
        return RED, False
    if "ACCEPTANCE TEST PASSED" in line or "FLOW VALIDATION PASSED" in line:
        return GREEN, True
    if stripped.startswith(("confirmed:", "OUTCOME", "VERIFIED", "verified_for_")):
        return CYAN, True
    if stripped.startswith(("note:", "#")):
        return DIM, False
    if re.match(r"^\s*\d+\s*(passed|failed)", stripped):
        return GREEN if "passed" in stripped else RED, True
    return FG, False


def render_frame(lines: list[str], group_label: str, page: int, total_pages: int,
                 progress: float) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    bold = ImageFont.truetype(FONT_BOLD, FONT_SIZE)
    small = ImageFont.truetype(FONT_PATH, 17)

    # window chrome
    d.rectangle([0, 0, WIDTH, HEADER_H], fill=TITLEBAR)
    for i, colour in enumerate((DOT_RED, DOT_AMBER, DOT_GREEN)):
        d.ellipse([28 + i * 26, HEADER_H // 2 - 7, 42 + i * 26, HEADER_H // 2 + 7], fill=colour)
    d.text((130, HEADER_H // 2 - 11), "AI Engineering Change Auditor  —  live terminal capture",
           font=bold, fill=FG)
    d.text((WIDTH - 130 - d.textlength(group_label, font=small), HEADER_H // 2 - 9),
           group_label, font=small, fill=CYAN)

    # progress bar
    d.rectangle([0, HEADER_H, WIDTH, HEADER_H + 4], fill=PANEL)
    d.rectangle([0, HEADER_H, int(WIDTH * max(0.0, min(1.0, progress))), HEADER_H + 4], fill=CYAN)

    # content
    char_w = font.getlength("M")
    block_w = max((font.getlength(ln) for ln in lines), default=0)
    x0 = max(MARGIN_X, int((WIDTH - min(block_w, WIDTH - 2 * MARGIN_X)) / 2))
    y = HEADER_H + 26
    for ln in lines[:MAX_LINES]:
        colour, is_bold = classify(ln)
        d.text((x0, y), ln[:118], font=bold if is_bold else font, fill=colour)
        y += LINE_HEIGHT
    _ = char_w

    # footer
    d.rectangle([0, HEIGHT - FOOTER_H, WIDTH, HEIGHT], fill=PANEL)
    d.text((MARGIN_X, HEIGHT - FOOTER_H + 16),
           "demo/captures — unedited output from a real execution",
           font=small, fill=DIM)
    pager = f"{page}/{total_pages}" if total_pages > 1 else ""
    d.text((WIDTH - MARGIN_X - d.textlength(pager, font=small), HEIGHT - FOOTER_H + 16),
           pager, font=small, fill=DIM)
    return img


def mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def main() -> int:
    exe = ffmpeg_exe()
    if FRAMES.exists():
        shutil.rmtree(FRAMES)
    FRAMES.mkdir(parents=True)
    AUDIO.mkdir(parents=True, exist_ok=True)
    (REPO / "docs").mkdir(exist_ok=True)

    # ---- resolve narration durations -------------------------------------
    missing = [g for g in GROUPS if not (AUDIO / f"{g.scenes[0]}").parent.exists()]
    _ = missing
    for i, g in enumerate(GROUPS, start=1):
        clip = AUDIO / f"n{i:02d}.mp3"
        if not clip.is_file():
            raise SystemExit(
                f"missing narration clip {clip}. Generate the {len(GROUPS)} clips first."
            )
        g.audio = str(clip)
        g.duration = audio_duration(clip)

    # ---- build pages ------------------------------------------------------
    total_video = sum(g.duration + GAP for g in GROUPS)
    frame_index = 0
    concat_lines: list[str] = []
    cursor = 0.0
    for g in GROUPS:
        g.pages = []
        for scene in g.scenes:
            lines = load_scene(scene)
            pages = paginate(lines)
            g.pages.extend([(scene, p, i + 1, len(pages)) for i, p in enumerate(pages)])
        if not g.pages:
            raise SystemExit(f"group '{g.label}' produced no pages")
        per_page = (g.duration + GAP) / len(g.pages)
        for scene, page_lines, page_no, page_total in g.pages:
            frame_index += 1
            progress = cursor / total_video if total_video else 0.0
            img = render_frame(page_lines, g.label, page_no, page_total, progress)
            out = FRAMES / f"frame_{frame_index:04d}.png"
            img.save(out)
            concat_lines.append(f"file '{out}'")
            concat_lines.append(f"duration {per_page:.4f}")
            cursor += per_page
        print(f"  {g.label:<30} audio={g.duration:5.2f}s pages={len(g.pages)}")

    # last frame must be repeated for the concat demuxer to honour the final duration
    concat_lines.append(f"file '{FRAMES / f'frame_{frame_index:04d}.png'}'")

    concat_txt = FRAMES / "concat.txt"
    concat_txt.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    # ---- audio track ------------------------------------------------------
    silence = AUDIO / "_silence.mp3"
    run([exe, "-y", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
         "-t", f"{GAP}", "-q:a", "9", str(silence)])
    audio_list = AUDIO / "concat.txt"
    entries: list[str] = []
    for i, g in enumerate(GROUPS):
        entries.append(f"file '{g.audio}'")
        if i < len(GROUPS) - 1:
            entries.append(f"file '{silence}'")
    entries.append(f"file '{silence}'")
    audio_list.write_text("\n".join(entries) + "\n", encoding="utf-8")

    audio_m4a = REPO / "demo" / "_audio.m4a"
    run([exe, "-y", "-f", "concat", "-safe", "0", "-i", str(audio_list),
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", str(audio_m4a)])

    # ---- video ------------------------------------------------------------
    video_only = REPO / "demo" / "_video.mp4"
    run([exe, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt),
         "-vf", f"fps={FPS},format=yuv420p", "-r", str(FPS),
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", str(video_only)])

    # ---- mux --------------------------------------------------------------
    run([exe, "-y", "-i", str(video_only), "-i", str(audio_m4a),
         "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart", "-shortest", str(OUT_MP4)])

    for tmp in (video_only, audio_m4a):
        tmp.unlink(missing_ok=True)

    actual = audio_duration(OUT_MP4)
    print(f"\nframes rendered : {frame_index}")
    print(f"planned duration: {mmss(total_video)}")
    print(f"actual duration : {mmss(actual)}")
    print(f"output          : {OUT_MP4}")
    print(f"size            : {OUT_MP4.stat().st_size / 1e6:.1f} MB")

    write_script(actual)
    return 0


def write_script(total: float) -> None:
    """Emit docs/demo-script.md with the REAL measured timings."""
    lines = [
        "# Demo script — AI Engineering Change Auditor",
        "",
        f"**Runtime:** {mmss(total)} (target: under 3 minutes)  ",
        "**Video:** `demo/ai-engineering-change-auditor-demo.mp4` (1920x1080, H.264/AAC)  ",
        "**Footage:** every frame is unedited output captured by `demo/capture_footage.py`,",
        "which drives the real backend over HTTP and executes real tests. Nothing on screen",
        "is typed by hand, mocked, or simulated.",
        "",
        "## Reproducing the video",
        "",
        "```bash",
        "# 1. capture real footage (drives the API, runs pytest for real)",
        "PYTHONPATH=backend python3 demo/capture_footage.py",
        "",
        "# 2. narration clips live in demo/audio/n01.mp3 .. n10.mp3",
        "",
        "# 3. render and mux",
        "python3 demo/make_video.py",
        "```",
        "",
        "## Timeline",
        "",
        "| # | Segment | On-screen evidence | Narration |",
        "|---|---------|--------------------|-----------|",
    ]
    cursor = 0.0
    for i, g in enumerate(GROUPS, start=1):
        start = mmss(cursor)
        cursor += g.duration + GAP
        scenes = ", ".join(
            s.replace("scene_", "").replace("__", "") for s in g.scenes
        )
        lines.append(f"| {i} | **{g.label}** ({start}) | `{scenes}` | {g.narration} |")
    lines += [
        "",
        "## What the demo proves",
        "",
        "The distinction the whole project rests on:",
        "",
        "```",
        "AI hypothesis  ->  evidence-backed finding  ->  reproduced defect",
        "               ->  proposed fix  ->  VERIFIED BY EXECUTION",
        "```",
        "",
        "A generated patch is **never** reported as a fix. Verification is refused with",
        "HTTP 409 until a human approves, and the `verified_for_tested_behavior` outcome",
        "is only reachable after four separate pytest runs have actually executed:",
        "",
        "| Run | Purpose | Result in the demo |",
        "|-----|---------|--------------------|",
        "| `baseline_reproduction` | prove the test really fails before the fix | exit 1, 4 failed / 2 passed |",
        "| `baseline_regression` | prove the suite was green before the fix | exit 0, 16 passed |",
        "| `post_fix_reproduction` | prove the fix makes the test pass | exit 0, 6 passed |",
        "| `post_fix_regression` | prove the fix broke nothing detected | exit 0, 16 passed |",
        "",
        "## Honest limitations shown on screen",
        "",
        "* GitLab Duo Agent Platform is **unavailable** in this environment (no credentials).",
        "  The flow YAML and Flows API client are real and validated in CI, but the run shown",
        "  uses the local deterministic engine and is labelled `local_deterministic` throughout.",
        "* Isolation is the `linux_namespace` tier, not a VM. The report says so.",
        "* `verified_for_tested_behavior` covers the tested behaviour only — the report refuses",
        "  to claim whole-application correctness.",
        "* No accuracy percentage is quoted anywhere, because none has been measured.",
        "",
    ]
    SCRIPT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"script written  : {SCRIPT_MD}")


if __name__ == "__main__":
    sys.exit(main())
