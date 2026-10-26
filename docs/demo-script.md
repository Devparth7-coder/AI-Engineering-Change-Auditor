# Demo script — AI Engineering Change Auditor (live product demonstration)

**Runtime:** 2:41 (target: under 3 minutes)
**Video:** `demo/ai-engineering-change-auditor-live-demo.mp4` (1920×1080, H.264/AAC)
**Footage:** a real browser session against the real running product, captured by
`demo/record_walkthrough.py` (Playwright, headless Chromium, screen recording).
Every click, poll, confirmation dialog and pytest execution on screen actually
happened: the recorder drives `http://127.0.0.1:3000` (Next.js production build)
which proxies `/api/*` to the FastAPI backend, which executes real `pytest`
invocations in isolated workspaces. Nothing is typed by hand, mocked, scripted
into the DOM, or simulated.

**Honesty gate:** the recorder asserts, per chapter, that the live UI really shows
what the chapter claims (`see(...)`); if the expected text never appears the run
aborts with an explanation instead of recording a demonstration of something that
did not happen. Chapter start offsets are written to `demo/record/markers.json`
from wall-clock time as each narration clip begins, so the voice-over is locked to
what is on screen.

## Reproducing the video

```bash
# 0. start the product for real
cd backend  && PYTHONPATH=. python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 &
cd frontend && npm run build && npx next start -H 0.0.0.0 -p 3000 &

# 1. record the live walkthrough (aborts if the UI contradicts the script)
python3 demo/record_walkthrough.py          # writes demo/record/*.webm + markers.json

# 2. mux the narration (demo/audio/live01.mp3 .. live10.mp3) at the recorded offsets
python3 demo/mux_live.py                    # -> demo/ai-engineering-change-auditor-live-demo.mp4
```

A fresh database is recommended so the history shown at the end contains exactly
the audit performed in the video (`rm -f var/auditor.db*` before starting).

## Timeline

Narration column summarises the spoken audio (`demo/audio/liveNN.mp3`); timings are
the measured chapter start offsets from the recorded run.

| # | Start | On screen (live UI) | Narration (summary) |
|---|-------|---------------------|---------------------|
| 1 | 0:01 | **Dashboard**, empty: `No audits yet`, all counters read from persisted rows (0), footer chips `api ok`, `isolation linux_namespace`, `duo unavailable`, `demo fixture ready` | What the product answers — can we trust this change, and what executed evidence proves it — and that every number on screen is read from the database, never estimated. |
| 2 | 0:16 | **Project Explorer** → merge request `feature/refactor-discount → main` → confirmation dialog `yes, start the audit` → **Audit Workspace** with live event stream | Picking the project and the merge request, the explicit confirmation, and the fact that the audit runs asynchronously — a long investigation never depends on one open HTTP request. |
| 3 | 0:32 | Workspace streaming to `completed 100%`: durable provenance trail (`change_investigated`, `test_executed`, `patch_proposed`, `report_generated`, …), session panel with engine `local_deterministic` and both commit SHAs | The provenance trail: who did what, in which stage, with which engine and against which exact revisions — and that the audit completed while we watched it poll. |
| 4 | 0:43 | **Findings Explorer**: F-5-001 `critical / reproduced`, expected behaviour (800) vs suspected behaviour (200), evidence items `diff_hunk`, `source_excerpt`, `contract_quote` with `corroborated 9 / refuted 0` | A hypothesis only becomes a finding once the repository corroborates it; each evidence item declares how it can be checked independently; confidence is never invented. |
| 5 | 1:05 | Click `generate + run reproduction test` → confirm `execute in isolation` → real pytest result appears: `exit=1 · 2/6 passed · status=failed · isolation=linux_namespace` | The reproduction test is generated from the declared contract and executed against the *original* revision with the network off; pytest really exits 1 and the intended assertions fail — reproduced, not suspected. |
| 6 | 1:24 | **Patch Review** P-5-001: proposed correction diff, side-effect analysis, decision record; confirm `yes, approve` with a reviewer note | A minimal patch is proposed and analysed, and nothing happens to any branch until a human approves; a generated patch is never reported as a fix. |
| 7 | 1:42 | Confirm `run verification now` → `Executed verification runs`: `baseline_reproduction exit 1`, `baseline_regression`, `post_fix_reproduction`, `post_fix_regression` → badge `verified_for_tested_behavior` | Verification means four separate pytest invocations in isolated workspaces: the test fails before the fix, the suite was green before, the test passes after, the suite stays green after. Only then is the fix "verified for tested behaviour". |
| 8 | 1:59 | **Verification Results**: every run bound to patch SHA, source/target commit SHAs, isolation tier, duration, passed/total | What verification does and does not prove: runs are bound to exact revisions and the recorded isolation tier, and the scope statement says so in plain language. |
| 9 | 2:11 | **Final report**: explained outcome label `defect_reproduced_and_fix_verified`, counts, remaining risks, recommended next steps, the report's own limitations | The report explains its outcome instead of asserting it, and lists its own limitations and remaining risks — including what no test in this audit can prove. |
| 10 | 2:24 | **Audit History**: exactly one session, `completed`, engine `local_deterministic`, 2 findings, 6 runs; sidebar chip `duo unavailable` | Everything persists across restarts; and the standing honesty note — the mandatory GitLab Duo Agent Platform integration is real but cannot run here without credentials, so this audit is labelled local deterministic everywhere and no Duo result is simulated. |

## What the demo proves

The distinction the whole project rests on, exercised live in the browser:

```
AI hypothesis  ->  evidence-backed finding  ->  reproduced defect
               ->  proposed fix  ->  human approval  ->  VERIFIED BY EXECUTION
```

A generated patch is **never** reported as a fix. Verification is refused with
HTTP 409 until a human approves, and `verified_for_tested_behavior` is only
reachable after four separate pytest runs have actually executed:

| Run | Purpose | Result in the demo |
|-----|---------|--------------------|
| `baseline_reproduction` | prove the test really fails before the fix | exit 1, 4 failed / 2 passed |
| `baseline_regression` | prove the suite was green before the fix | exit 0, 16 passed |
| `post_fix_reproduction` | prove the fix makes the test pass | exit 0, 6 passed |
| `post_fix_regression` | prove the fix broke nothing detected | exit 0, 16 passed |

## Honest limitations shown on screen

* GitLab Duo Agent Platform is **unavailable** in this environment (no credentials).
  The Flow Registry v1 YAML, the agent config and the Flows API client are real and
  validated in CI, but the run shown uses the local deterministic engine and is
  labelled `local_deterministic` in the session panel, the history and the report.
  The dashboard and workspace say so in a warning banner; nothing is passed off as Duo.
* Isolation here is `linux_namespace` (unshare + rlimits, no network), which the
  security documentation explicitly describes as **not** a hard security boundary;
  the Docker executor is used automatically when Docker is present.
* The audited repository is the bundled deterministic fixture
  (`examples/discount-service`), because no GitLab instance is reachable. With
  `GITLAB_URL`/`GITLAB_TOKEN` set, the same screens drive real projects and MRs.
* Confidence is deliberately uncalibrated: corroborated/refuted evidence counts are
  shown instead of invented accuracy percentages.

## Appendix — earlier terminal/slideshow capture

`demo/ai-engineering-change-auditor-demo.mp4` (2:54) is the earlier API-driven
capture rendered as titled slides over real terminal footage
(`demo/capture_footage.py` + `demo/make_video.py`, narration `demo/audio/n01..n10.mp3`).
It is kept as an appendix only; the live product demonstration above is the
primary demo video.
