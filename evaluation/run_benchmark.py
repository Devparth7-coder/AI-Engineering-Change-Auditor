#!/usr/bin/env python3
"""Reproducible benchmark for the AI Engineering Change Auditor (§10).

Every number this script prints is MEASURED in this run against the committed
corpus in ``evaluation/corpus/``.  Nothing is estimated, sampled from logs, or
hardcoded: each case is rebuilt as a real git repository, audited through the
real HTTP API (TestClient), reproduced and verified with real pytest runs in
isolated workspaces, and compared against the case's ground-truth file.

Metrics (definitions in docs/evaluation.md):

  defect_recall            injected defects with >=1 critical/high finding that
                           cites the injected file+function  /  injected defects
  finding_precision        defect-pointing findings on defective cases /
                           all defect-pointing findings (clean-case findings
                           are false positives and lower this)
  reproduction_rate        findings whose generated test executed and failed on
                           the intended assertion  /  findings with generated tests
  patch_verification_rate  patches reaching verified_for_tested_behavior /
                           patches whose verification was attempted
  regression_rate          post_fix_regression runs with exit != 0 / all such runs
  false_positive_rate      clean cases with >=1 defect-pointing finding / clean cases
  median_audit_duration    median wall-clock seconds per audit
  resource_consumption     pytest invocations, executed assertions, wall time;
                           credits are 0 because the LOCAL_ENGINE uses no LLM

Usage:
    cd backend && PYTHONPATH=. python3 ../evaluation/run_benchmark.py [--cases a,b] [--no-verify]

Results are written to evaluation/results/benchmark-<utc>.json (git-ignored;
the measured summary is quoted in docs/evaluation.md).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
CORPUS = REPO / "evaluation" / "corpus"
RESULTS = REPO / "evaluation" / "results"
sys.path.insert(0, str(BACKEND))

_DB = Path(tempfile.mkdtemp(prefix="eca-bench-")) / "bench.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["DUO_ENABLED"] = "false"
os.environ["GITLAB_URL"] = ""
os.environ["GITLAB_TOKEN"] = ""
os.environ["DEMO_ENABLED"] = "true"
os.environ["SANDBOX_KEEP_WORKSPACES"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Project  # noqa: E402


# --------------------------------------------------------------------------- #
# corpus plumbing
# --------------------------------------------------------------------------- #
def build_repo(case_dir: Path, dest: Path) -> None:
    """Materialise one corpus case as a real two-branch git repository."""
    dest.mkdir(parents=True, exist_ok=True)

    def run(*argv: str) -> None:
        proc = subprocess.run(argv, cwd=dest, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(argv)} failed: {proc.stderr}")

    env = {"GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@local",
           "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "bench@local"}
    old = dict(os.environ)
    os.environ.update(env)
    try:
        run("git", "init", "-q", "-b", "main")
        for rel in sorted((case_dir / "base").rglob("*")):
            if rel.is_file():
                target = dest / rel.relative_to(case_dir / "base")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(rel.read_bytes())
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "base: contract honoured")
        run("git", "checkout", "-q", "-b", "feature/change")
        for rel in sorted((case_dir / "change").rglob("*")):
            if rel.is_file():
                target = dest / rel.relative_to(case_dir / "change")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(rel.read_bytes())
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "change under audit")
    finally:
        os.environ.clear()
        os.environ.update(old)


DEFECT_POINTING_SEVERITIES = {"critical", "high"}


def is_defect_pointing(finding: dict, defect: dict | None) -> bool:
    """A finding 'points at' the injected defect when it cites the same file and
    names the injected function — independent of which rule produced it."""
    if finding.get("severity") not in DEFECT_POINTING_SEVERITIES:
        return False
    if defect is None:
        return True  # any critical/high finding on a clean case is a false positive
    blob = json.dumps(finding).lower()
    return (
        finding.get("file_path", "").endswith(defect["file"].lstrip("src/"))
        or defect["file"] in finding.get("file_path", "")
    ) and defect["function"].lower() in blob


# --------------------------------------------------------------------------- #
# one case end to end
# --------------------------------------------------------------------------- #
def run_case(client, case_dir: Path, workroot: Path, *, verify: bool) -> dict:
    gt = json.loads((case_dir / "ground_truth.json").read_text())
    repo = workroot / case_dir.name
    build_repo(case_dir, repo)

    with session_scope() as session:
        project = Project(
            name=f"bench-{case_dir.name}",
            namespace="benchmark",
            path_with_namespace=f"benchmark/{case_dir.name}",
            default_branch="main",
            local_path=str(repo),
            origin="benchmark",
        )
        session.add(project)
        session.commit()
        project_id = project.id

    started = time.monotonic()
    created = client.post("/api/audits", json={
        "project_id": project_id,
        "merge_request_iid": 1,
        "source_branch": "feature/change",
        "target_branch": "main",
        "mode": "local_demo",
        "auto_start": True,
    })
    assert created.status_code == 201, created.text
    audit_id = created.json()["id"]

    deadline = time.time() + 300
    detail = {}
    while time.time() < deadline:
        detail = client.get(f"/api/audits/{audit_id}").json()
        if detail["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(1.0)
    duration = time.monotonic() - started
    assert detail.get("status") == "completed", f"audit {audit_id}: {detail.get('status')}"

    findings = client.get(f"/api/audits/{audit_id}/findings").json()
    findings = findings["findings"] if isinstance(findings, dict) else findings
    defect = gt.get("injected_defect")
    pointing = [f for f in findings if is_defect_pointing(f, defect)]
    detected = bool(pointing) and defect is not None

    runs_before = _run_count(client, audit_id)

    patch_outcome = None
    patches = client.get(f"/api/audits/{audit_id}/patches").json()
    patches = patches["patches"] if isinstance(patches, dict) else patches
    if verify and gt.get("fix_expected") and patches:
        patch_id = patches[0]["id"]
        approved = client.post(f"/api/patches/{patch_id}/approve", json={
            "decided_by": "benchmark-harness",
            "note": "benchmark approval against ground truth",
        })
        assert approved.status_code == 200, approved.text
        verified = client.post(f"/api/patches/{patch_id}/verify", json={})
        assert verified.status_code == 200, verified.text
        body = verified.json()
        patch_outcome = {
            "outcome": body.get("outcome") or body.get("verification_outcome"),
            "runs": [r.get("test_type") for r in body.get("runs", [])],
            "regression_failures": sum(
                1 for r in body.get("runs", [])
                if r.get("test_type") == "post_fix_regression"
                and r.get("exit_code") not in (0, None)
            ),
        }

    runs = client.get(f"/api/audits/{audit_id}/test-runs").json()
    runs = runs["test_runs"] if isinstance(runs, dict) else runs
    # "reproduced" / "not_reproduced" both REQUIRE an executed pytest run;
    # "inconclusive" / "execution_error" mean no clean execution happened and
    # are reported separately instead of diluting the reproduction rate.
    with_tests = [
        f for f in findings
        if f.get("repro_outcome") in ("reproduced", "not_reproduced")
    ]
    reproduced = [f for f in with_tests if f["repro_outcome"] == "reproduced"]
    inconclusive = [
        f for f in findings
        if f.get("repro_outcome") in ("inconclusive", "execution_error")
    ]

    aux = gt.get("expected_aux_finding")
    aux_hit = bool(aux) and any(
        f.get("rule_id") == aux["rule_id"] and aux["function"] in json.dumps(f)
        for f in findings
    )

    return {
        "case": case_dir.name,
        "injected_defect": bool(defect),
        "detected": detected,
        "findings": len(findings),
        "defect_pointing_findings": len(pointing),
        "false_positives": len(pointing) if defect is None else 0,
        "aux_expected_hit": aux_hit if aux else None,
        "reproduced": len(reproduced),
        "findings_with_generated_tests": len(with_tests),
        "reproduction_inconclusive": len(inconclusive),
        "audit_phase_runs": runs_before,
        "pytest_runs": len(runs) - runs_before if runs_before else len(runs),
        "patch_outcome": patch_outcome,
        "duration_seconds": round(duration, 2),
        "engine": detail.get("engine") or detail.get("report", {}).get("audit", {}).get("engine"),
    }


def _run_count(client, audit_id: int) -> int:
    runs = client.get(f"/api/audits/{audit_id}/test-runs").json()
    runs = runs["test_runs"] if isinstance(runs, dict) else runs
    return len(runs)


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="", help="comma-separated corpus case ids")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip approve+verify (faster; verification metrics empty)")
    args = ap.parse_args()

    cases = sorted(p for p in CORPUS.iterdir() if p.is_dir())
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",")}
        cases = [c for c in cases if c.name in wanted]
    if not cases:
        print("no corpus cases selected", file=sys.stderr)
        return 2

    RESULTS.mkdir(parents=True, exist_ok=True)
    workroot = Path(tempfile.mkdtemp(prefix="eca-bench-repos-"))

    rows: list[dict] = []
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        print(f"isolation={health['isolation']['selected_isolation_level']} "
              f"engine=local_deterministic duo_available="
              f"{client.get('/api/duo/availability').json()['available']}")
        for case_dir in cases:
            print(f"--- {case_dir.name}")
            row = run_case(client, case_dir, workroot, verify=not args.no_verify)
            rows.append(row)
            print(f"    detected={row['detected']} findings={row['findings']} "
                  f"reproduced={row['reproduced']} runs={row['pytest_runs']} "
                  f"patch={row['patch_outcome'] and row['patch_outcome']['outcome']} "
                  f"({row['duration_seconds']}s)")

    injected = [r for r in rows if r["injected_defect"]]
    clean = [r for r in rows if not r["injected_defect"]]
    detected = [r for r in injected if r["detected"]]
    pointing_total = sum(r["defect_pointing_findings"] for r in rows)
    pointing_on_defective = sum(r["defect_pointing_findings"] for r in injected)
    with_tests = sum(r["findings_with_generated_tests"] for r in rows)
    reproduced = sum(r["reproduced"] for r in rows)
    attempted = [r for r in rows if r["patch_outcome"]]
    verified = [r for r in attempted
                if r["patch_outcome"]["outcome"] == "verified_for_tested_behavior"]
    reg_runs = [r for r in rows if r["patch_outcome"]]
    reg_fail = sum(r["patch_outcome"]["regression_failures"] for r in reg_runs)
    fp_cases = [r for r in clean if r["false_positives"] > 0]

    metrics = {
        "defect_recall": round(len(detected) / len(injected), 4) if injected else None,
        "finding_precision": round(pointing_on_defective / pointing_total, 4)
        if pointing_total else None,
        "reproduction_rate": round(reproduced / with_tests, 4) if with_tests else None,
        "reproduction_inconclusive_count": sum(r["reproduction_inconclusive"] for r in rows),
        "patch_verification_rate": round(len(verified) / len(attempted), 4)
        if attempted else None,
        "regression_rate": round(reg_fail / len(reg_runs), 4) if reg_runs else None,
        "false_positive_rate": round(len(fp_cases) / len(clean), 4) if clean else None,
        "median_audit_duration_seconds": round(
            statistics.median(r["duration_seconds"] for r in rows), 2),
        "resource_consumption": {
            "pytest_invocations": sum(r["pytest_runs"] + r["audit_phase_runs"] for r in rows),
            "of_which_verification_matrix": sum(r["pytest_runs"] for r in rows),
            "of_which_during_audit": sum(r["audit_phase_runs"] for r in rows),
            "wall_seconds_total": round(sum(r["duration_seconds"] for r in rows), 2),
            "llm_credits": 0,
            "note": "LOCAL_ENGINE performs no model calls; credits are zero by construction.",
        },
    }

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "corpus": [c.name for c in cases],
        "cases": rows,
        "metrics": metrics,
    }
    out = RESULTS / f"benchmark-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\n================ MEASURED BENCHMARK ================")
    for key, value in metrics.items():
        print(f"  {key:34} {value}")
    print(f"  results file: {out.relative_to(REPO)}")
    print("=====================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
