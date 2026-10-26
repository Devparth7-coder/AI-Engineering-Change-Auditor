# Demo script — AI Engineering Change Auditor

**Runtime:** 2:54 (target: under 3 minutes)  
**Video:** `demo/ai-engineering-change-auditor-demo.mp4` (1920x1080, H.264/AAC)  
**Footage:** every frame is unedited output captured by `demo/capture_footage.py`,
which drives the real backend over HTTP and executes real tests. Nothing on screen
is typed by hand, mocked, or simulated.

## Reproducing the video

```bash
# 1. capture real footage (drives the API, runs pytest for real)
PYTHONPATH=backend python3 demo/capture_footage.py

# 2. narration clips live in demo/audio/n01.mp3 .. n10.mp3

# 3. render and mux
python3 demo/make_video.py
```

## Timeline

| # | Segment | On-screen evidence | Narration |
|---|---------|--------------------|-----------|
| 1 | **Introduction** (0:00) | `title` | Can we trust a proposed code change, and what evidence proves it works? This is the AI Engineering Change Auditor. |
| 2 | **The change under audit** (0:07) | `02_the_change` | A real merge request. One line changed: apply_discount now returns price times discount_rate. Its own docstring says it must return the FINAL price — a thousand at twenty percent should be eight hundred. It returns two hundred. And all sixteen existing tests still pass. |
| 3 | **Capability and Duo honesty** (0:28) | `03_health, 04_duo_honesty` | The system first reports what it can actually do — database, isolation tier, GitLab, Duo — probed, never assumed. The Duo Agent Platform integration is mandatory, so its absence is stated plainly: no credentials here, the missing prerequisite named, and the audit falls back to a clearly-labelled local deterministic engine. Nothing is ever passed off as Duo. |
| 4 | **Start the audit** (0:55) | `05_select_mr, 06_audit_runs` | We select the merge request and start the audit. It runs asynchronously, so a long investigation never depends on one open HTTP request, and every stage writes to a durable provenance trail. |
| 5 | **Findings and evidence** (1:09) | `07_findings` | A hypothesis is not a finding until the repository corroborates it. Each evidence item declares how it can be checked, and the investigator verifies it independently. Facts stay separate from hypotheses, and confidence is explicitly uncalibrated — there is no invented accuracy score anywhere. |
| 6 | **Reproduce by execution** (1:29) | `08_generated_test, 09_reproduced` | From the declared contract it generates a reproduction test, then executes it against the original revision in an isolated workspace with network disabled. Pytest really exits one. Four assertions fail — and they are the intended ones. The defect is now reproduced, not merely suspected. |
| 7 | **The human approval gate** (1:49) | `10_patch_gate` | A minimal patch is proposed, with its side effects analysed. Verification is refused with HTTP 409 until a human approves. A generated patch is never reported as a fix. |
| 8 | **Verified by execution** (2:02) | `11_verified` | After approval, four separate pytest runs execute: baseline reproduction fails, baseline regression passes, post-fix reproduction passes, post-fix regression passes. Only now is the fix verified for tested behaviour — with a plain statement of what that does not prove. |
| 9 | **Report and history** (2:23) | `12_report, 13_history` | The report gives an explained outcome label, the exact revisions, remaining risks and its own limitations. Everything persists: restart the application and the audit is still there. |
| 10 | **CI validation and close** (2:36) | `14_flow_validation, 15_acceptance, end` | The Duo flow definition is validated in CI, and a missing flow fails the build. Finally, the mandatory acceptance test: fourteen stages, ninety-one checks, zero failures. Every claim in this video came from a real execution. |

## What the demo proves

The distinction the whole project rests on:

```
AI hypothesis  ->  evidence-backed finding  ->  reproduced defect
               ->  proposed fix  ->  VERIFIED BY EXECUTION
```

A generated patch is **never** reported as a fix. Verification is refused with
HTTP 409 until a human approves, and the `verified_for_tested_behavior` outcome
is only reachable after four separate pytest runs have actually executed:

| Run | Purpose | Result in the demo |
|-----|---------|--------------------|
| `baseline_reproduction` | prove the test really fails before the fix | exit 1, 4 failed / 2 passed |
| `baseline_regression` | prove the suite was green before the fix | exit 0, 16 passed |
| `post_fix_reproduction` | prove the fix makes the test pass | exit 0, 6 passed |
| `post_fix_regression` | prove the fix broke nothing detected | exit 0, 16 passed |

## Honest limitations shown on screen

* GitLab Duo Agent Platform is **unavailable** in this environment (no credentials).
  The flow YAML and Flows API client are real and validated in CI, but the run shown
  uses the local deterministic engine and is labelled `local_deterministic` throughout.
* Isolation is the `linux_namespace` tier, not a VM. The report says so.
* `verified_for_tested_behavior` covers the tested behaviour only — the report refuses
  to claim whole-application correctness.
* No accuracy percentage is quoted anywhere, because none has been measured.
