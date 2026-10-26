#!/usr/bin/env python3
"""Record a LIVE product demonstration of the real web dashboard.

Unlike the terminal-capture video, this records an actual browser session
against the actually-running backend and frontend: real clicks, real polling,
real confirmations, real pytest executions. Playwright captures the page as a
WebM; chapter markers are written alongside so narration can be muxed at exact
offsets.

If the UI does not show what a chapter claims, the recording aborts rather than
producing a misleading video.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

REPO = Path(__file__).resolve().parents[1]
AUDIO = REPO / "demo" / "audio"
OUT = REPO / "demo" / "record"
BASE = "http://127.0.0.1:3000"
VIEW = {"width": 1920, "height": 1080}
GAP = 0.7

CHAPTERS = [f"live{i:02d}" for i in range(1, 11)]


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def duration(path: Path) -> float:
    out = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, check=False,
    ).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    if not m:
        raise SystemExit(f"cannot probe {path}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    durs = []
    for c in CHAPTERS:
        p = AUDIO / f"{c}.mp3"
        if not p.is_file():
            raise SystemExit(f"missing narration {p}")
        durs.append(duration(p))
    print("narration durations:", [round(d, 2) for d in durs])

    markers: list[float] = []
    t0 = time.monotonic()

    def mark() -> None:
        markers.append(round(time.monotonic() - t0, 3))

    def gate(i: int) -> None:
        """Hold the current view until this chapter's narration would end."""
        if i == 0:
            return
        target = markers[i - 1] + durs[i - 1] + GAP
        wait = target - (time.monotonic() - t0)
        if wait > 0:
            time.sleep(wait)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport=VIEW,
            record_video_dir=str(OUT),
            record_video_size=VIEW,
            device_scale_factor=1,
        )
        page = context.new_page()
        page.set_default_timeout(30000)

        def see(text: str, timeout: int = 30000) -> None:
            try:
                page.get_by_text(text, exact=False).first.wait_for(timeout=timeout)
            except PWTimeout as exc:
                raise SystemExit(
                    f"ABORT: expected to see {text!r} on screen but the live UI never "
                    f"showed it. Refusing to record a demonstration of something that "
                    f"did not happen.\n{exc}"
                )

        def click(text: str, scope=None) -> None:
            loc = (scope or page).get_by_text(text, exact=False).first
            loc.wait_for(timeout=20000)
            loc.click()

        # ---- ch1: dashboard -------------------------------------------------
        page.goto(BASE + "/", wait_until="load")
        see("Dashboard")
        see("live · polling the backend")
        mark()

        # ---- ch2: project explorer -> start audit ---------------------------
        gate(1)
        click("Project Explorer")
        see("Merge requests")
        click("start audit")
        dialog = page.locator("div.fixed")
        dialog.get_by_text("yes, start the audit").click()
        page.wait_for_url(re.compile(r"/audits/\d+"), timeout=30000)
        audit_id = int(page.url.rstrip("/").rsplit("/", 1)[-1])
        see("Live event stream")
        mark()

        # ---- ch3: workspace streams to completion ---------------------------
        gate(2)
        see("100%", timeout=60000)
        see("completed")
        mark()

        # ---- ch4: findings and evidence -------------------------------------
        gate(3)
        page.goto(f"{BASE}/findings?audit={audit_id}", wait_until="load")
        see("Findings Explorer")
        see("corroborated")
        mark()

        # ---- ch5: execute the reproduction test ------------------------------
        gate(4)
        click("generate + run reproduction test")
        page.locator("div.fixed").get_by_text("execute in isolation").click()
        see("exit=", timeout=120000)
        mark()

        # ---- ch6: patch review + human approval ------------------------------
        gate(5)
        patches = page.evaluate(
            f"fetch('/api/audits/{audit_id}/patches').then(r=>r.json())"
        )
        plist = patches["patches"] if isinstance(patches, dict) else patches
        patch_id = plist[0]["id"]
        page.goto(f"{BASE}/patches/{patch_id}", wait_until="load")
        see("Proposed correction")
        click("approve")
        page.locator("div.fixed").get_by_text("yes, approve").click()
        see("approved")
        mark()

        # ---- ch7: verify by execution ----------------------------------------
        gate(6)
        click("verify by execution")
        page.locator("div.fixed").get_by_text("run verification now").click()
        see("post_fix_regression", timeout=180000)
        see("verified_for_tested_behavior")
        mark()

        # ---- ch8: verification results ---------------------------------------
        gate(7)
        page.goto(f"{BASE}/verifications?audit={audit_id}", wait_until="load")
        see("Verification Results")
        see("post_fix_reproduction")
        mark()

        # ---- ch9: final report -----------------------------------------------
        gate(8)
        page.goto(f"{BASE}/audits/{audit_id}/report", wait_until="load")
        see("Final report")
        see("defect_reproduced_and_fix_verified")
        mark()

        # ---- ch10: history + duo honesty --------------------------------------
        gate(9)
        page.goto(f"{BASE}/history", wait_until="load")
        see("Audit History")
        see("local_deterministic")
        mark()

        # hold the final view for the closing narration
        time.sleep(max(0.0, durs[9] + 1.6))

        video_path = page.video.path()
        context.close()
        browser.close()

    total = time.monotonic() - t0
    payload = {
        "video": str(video_path),
        "chapters": [
            {"index": i + 1, "clip": f"{CHAPTERS[i]}.mp3", "start": markers[i],
             "narration_seconds": round(durs[i], 3)}
            for i in range(len(markers))
        ],
        "total_seconds": round(total, 3),
    }
    (OUT / "markers.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"\nraw recording: {video_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
