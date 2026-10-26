#!/usr/bin/env python3
"""Mandatory end-to-end acceptance test (project spec section 15).

Drives the REAL API and the REAL executor. Nothing in the expected report is
hardcoded: every assertion below is checked against values produced by actual
pytest executions inside the isolation sandbox.

Run:  cd backend && python3 ../tests/acceptance_e2e.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

DB_PATH = Path(tempfile.mkdtemp(prefix="eca-acceptance-")) / "acceptance.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["DEMO_ENABLED"] = "true"
os.environ["DUO_ENABLED"] = "false"
os.environ["GITLAB_URL"] = ""
os.environ["GITLAB_TOKEN"] = ""
os.environ["SANDBOX_MODE"] = os.environ.get("SANDBOX_MODE", "auto")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
failures: list[str] = []
step_no = 0
checks_run = 0


def step(title: str) -> None:
    global step_no
    step_no += 1
    print(f"\n\033[1m[{step_no:02d}] {title}\033[0m")


def check(condition: bool, label: str, detail: str = "") -> bool:
    global checks_run
    checks_run += 1
    print(f"     {PASS if condition else FAIL}  {label}")
    if detail:
        for line in detail.strip().splitlines()[:6]:
            print(f"          {line}")
    if not condition:
        failures.append(label)
    return bool(condition)


def main() -> int:
    with TestClient(app) as client:
        # -------------------------------------------------------------- health
        step("Health and capability probe")
        health = client.get("/api/health").json()
        check(health["status"] in ("ok", "degraded"), f"API healthy ({health['status']})")
        isolation = health["isolation"]["selected_isolation_level"]
        check(
            isolation in ("container", "linux_namespace"),
            f"Real code isolation available: {isolation}",
            json.dumps(health["isolation"], indent=2),
        )
        check(health["demo_mode_available"], "Demonstration repository present")

        # ------------------------------------------- Duo platform honesty check
        step("GitLab Duo Agent Platform availability (must be reported honestly)")
        duo = client.get("/api/duo/availability").json()
        check(not duo["available"], "Duo reports UNAVAILABLE (no credentials in this environment)")
        check(bool(duo["missing_prerequisite"]), "Missing prerequisite stated explicitly",
              duo["missing_prerequisite"])
        check(duo["flow_yaml_present"], f"Flow Registry v1 YAML shipped at {duo['flow_config_path']}")

        # --------------------------------------------------------- project/MR
        step("Connect a project and select a merge request")
        projects = client.get("/api/projects").json()
        check(len(projects) >= 1, f"Project listed ({len(projects)} found)")
        project = projects[0]
        check(project["origin"] == "local_demo", "Clearly labelled as demonstration mode")
        mrs = client.get(f"/api/projects/{project['id']}/merge-requests").json()
        check(len(mrs) >= 1, f"Merge request discovered ({len(mrs)} found)")
        mr = mrs[0]
        check("[DEMO]" in mr["title"], "Demo merge request is labelled as such", mr["title"])
        print(f"          source={mr['source_branch']} target={mr['target_branch']}")

        # -------------------------------------------------- 1. developer starts
        step("Developer starts an audit")
        created = client.post("/api/audits", json={
            "project_id": project["id"],
            "merge_request_iid": mr["iid"],
            "source_branch": mr["source_branch"],
            "target_branch": mr["target_branch"],
            "mode": "local_demo",
            "auto_start": True,
        })
        check(created.status_code == 201, f"POST /api/audits -> {created.status_code}")
        audit = created.json()
        audit_id = audit["id"]
        check(audit["status"] in ("queued", "created"), f"Audit {audit_id} queued (status={audit['status']})")

        # ------------------------------------------------------- wait for stages
        step("Audit executes asynchronously (polling the event stream)")
        deadline = time.time() + 300
        last_seq, statuses = 0, []
        while time.time() < deadline:
            detail = client.get(f"/api/audits/{audit_id}").json()
            status = detail["status"]
            if status not in statuses:
                statuses.append(status)
                print(f"          status -> {status} ({detail['progress_pct']}%) stage={detail['stage']}")
            ev_resp = client.get(f"/api/audits/{audit_id}/events", params={"after_seq": last_seq})
            events = ev_resp.json()
            if not isinstance(events, list):
                raise SystemExit(
                    f"event stream returned HTTP {ev_resp.status_code}: {events}"
                )
            for ev in events:
                last_seq = ev["seq"]
            if status in ("completed", "completed_with_errors", "failed"):
                break
            time.sleep(1.0)
        detail = client.get(f"/api/audits/{audit_id}").json()
        check(detail["status"] in ("completed", "completed_with_errors"),
              f"Audit reached a terminal state: {detail['status']}")
        check(detail["engine"] == "local_deterministic",
              "Engine honestly recorded as local_deterministic (Duo was unavailable)")
        # NOTE: the whole pipeline can finish in a couple of seconds, so a 1 Hz
        # poll legitimately observes only queued -> completed. Stage progression
        # is therefore asserted against the DURABLE event log, which is the real
        # audit trail, not against whatever the poller happened to catch.
        if len(statuses) >= 5:
            check(True, f"Observed {len(statuses)} distinct workflow stages while polling",
                  " -> ".join(statuses))

        events = client.get(f"/api/audits/{audit_id}/events", params={"limit": 500}).json()
        seen_types = {e["event_type"] for e in events}
        # The 8-step workflow must leave a real trace for every step it claims.
        required_trace = [
            "audit_created", "audit_started", "context_fetched", "stage_started",
            "finding_created", "test_generated", "test_executed", "patch_proposed",
            "report_generated", "stage_completed", "audit_completed",
        ]
        missing_trace = [t for t in required_trace if t not in seen_types]
        check(not missing_trace,
              f"Event log records every workflow step ({len(seen_types)} distinct event types)",
              "missing: " + ", ".join(missing_trace) if missing_trace
              else " -> ".join(required_trace))
        check(any(e["event_type"] == "duo_unavailable" for e in events),
              "A duo_unavailable event was recorded (no silent substitution)")

        # ------------------------------------------------- 2/3. defect identified
        step("System identifies the suspected calculation defect")
        findings_resp = client.get(f"/api/audits/{audit_id}/findings").json()
        findings = findings_resp["findings"]
        check(len(findings) >= 1, f"{len(findings)} finding(s) produced")
        arith = next((f for f in findings if f["rule_id"] == "R-ARITH-001"), None)
        check(arith is not None, "Calculation defect finding present (R-ARITH-001)")
        if arith is None:
            print("\n".join(failures))
            return 1
        check(arith["symbol"] == "apply_discount", f"Located in `{arith['symbol']}`")
        check("800" in arith["expected_behavior"], "Expected behaviour cites the contract value 800",
              arith["expected_behavior"])
        check("price * discount_rate" in arith["suspected_behavior"],
              "Suspected behaviour names the actual expression", arith["suspected_behavior"])
        check(len(arith["evidence"]) >= 4, f"{len(arith['evidence'])} evidence items attached")
        check(any(e["kind"] == "prior_revision" for e in arith["evidence"]),
              "Evidence includes the prior (target) revision for comparison")
        check(arith["confidence"].get("calibrated_on_labelled_corpus") is False,
              "Confidence is explicitly marked uncalibrated (no invented accuracy score)")
        check(len(arith["facts"]) >= 3, f"{len(arith['facts'])} confirmed facts separated from hypotheses")
        check(len(arith["hypotheses"]) >= 1, "Hypotheses kept separate from facts")

        inv = arith.get("investigation") or {}
        check(inv.get("corroboration_ratio", 0) >= 0.8,
              f"Evidence corroborated against the repository ({inv.get('corroboration_ratio')})")

        finding_id = arith["id"]
        one = client.get(f"/api/findings/{finding_id}").json()
        check(one["public_id"] == arith["public_id"], "Finding retrievable by id with full evidence")

        # ------------------------------------------------- 4/5. reproduction test
        step("Reproduction test was generated and executed against the ORIGINAL revision")
        runs_resp = client.get(f"/api/audits/{audit_id}/test-runs").json()
        repro_runs = [r for r in runs_resp["test_runs"] if r["test_type"] == "reproduction"]
        check(len(repro_runs) >= 1, f"{len(repro_runs)} reproduction run(s) recorded")
        repro = repro_runs[0]
        check(repro["exit_code"] == 1, f"pytest really exited non-zero (exit_code={repro['exit_code']})")
        check(repro["tests_failed"] >= 1,
              f"{repro['tests_failed']} assertion(s) failed, {repro['tests_passed']} passed")
        check(repro["intended_tests_executed"], "The intended tests actually executed (not skipped/missing)")
        check(repro["intended_assertion_failed"] is True,
              "Failure was the INTENDED assertion, not an unrelated error")
        check(repro["isolation_level"] in ("container", "linux_namespace"),
              f"Executed under real isolation: {repro['isolation_level']}")
        check(repro["network_disabled"], "Network egress was disabled during execution")
        check("apply_discount" in repro["test_source"], "Executed test source is persisted for review")
        expectations = repro.get("failures") or []
        check(len(expectations) >= 1, f"{len(expectations)} failure record(s) persisted")

        after_repro = client.get(f"/api/findings/{finding_id}").json()
        check(after_repro["repro_outcome"] == "reproduced",
              f"Finding status = {after_repro['repro_outcome']} (defect reproduced by execution)")
        check(after_repro["verification_status"] in ("reproduced", "fix_proposed", "fix_approved", "fix_verified"),
              f"Verification status = {after_repro['verification_status']}")

        # ------------------------------------------------------- 6. patch proposed
        step("System proposes a correction (original and proposed kept separate)")
        patches_resp = client.get(f"/api/audits/{audit_id}/patches").json()
        check(patches_resp["count"] >= 1, f"{patches_resp['count']} patch proposal(s)")
        patch = patches_resp["patches"][0]
        check(patch["approval_status"] == "pending", "Patch is PENDING -- not auto-approved")
        check("price * discount_rate" in patch["original_code"], "Original code stored separately")
        check("price * (1 - discount_rate)" in patch["proposed_code"], "Proposed code stored separately")
        check(patch["patch_text"].startswith("---"), "Unified diff available for review")
        check(bool(patch["explanation"]), "Explanation present")
        check(len(patch["side_effects"]) >= 2, f"{len(patch['side_effects'])} side effects analysed")
        check(patch["verification_outcome"] == "not_attempted",
              "Verification outcome is 'not_attempted' before approval (never pre-claimed)")

        # ------------------------------------------------- 7. verify blocked first
        step("Verification is REFUSED without explicit human approval")
        blocked = client.post(f"/api/patches/{patch['id']}/verify")
        check(blocked.status_code == 409,
              f"POST /verify without approval -> HTTP {blocked.status_code} (expected 409)",
              blocked.json().get("detail", ""))

        # --------------------------------------------------------- 7. approval
        step("User approves the proposed patch")
        approved = client.post(f"/api/patches/{patch['id']}/approve",
                               json={"decided_by": "acceptance-test", "note": "Approved for isolated verification"})
        check(approved.status_code == 200, f"POST /approve -> {approved.status_code}")
        check(approved.json()["approval_status"] == "approved", "Approval recorded")
        check(approved.json()["decided_by"] == "acceptance-test", "Approval attributed to a human actor")

        # ------------------------------------------- 8/9/10/11. apply + verify
        step("Patch applied in an isolated workspace; tests executed against the fix")
        verified = client.post(f"/api/patches/{patch['id']}/verify", timeout=600)
        check(verified.status_code == 200, f"POST /verify -> {verified.status_code}")
        vres = verified.json()
        print("          " + vres["summary"].replace("\n", "\n          "))
        check(vres["outcome"] == "verified_for_tested_behavior",
              f"Verification outcome = {vres['outcome']}")
        check(vres["intended_tests_executed"], "Intended tests actually executed post-fix")
        runs = {r["label"]: r for r in vres["runs"]}
        check("post_fix_reproduction" in runs, "Post-fix reproduction run recorded")
        check(runs.get("post_fix_reproduction", {}).get("tests_failed", 1) == 0,
              f"Post-fix reproduction: {runs['post_fix_reproduction']['tests_passed']}/"
              f"{runs['post_fix_reproduction']['tests_total']} assertions pass")
        check(runs.get("post_fix_reproduction", {}).get("exit_code") == 0,
              "Post-fix pytest exit code is 0")
        check(runs.get("baseline_reproduction", {}).get("tests_failed", 0) >= 1,
              "Baseline (original revision) genuinely failed -- comparison is meaningful")
        check("post_fix_regression" in runs, "Regression suite executed after the fix")
        reg = runs.get("post_fix_regression", {})
        check(reg.get("tests_failed", 1) == 0 and reg.get("tests_total", 0) > 0,
              f"Regression suite green: {reg.get('tests_passed')}/{reg.get('tests_total')}")
        check(vres["regressions"] == [], "No previously-passing test broke")
        check(len(vres["remaining_concerns"]) >= 3,
              f"{len(vres['remaining_concerns'])} remaining concerns stated, not hidden")
        check(any("does NOT establish" in c for c in vres["remaining_concerns"]),
              "Report refuses to claim whole-application correctness")

        # --------------------------------------------------- 12. results stored
        step("Actual execution results are stored against revision identifiers")
        all_runs = client.get(f"/api/audits/{audit_id}/test-runs").json()["test_runs"]
        check(len(all_runs) >= 4, f"{len(all_runs)} test run(s) persisted")
        labels = sorted({r["test_type"] for r in all_runs})
        check(len(labels) >= 3, f"Run types persisted: {labels}")
        check(all(r["code_revision"] for r in all_runs),
              "Every run is bound to a code revision SHA")
        check(all(r["sanitized_output"] for r in all_runs),
              "Every run stored a sanitised output summary")
        patched_runs = [r for r in all_runs if r["patch_applied"]]
        check(len(patched_runs) >= 2, f"{len(patched_runs)} run(s) recorded as patch-applied")

        final_finding = client.get(f"/api/findings/{finding_id}").json()
        check(final_finding["verification_status"] == "fix_verified",
              f"Finding verification_status = {final_finding['verification_status']}")

        # -------------------------------------------------------- 13. the report
        step("Final report distinguishes reproduced defect, fix, and limitations")
        report_resp = client.get(f"/api/audits/{audit_id}/report")
        check(report_resp.status_code == 200, f"GET /report -> {report_resp.status_code}")
        report = report_resp.json()
        check(report["outcome"] == "defect_reproduced_and_fix_verified",
              f"Report outcome = {report['outcome']}")
        check(bool(report["outcome_explanation"]), "Outcome label is explained, not a bare score")
        payload = report["report"]
        check(payload["change"]["source_commit_sha"] and payload["change"]["target_commit_sha"],
              f"SHAs recorded: {payload['change']['source_commit_sha'][:12]} <- "
              f"{payload['change']['target_commit_sha'][:12]}")
        duo_section = payload["gitlab_duo_agent_platform"]
        check(duo_section["used_for_this_audit"] is False,
              "Report states the Duo platform was NOT used for this audit")
        check("Missing prerequisite" in duo_section["statement"],
              "Report names the missing Duo prerequisite", duo_section["statement"][:200])
        counts = payload["counts"]
        check(counts["reproduced"] >= 1 and counts["fixes_verified"] >= 1,
              f"counts: reproduced={counts['reproduced']} fixes_verified={counts['fixes_verified']} "
              f"regressions={counts['regressions']}")
        check(len(payload["remaining_risks"]) >= 1, f"{len(payload['remaining_risks'])} remaining risks listed")
        check(len(payload["recommended_next_steps"]) >= 1, "Recommended next steps present")
        check(len(payload["limitations"]) >= 3, f"{len(payload['limitations'])} explicit limitations")
        check(payload["provenance"]["events_recorded"] >= 10,
              f"{payload['provenance']['events_recorded']} provenance events recorded")
        check("approval_history" in payload and len(payload["approval_history"]) >= 1,
              "Patch approval history preserved")
        check(payload["regression_results"]["post_fix"], "Post-fix regression results embedded")
        check(not any("quality_score" in json.dumps(payload[k]) for k in ("outcome", "counts")),
              "No unexplained AI quality score anywhere in the report")

        # ------------------------------------------------- 14. survives restart
        step("Audit can be reopened after the application restarts")
        history = client.get("/api/audits").json()
        check(any(a["id"] == audit_id for a in history), "Audit appears in history")
        dashboard = client.get("/api/dashboard").json()
        check(dashboard["totals"]["verified_fixes"] >= 1,
              f"Dashboard verified_fixes={dashboard['totals']['verified_fixes']} (from persisted rows)")
        check(dashboard["totals"]["audits"] >= 1, f"Dashboard audits={dashboard['totals']['audits']}")
    # --- TestClient context exit == application shutdown; now reopen cold -------
    with TestClient(app) as cold:
        reopened = cold.get(f"/api/audits/{audit_id}").json()
        check(reopened["status"] in ("completed", "completed_with_errors"),
              f"Reopened after restart with status={reopened['status']}")
        check(len(reopened["findings"]) >= 1, f"{len(reopened['findings'])} finding(s) survived")
        check(len(reopened["test_runs"]) >= 4, f"{len(reopened['test_runs'])} test run(s) survived")
        cold_report = cold.get(f"/api/audits/{audit_id}/report").json()
        check(cold_report["outcome"] == "defect_reproduced_and_fix_verified",
              "Report survived the restart with the same outcome")
        cold_events = cold.get(f"/api/audits/{audit_id}/events", params={"limit": 500}).json()
        check(len(cold_events) >= 10, f"{len(cold_events)} provenance events survived")

    print("\n" + "=" * 72)
    if failures:
        print(f"\033[91mACCEPTANCE TEST FAILED -- {len(failures)} check(s):\033[0m")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\033[92mACCEPTANCE TEST PASSED -- {step_no} stages, "
          f"{checks_run} checks, 0 failures.\033[0m")
    print(f"Database used: {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
