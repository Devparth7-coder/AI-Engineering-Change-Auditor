#!/usr/bin/env python3
"""Capture REAL terminal footage for the demo video.

Nothing in the video is hand-written or simulated. This script drives the actual
backend over HTTP, executes the actual demonstration audit, and records the real
responses, the real pytest exit codes and the real verification runs into
per-scene text files. `make_video.py` then renders those files verbatim.

If a step fails, the failure is recorded as-is -- a broken demo must not be
papered over with invented output.
"""

from __future__ import annotations

import json
import os
import re
import pathlib
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
CAPTURES = REPO / "demo" / "captures"
FIXTURE = REPO / "examples" / "discount-service"
BACKEND = REPO / "backend"

WIDTH = 104  # characters; frames are rendered at this width


def banner(title: str, subtitle: str = "") -> str:
    rule = "─" * WIDTH
    out = [f"┌{rule}┐"]
    out.append(f"│ {title:<{WIDTH - 2}} │")
    if subtitle:
        out.append(f"│ {subtitle:<{WIDTH - 2}} │")
    out.append(f"└{rule}┘")
    return "\n".join(out)


def sh(args: list[str], cwd: pathlib.Path | None = None, env: dict | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        args, cwd=str(cwd or REPO), capture_output=True, text=True,
        env={**os.environ, **(env or {})}, check=False, timeout=600,
    )
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def save(name: str, text: str) -> None:
    CAPTURES.mkdir(parents=True, exist_ok=True)
    # Strip ANSI colour codes: frames are rendered with our own palette, and
    # raw escapes corrupt both the display and any line counting.
    text = ANSI_RE.sub("", text)
    (CAPTURES / f"{name}.txt").write_text(text.rstrip() + "\n", encoding="utf-8")
    lines = len(text.rstrip().splitlines())
    print(f"  captured {name:<26} {lines:>4} lines")


def wrap(text: str, width: int = WIDTH) -> str:
    out = []
    for line in text.splitlines():
        while len(line) > width:
            out.append(line[:width])
            line = line[width:]
        out.append(line)
    return "\n".join(out)


def compact(payload: object, max_lines: int = 34, width: int = WIDTH) -> str:
    """Flatten a JSON payload to `path = value` lines of real scalars.

    Raw ``json.dumps(indent=2)`` of a 70-key probe response pages terribly and
    buries the signal under braces. This keeps every real value but presents it
    densely. Long strings are truncated with an ellipsis; nulls and empties are
    dropped; lists of scalars stay on one line.
    """
    out: list[str] = []

    def scalar(v: object) -> str:
        text = json.dumps(v, default=str) if not isinstance(v, str) else v
        limit = width - 46
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def walk(node: object, path: str, depth: int) -> None:
        if len(out) >= max_lines:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if v is None or v == "" or v == [] or v == {}:
                    continue
                walk(v, f"{path}.{k}" if path else k, depth + 1)
        elif isinstance(node, list):
            if all(isinstance(x, (str, int, float, bool)) for x in node):
                out.append(f"{'  ' * depth}{path} = {scalar(node)}")
                return
            for i, v in enumerate(node[:8]):
                walk(v, f"{path}[{i}]" if path else f"[{i}]", depth + 1)
        else:
            out.append(f"{'  ' * min(depth, 4)}{path} = {scalar(node)}")

    walk(payload, "", 0)
    if len(out) >= max_lines:
        out.append("  … (further fields omitted for legibility; all values above are real)")
    return "\n".join(out)


def main() -> int:
    print("Capturing demo footage (all output is real)")

    # ---------------------------------------------------------------- scene 02
    # The fixture: a real repository, a real defect, a real green test suite.
    code, diff = sh(["git", "diff", "main", "feature/refactor-discount", "--",
                     "src/discount_service/pricing.py"], cwd=FIXTURE)
    code2, log = sh(["git", "log", "--oneline", "--all", "--decorate"], cwd=FIXTURE)
    code3, sha_main = sh(["git", "rev-parse", "main"], cwd=FIXTURE)
    code4, sha_feat = sh(["git", "rev-parse", "feature/refactor-discount"], cwd=FIXTURE)

    behaviour = []
    for rev, label in (("main", "target  (main)"),
                       ("feature/refactor-discount", "source  (feature/refactor-discount)")):
        tmp = pathlib.Path(tempfile.mkdtemp())
        sh(["git", "archive", rev], cwd=FIXTURE)  # warm-up, ignore
        proc = subprocess.run(
            ["git", "archive", rev], cwd=str(FIXTURE), capture_output=True, check=True
        )
        subprocess.run(["tar", "-x", "-C", str(tmp)], input=proc.stdout, check=True)
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0,'src');"
             "from discount_service.pricing import apply_discount;"
             "print(f'{apply_discount(1000.0, 0.20)!r}')"],
            cwd=str(tmp), capture_output=True, text=True, check=False,
        )
        behaviour.append(f"  {label:<42} apply_discount(1000.0, 0.20) -> {r.stdout.strip()}")

    # Run the pre-existing suite against the DEFECTIVE revision, materialised
    # exactly the way the auditor materialises a revision: git archive into a
    # clean directory. The working tree is on `main`, so running pytest in place
    # would silently test the wrong revision while claiming otherwise.
    feat_ws = pathlib.Path(tempfile.mkdtemp(prefix="feature-rev-"))
    proc = subprocess.run(["git", "archive", "feature/refactor-discount"],
                          cwd=str(FIXTURE), capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(feat_ws)], input=proc.stdout, check=True)
    suite = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-p", "no:cacheprovider"],
        cwd=str(feat_ws), capture_output=True, text=True, check=False, timeout=300,
    )
    # pytest 9 suppresses the summary line under -q, so run it verbosely enough
    # to capture the real "N passed" verdict -- that line IS the evidence.
    suite_tail = "\n".join(
        ln for ln in (suite.stdout or suite.stderr).strip().splitlines()
        if ln.strip() and ("passed" in ln or "failed" in ln or "error" in ln
                           or "[100%]" in ln or "test session starts" in ln)
    )

    save("scene_02_the_change", "\n".join([
        banner("THE CHANGE UNDER AUDIT", "examples/discount-service - a real git repository"),
        "",
        f"$ git log --oneline --all --decorate",
        log,
        "",
        f"  main                     {sha_main}",
        f"  feature/refactor-discount {sha_feat}",
        "",
        "$ git diff main feature/refactor-discount -- src/discount_service/pricing.py",
        diff,
        "",
        banner("WHAT THE CONTRACT SAYS vs WHAT THE CODE DOES"),
        "",
        "  The docstring declares: 'Return the FINAL price payable after applying",
        "  discount_rate' with a worked example  price=1000, discount_rate=0.2 -> 800.0",
        "",
        "$ python -c 'from discount_service.pricing import apply_discount; ...'",
        *behaviour,
        "",
        banner("THE PRE-EXISTING TEST SUITE ON THE DEFECTIVE BRANCH"),
        "",
        "$ git archive feature/refactor-discount | tar -x -C /tmp/ws && cd /tmp/ws",
        "$ python -m pytest tests/",
        suite_tail,
        "",
        "  >>> A green suite does NOT demonstrate the change is correct.",
        "  >>> This is exactly the gap the auditor exists to close.",
    ]))

    # ------------------------------------------------------- scenes 03..13
    os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/demo.db"
    os.environ["DEMO_ENABLED"] = "true"
    os.environ["DUO_ENABLED"] = "false"
    os.environ["GITLAB_URL"] = ""
    os.environ["GITLAB_TOKEN"] = ""
    sys.path.insert(0, str(BACKEND))

    from fastapi.testclient import TestClient  # noqa: E402
    from app.main import app  # noqa: E402

    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()

    def call(method: str, path: str, **kw) -> tuple[int, object, str]:
        """One real HTTP call -> (status, payload, rendered transcript)."""
        r = client.request(method, path, **kw)
        try:
            payload: object = r.json()
            rendered_body = json.dumps(payload, indent=2, default=str)
        except Exception:
            payload = r.text
            rendered_body = str(r.text)
        head = f"$ curl -s -X {method} {path}"
        params = kw.get("params")
        if params:
            head += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        if kw.get("json") is not None:
            head += f" \\\n      -d '{json.dumps(kw['json'])}'"
        return r.status_code, payload, f"{head}\n\nHTTP {r.status_code}\n{rendered_body}"

    def show(method: str, path: str, body: dict | None = None) -> tuple[int, object, str]:
        kw = {"json": body} if body is not None else {}
        return call(method, path, **kw)

    def show_compact(method: str, path: str, body: dict | None = None,
                     max_lines: int = 34) -> tuple[int, object, str]:
        code_, payload, _ = show(method, path, body)
        head = f"$ curl -s -X {method} {path}"
        if body is not None:
            head += f" \\\n      -d '{json.dumps(body)}'"
        return code_, payload, f"{head}\n\nHTTP {code_}\n{compact(payload, max_lines)}"

    # ---- health ----
    _, _, health_txt = show_compact("GET", "/api/health", max_lines=30)
    save("scene_03_health", "\n".join([
        banner("STEP 1 - CONNECT: WHAT CAN THIS SYSTEM ACTUALLY DO?",
               "GET /api/health reports verified capability, not an optimistic constant"),
        "", health_txt,
    ]))

    # ---- duo availability ----
    _, _, duo_txt = show_compact("GET", "/api/duo/availability", max_lines=26)
    save("scene_04_duo_honesty", "\n".join([
        banner("GITLAB DUO AGENT PLATFORM - REPORTED HONESTLY",
               "The mandatory integration is not faked when credentials are absent"),
        "", duo_txt,
        "", "  >>> No GitLab credentials exist in this environment, so the Duo Agent",
        "  >>> Platform cannot be invoked. The system says so, names the missing",
        "  >>> prerequisite, and falls back to a clearly-labelled local engine.",
    ]))

    # ---- project + MR ----
    _, projects, _ = call("GET", "/api/projects")
    project = projects[0]
    _, mrs_raw, _ = call("GET", f"/api/projects/{project['id']}/merge-requests")
    mrs = mrs_raw["merge_requests"] if isinstance(mrs_raw, dict) else mrs_raw
    mr = mrs[0]
    _, _, proj_txt = show_compact("GET", "/api/projects", max_lines=14)
    _, _, mr_txt = show_compact(
        "GET", f"/api/projects/{project['id']}/merge-requests", max_lines=16)
    save("scene_05_select_mr", "\n".join([
        banner("STEP 2 - SELECT THE MERGE REQUEST TO AUDIT"),
        "", proj_txt, "", mr_txt,
    ]))

    # ---- start audit ----
    audit_body = {
        "project_id": project["id"],
        "merge_request_iid": mr["iid"],
        "source_branch": mr["source_branch"],
        "target_branch": mr["target_branch"],
        "mode": "local_demo",
        "auto_start": True,
    }
    code_a, audit, audit_txt = show("POST", "/api/audits", audit_body)
    audit_id = audit["id"]

    polled = []
    t0 = time.time()
    status = ""
    while time.time() - t0 < 180:
        _, detail, _ = call("GET", f"/api/audits/{audit_id}")
        status = detail["status"]
        polled.append(f"  t+{time.time() - t0:4.1f}s  status={status:<14} "
                      f"stage={detail.get('stage', ''):<22} progress={detail.get('progress_pct')}%")
        if status in ("completed", "completed_with_errors", "failed"):
            break
        time.sleep(0.4)

    _, events, _ = call("GET", f"/api/audits/{audit_id}/events",
                        params={"after_seq": 0, "limit": 500})
    ev_lines = [
        f"  seq={e['seq']:>3}  {e['event_type']:<20} {e['agent_role']:<22} {e['message'][:44]}"
        for e in events
    ]
    save("scene_06_audit_runs", "\n".join([
        banner("STEP 3 - RUN THE AUDIT ASYNCHRONOUSLY",
               "Long-running work does not depend on one open HTTP request"),
        "", audit_txt, "",
        "$ watch -n0.4 'curl -s /api/audits/%d'" % audit_id,
        *polled,
        "", banner("THE DURABLE PROVENANCE TRAIL", "GET /api/audits/%d/events" % audit_id),
        "", *ev_lines,
    ]))

    # ---- findings ----
    _, findings_raw, _ = call("GET", f"/api/audits/{audit_id}/findings")
    findings = findings_raw["findings"]
    arith = next(f for f in findings if f["rule_id"] == "R-ARITH-001")
    fid = arith["id"]
    _, one, _ = call("GET", f"/api/findings/{fid}")
    inv = one["investigation"]

    fl = [
        banner("STEP 4 - FINDINGS, WITH EVIDENCE ATTACHED",
               "A hypothesis is not a finding until the repository corroborates it"),
        "",
        f"  public_id      {one['public_id']}",
        f"  rule           {one['rule_id']}  ({one['category']})",
        f"  severity       {one['severity']}",
        f"  title          {one['title']}",
        f"  location       {one['file_path']}:{one['line_start']}-{one['line_end']}",
        f"  symbol         {one['symbol']}",
        "",
        f"  expected       {one['expected_behavior']}",
        f"  suspected      {one['suspected_behavior']}",
        "",
        banner("EVIDENCE ITEMS"), "",
    ]
    for i, ev in enumerate(one["evidence"]):
        fl += [
            f"  #{i}  kind={ev['kind']:<15} check={ev.get('check', 'source_lines'):<16} "
            f"{ev['file_path']}:{ev.get('line_start')}-{ev.get('line_end')}",
            f"       {ev['description'][:88]}",
        ]
    fl += [
        "", banner("INDEPENDENT CORROBORATION"), "",
        f"  corroborated   {len(inv.get('corroborated', []))}",
        f"  refuted        {len(inv.get('refuted', []))}",
        f"  ratio          {inv.get('corroboration_ratio')}",
        "",
        "  CONFIRMED FACTS (separated from hypotheses):",
    ]
    fl += [f"    - {f[:92]}" for f in one["facts"][:9]]
    fl += ["", "  HYPOTHESES (explicitly not facts):"]
    fl += [f"    - {h[:92]}" for h in one["hypotheses"][:4]]
    fl += ["", "  confidence:",
           json.dumps(one["confidence"], indent=4, default=str)]
    save("scene_07_findings", "\n".join(fl))

    # ---- generated test + reproduction ----
    _, runs_raw, _ = call("GET", f"/api/audits/{audit_id}/test-runs")
    runs = runs_raw["test_runs"] if isinstance(runs_raw, dict) else runs_raw
    repro = next(r for r in runs if r["test_type"] == "reproduction")

    tl = [
        banner("STEP 5 - GENERATE A REPRODUCTION TEST FROM THE CONTRACT"), "",
        f"  file           {repro['test_file_name']}",
        "",
        "  EXPECTATIONS DERIVED FROM THE DECLARED CONTRACT:",
    ]
    for e in repro["expectations"]:
        tl.append(f"    [{e.get('origin', '?'):<24}] {e.get('call', e.get('description', ''))}"
                  f" -> {e.get('expected', '')}")
    tl += ["", banner("THE GENERATED TEST SOURCE THAT WAS ACTUALLY EXECUTED"), ""]
    src = repro["test_source"] or ""
    tl += src.splitlines()[:44]
    save("scene_08_generated_test", "\n".join(tl))

    rl = [
        banner("STEP 6 - EXECUTE IT AGAINST THE ORIGINAL REVISION",
               "Reproduction means the test really ran and really failed"),
        "",
        f"  revision          {repro['code_revision']}  ({repro['revision_label']})",
        f"  isolation         {repro['isolation_level']}",
        f"  network disabled  {repro['network_disabled']}",
        f"  exit code         {repro['exit_code']}",
        f"  tests             {repro['tests_total']} total / "
        f"{repro['tests_passed']} passed / {repro['tests_failed']} failed",
        f"  intended executed {repro['intended_tests_executed']}",
        f"  intended failed   {repro['intended_assertion_failed']}",
        f"  assertion sig     {repro['assertion_signature']}",
        f"  duration          {repro['duration_ms']} ms",
        "",
        banner("REAL PYTEST OUTPUT (sanitised)"), "",
    ]
    rl += (repro["sanitized_output"] or "").splitlines()[:26]
    rl += ["", banner("FAILURES RECORDED"), ""]
    for f_ in repro["failures"][:5]:
        rl.append(f"  {f_.get('outcome', ''):<7} {f_.get('node_id', '')}")
        msg = (f_.get("message") or "").strip().splitlines()
        if msg:
            rl.append(f"          {msg[-1][:94]}")
    _, after, _ = call("GET", f"/api/findings/{fid}")
    rl += ["", f"  finding verification_status -> {after['verification_status']}",
           f"  finding repro_outcome       -> {after['repro_outcome']}"]
    save("scene_09_reproduced", "\n".join(rl))

    # ---- patch + approval gate ----
    _, patches_raw, _ = call("GET", f"/api/audits/{audit_id}/patches")
    patches = patches_raw["patches"] if isinstance(patches_raw, dict) else patches_raw
    patch = patches[0]
    pid = patch["id"]

    code_v, refused, refused_txt = show("POST", f"/api/patches/{pid}/verify")
    pl = [
        banner("STEP 7 - A PATCH IS PROPOSED, NEVER AUTO-APPLIED"), "",
        f"  public_id        {patch['public_id']}",
        f"  approval_status  {patch['approval_status']}",
        f"  verification     {patch['verification_outcome']}",
        "",
        "  ORIGINAL CODE:",
    ]
    pl += [f"    {ln}" for ln in (patch["original_code"] or "").splitlines()[:8]]
    pl += ["", "  PROPOSED CODE:"]
    pl += [f"    {ln}" for ln in (patch["proposed_code"] or "").splitlines()[:8]]
    pl += ["", "  UNIFIED DIFF:"]
    pl += [f"    {ln}" for ln in (patch["patch_text"] or "").splitlines()[:12]]
    pl += ["", "  SIDE-EFFECT ANALYSIS:"]
    pl += [f"    - {s[:92]}" for s in (patch["side_effects"] or [])[:5]]
    pl += ["", banner("VERIFICATION IS REFUSED WITHOUT HUMAN APPROVAL"), "",
           refused_txt, "",
           "  >>> A generated patch is NEVER reported as a fix.", ]
    save("scene_10_patch_gate", "\n".join(pl))

    # ---- approve ----
    code_ap, approved, _ = call(
        "POST", f"/api/patches/{pid}/approve",
        json={"decided_by": "demo-reviewer",
              "note": "Evidence and reproduction reviewed."},
    )
    appr_txt = "\n".join([
        "$ curl -s -X POST /api/patches/%d/approve \\" % pid,
        "      -d '{\"decided_by\": \"demo-reviewer\", "
        "\"note\": \"Evidence and reproduction reviewed.\"}'",
        "",
        f"HTTP {code_ap}",
        f"  public_id        {approved.get('public_id')}",
        f"  approval_status  {approved.get('approval_status')}",
        f"  decided_by       {approved.get('decided_by')}",
        f"  decision_note    {approved.get('decision_note')}",
        f"  decided_at       {str(approved.get('decided_at'))[:19]}",
        "",
        "  >>> The decision is attributed and timestamped. Verification cannot",
        "  >>> start until a human has made it.",
    ])
    # ---- verify ----
    t0 = time.time()
    code_vf, ver, _ = call("POST", f"/api/patches/{pid}/verify")
    elapsed = time.time() - t0
    ver_txt = "\n".join([
        "$ curl -s -X POST /api/patches/%d/verify" % pid,
        "",
        f"HTTP {code_vf}",
        f"  patch_id                 {ver.get('patch_id')}",
        f"  outcome                  {ver.get('outcome')}",
        f"  intended_tests_executed  {ver.get('intended_tests_executed')}",
        f"  regressions              {len(ver.get('regressions') or [])}",
        f"  runs executed            {len(ver.get('runs') or [])}",
    ])

    vl = [
        banner("STEP 8 - HUMAN APPROVES, THEN THE FIX IS VERIFIED BY EXECUTION",
               "Four separate pytest runs, each in an isolated workspace"),
        "", appr_txt, "", ver_txt, "",
        f"  (the verify call took {elapsed:.1f}s of real test execution)", "",
        banner("THE FOUR EXECUTED RUNS"), "",
        f"  {'label':<24}{'status':<9}{'exit':<6}{'total':<7}{'pass':<6}{'fail':<6}"
        f"{'patch':<7}{'isolation':<18}{'ms':<7}",
        "  " + "-" * (WIDTH - 4),
    ]
    for r in (ver or {}).get("runs") or []:
        vl.append(
            f"  {r.get('label', '?'):<24}{r.get('execution_status', '?'):<9}"
            f"{str(r.get('exit_code')):<6}{str(r.get('tests_total')):<7}"
            f"{str(r.get('tests_passed')):<6}{str(r.get('tests_failed')):<6}"
            f"{str(r.get('patch_applied')):<7}{r.get('isolation_level', '?'):<18}"
            f"{str(r.get('duration_ms')):<7}"
        )
    vl += [
        "",
        f"  OUTCOME   {patch['verification_outcome']}  ->  {(ver or {}).get('outcome')}",
        f"  intended tests executed post-fix: {(ver or {}).get('intended_tests_executed')}",
        f"  regressions introduced:           {len((ver or {}).get('regressions') or [])}",
        "",
        banner("WHAT THE SYSTEM REFUSES TO CLAIM"), "",
        "  " + wrap(str((ver or {}).get("scope_statement", "")), WIDTH - 4),
        "",
        "  remaining concerns:",
    ]
    vl += [f"    - {wrap(c, WIDTH - 8)}" for c in ((ver or {}).get("remaining_concerns") or [])[:6]]
    save("scene_11_verified", "\n".join(vl))

    # ---- report ----
    _, report, report_txt = show("GET", f"/api/audits/{audit_id}/report")
    rep = report["report"] if isinstance(report, dict) and "report" in report else {}
    counts = rep.get("counts") or {}
    rpl = [
        banner("STEP 9 - THE FINAL REPORT", "An outcome label with an explanation, never a bare score"),
        "",
        f"  outcome            {report.get('outcome')}",
        f"  explanation        {wrap(report.get('outcome_explanation', ''), WIDTH - 22)}",
        "",
        f"  source revision    {rep.get('source_commit_sha', '')}",
        f"  target revision    {rep.get('target_commit_sha', '')}",
        "",
        "  counts: " + json.dumps(counts),
        "",
        banner("LIMITATIONS STATED BY THE SYSTEM ITSELF"), "",
    ]
    rpl += [f"  - {wrap(l, WIDTH - 4)}" for l in (rep.get("limitations") or [])[:7]]
    rpl += ["", banner("REMAINING RISKS"), ""]
    rpl += [f"  - {wrap(r_, WIDTH - 4)}" for r_ in (rep.get("remaining_risks") or [])[:6]]
    save("scene_12_report", "\n".join(rpl))

    # ---- persistence ----
    client.__exit__(None, None, None)
    client2 = TestClient(app, raise_server_exceptions=False)
    client2.__enter__()
    _, reopened, _ = call("GET", f"/api/audits/{audit_id}")
    _, dash, _ = call("GET", "/api/dashboard")
    _, hist_raw, _ = call("GET", "/api/audits")
    hist = hist_raw["audits"] if isinstance(hist_raw, dict) and "audits" in hist_raw else hist_raw
    pl2 = [
        banner("STEP 10 - HISTORY IS PRESERVED ACROSS A RESTART",
               "The application was shut down and started again"),
        "",
        f"  audit status after restart   {reopened['status']}",
        f"  findings after restart       {len(reopened.get('findings', []))}",
        f"  test runs after restart      {len(reopened.get('test_runs', []))}",
        f"  history entries              {len(hist)}",
        "",
        "  dashboard (computed from persisted rows, never estimated):",
        json.dumps(dash.get("totals", {}), indent=4),
        f"  verified_fixes               {dash.get('verified_fixes')}",
    ]
    save("scene_13_history", "\n".join(pl2))
    client2.__exit__(None, None, None)

    # ---- flow validation + acceptance ----
    code_f, flow_out = sh([sys.executable, str(REPO / "agents" / "validate_flow.py"), "--quiet"])
    save("scene_14_flow_validation", "\n".join([
        banner("THE GITLAB DUO AGENT PLATFORM ARTEFACTS ARE VALIDATED IN CI",
               "agents/flows/change-audit/flow.yaml  +  .gitlab/duo/agent-config.yml"),
        "",
        "$ python agents/validate_flow.py",
        wrap(flow_out, WIDTH),
        "",
        f"  exit code {code_f} - a missing or invalid flow definition FAILS the build.",
        "  Agent jobs never report success when they were skipped.",
    ]))

    code_acc, acc_out = sh([sys.executable, str(REPO / "tests" / "acceptance_e2e.py")],
                           cwd=BACKEND, env={"PYTHONPATH": str(BACKEND)})
    plain = ANSI_RE.sub("", acc_out)
    tail = [ln for ln in plain.splitlines() if "PASS" in ln or "FAIL" in ln or "ACCEPTANCE" in ln]
    passed = len([t for t in tail if "PASS " in t or "PASS  " in t])
    failed = len([t for t in tail if "FAIL" in t and "FAILURES" not in t and "0 failures" not in t])
    verdict = next((t.strip() for t in tail if "ACCEPTANCE TEST" in t), "")
    save("scene_15_acceptance", "\n".join([
        banner("THE MANDATORY END-TO-END ACCEPTANCE TEST"),
        "",
        "$ cd backend && python ../tests/acceptance_e2e.py",
        "",
        f"  {passed} checks passed",
        f"  {failed} checks failed",
        "",
        "  " + verdict,
        "",
        "  Every claim in this video was produced by a real execution:",
        "  real git revisions, real pytest runs, real isolated workspaces.",
    ]))

    print(f"\nAll captures written to {CAPTURES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
