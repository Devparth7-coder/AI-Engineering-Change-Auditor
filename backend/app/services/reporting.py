"""Reporting service.

Builds the final audit report **exclusively from persisted records**.  Nothing
here computes a new verdict: it reads findings, test runs, patch proposals,
approval decisions and agent events, then labels the outcome.

There is deliberately no "AI quality score" in this report.  Every statement is
either a repository fact, an execution result, or an explicit statement of what
remains unverified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import store
from .analysis.engine import ChangeBundle
from app.models import (
    AgentEvent,
    ApprovalStatus,
    AuditReport,
    AuditSession,
    Finding,
    PatchProposal,
    Project,
    ReproOutcome,
    TestRun,
    TestRunStatus,
    TestType,
    VerificationOutcome,
    VerificationStatus,
    utcnow,
)

# Explicit, explainable outcome labels. Each is defined in docs/evaluation.md.
OUTCOME_FIX_VERIFIED = "defect_reproduced_and_fix_verified"
OUTCOME_FIX_FAILED = "defect_reproduced_fix_not_verified"
OUTCOME_AWAITING_APPROVAL = "defect_reproduced_awaiting_human_approval"
OUTCOME_REGRESSION = "regression_detected_after_patch"
OUTCOME_NO_DEFECT = "no_defect_reproduced"
OUTCOME_INCONCLUSIVE = "inconclusive_insufficient_evidence"
OUTCOME_EXECUTION_ERRORS = "execution_errors_prevented_conclusion"
OUTCOME_HYPOTHESES_ONLY = "hypotheses_only_not_reproduced"

OUTCOME_EXPLANATIONS = {
    OUTCOME_FIX_VERIFIED: (
        "At least one defect was reproduced by executing a test against the original "
        "revision, a patch was approved by a human, and after applying it in an "
        "isolated workspace the intended assertions passed with no previously-passing "
        "regression test broken. This is scoped to the tested behaviour only."
    ),
    OUTCOME_FIX_FAILED: (
        "A defect was reproduced, a patch was approved, but the intended assertions "
        "still fail (or the run was inconclusive) after the patch was applied."
    ),
    OUTCOME_AWAITING_APPROVAL: (
        "A defect was reproduced and a patch was proposed, but no human has approved "
        "it yet, so nothing was applied or verified."
    ),
    OUTCOME_REGRESSION: (
        "A patch made the targeted assertions pass but broke at least one test that "
        "passed at the original revision."
    ),
    OUTCOME_NO_DEFECT: (
        "Findings were investigated and reproduction tests executed, but every "
        "intended assertion passed against the original revision. No defect was "
        "confirmed; the findings remain hypotheses."
    ),
    OUTCOME_INCONCLUSIVE: (
        "The available repository evidence was insufficient to support a testable "
        "claim, so no conclusion about a defect can be drawn."
    ),
    OUTCOME_EXECUTION_ERRORS: (
        "One or more test executions failed to run (collection error, timeout or "
        "environment fault). Those runs prove nothing and are reported as errors, "
        "not as passes."
    ),
    OUTCOME_HYPOTHESES_ONLY: (
        "Findings were produced but none could be converted into an executable, "
        "falsifiable test, so no execution evidence exists either way."
    ),
}


@dataclass
class ReportCounts:
    findings: int = 0
    by_severity: dict[str, int] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)
    by_verification_status: dict[str, int] = field(default_factory=dict)
    reproduced: int = 0
    not_reproduced: int = 0
    inconclusive: int = 0
    execution_error: int = 0
    patches_proposed: int = 0
    patches_approved: int = 0
    patches_rejected: int = 0
    fixes_verified: int = 0
    verification_failed: int = 0
    regressions: int = 0
    test_runs: int = 0
    tests_executed: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    tests_skipped: int = 0
    tests_errored: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": self.findings,
            "by_severity": self.by_severity,
            "by_category": self.by_category,
            "by_verification_status": self.by_verification_status,
            "reproduced": self.reproduced,
            "not_reproduced": self.not_reproduced,
            "inconclusive": self.inconclusive,
            "execution_error": self.execution_error,
            "patches_proposed": self.patches_proposed,
            "patches_approved": self.patches_approved,
            "patches_rejected": self.patches_rejected,
            "fixes_verified": self.fixes_verified,
            "verification_failed": self.verification_failed,
            "regressions": self.regressions,
            "test_runs": self.test_runs,
            "tests_executed": self.tests_executed,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "tests_skipped": self.tests_skipped,
            "tests_errored": self.tests_errored,
        }


def _finding_payload(f: Finding, runs: list[TestRun], patches: list[PatchProposal]) -> dict[str, Any]:
    return {
        "finding_id": f.public_id,
        "id": f.id,
        "title": f.title,
        "category": f.category.value,
        "severity": f.severity.value,
        "description": f.description,
        "location": {
            "file_path": f.file_path,
            "line_start": f.line_start,
            "line_end": f.line_end,
            "symbol": f.symbol,
        },
        "evidence": f.evidence,
        "expected_behavior": f.expected_behavior,
        "suspected_behavior": f.suspected_behavior,
        "confidence": f.confidence,
        "verification_status": f.verification_status.value,
        "repro_outcome": f.repro_outcome.value if f.repro_outcome else None,
        "recommended_action": f.recommended_action,
        "rule_id": f.rule_id,
        "engine": f.engine.value,
        "facts": f.facts,
        "hypotheses": f.hypotheses,
        "missing_information": f.missing_information,
        "investigation": f.investigation,
        "reproduction_test": {
            "file_name": f.repro_test_file,
            "rationale": f.repro_test_rationale,
            "expectations": f.repro_expectations,
        },
        "test_runs": [_run_payload(r) for r in runs],
        "patches": [_patch_payload(p) for p in patches],
    }


def _run_payload(r: TestRun) -> dict[str, Any]:
    return {
        "test_run_id": r.public_id,
        "id": r.id,
        "test_type": r.test_type.value,
        "code_revision": r.code_revision,
        "revision_label": r.revision_label,
        "patch_applied": r.patch_applied,
        "execution_status": r.execution_status.value,
        "exit_code": r.exit_code,
        "counts": {
            "total": r.tests_total,
            "passed": r.tests_passed,
            "failed": r.tests_failed,
            "skipped": r.tests_skipped,
            "errored": r.tests_errored,
        },
        "intended_tests_executed": r.intended_tests_executed,
        "intended_assertion_failed": r.intended_assertion_failed,
        "assertion_signature": r.assertion_signature,
        "failures": r.failures,
        "isolation": {
            "level": r.isolation_level.value,
            "network_disabled": r.network_disabled,
        },
        "duration_ms": r.duration_ms,
        "test_file_name": r.test_file_name,
        "sanitized_output": r.sanitized_output,
        "started_at": store.iso(r.started_at),
        "completed_at": store.iso(r.completed_at),
    }


def _patch_payload(p: PatchProposal) -> dict[str, Any]:
    return {
        "patch_id": p.public_id,
        "id": p.id,
        "finding_id": p.finding_id,
        "base_commit_sha": p.base_commit_sha,
        "file_path": p.file_path,
        "original_code": p.original_code,
        "proposed_code": p.proposed_code,
        "patch_text": p.patch_text,
        "explanation": p.explanation,
        "side_effects": p.side_effects,
        "related_tests": p.related_tests,
        "approval_status": p.approval_status.value,
        "decided_by": p.decided_by,
        "decision_note": p.decision_note,
        "decided_at": store.iso(p.decided_at),
        "engine": p.engine.value,
        "verification_outcome": p.verification_outcome.value,
        "verification_detail": p.verification_detail,
        "verified_at": store.iso(p.verified_at),
        "created_at": store.iso(p.created_at),
    }


def determine_outcome(
    findings: list[Finding], patches: list[PatchProposal], runs: list[TestRun]
) -> tuple[str, list[str]]:
    """Pick an explicit outcome label and the reasons behind it."""
    reasons: list[str] = []
    statuses = {f.verification_status for f in findings}
    repro = [f for f in findings if f.repro_outcome == ReproOutcome.REPRODUCED]
    not_repro = [f for f in findings if f.repro_outcome == ReproOutcome.NOT_REPRODUCED]
    exec_err = [f for f in findings if f.repro_outcome == ReproOutcome.EXECUTION_ERROR]
    verified = [p for p in patches if p.verification_outcome == VerificationOutcome.VERIFIED_FOR_TESTED_BEHAVIOR]
    regressed = [p for p in patches if p.verification_outcome == VerificationOutcome.REGRESSION_DETECTED]
    failed_verify = [
        p
        for p in patches
        if p.verification_outcome
        in (VerificationOutcome.VERIFICATION_FAILED, VerificationOutcome.INCONCLUSIVE, VerificationOutcome.EXECUTION_ERROR)
    ]
    pending = [p for p in patches if p.approval_status == ApprovalStatus.PENDING]
    approved = [p for p in patches if p.approval_status == ApprovalStatus.APPROVED]
    not_attempted = [
        p for p in approved if p.verification_outcome == VerificationOutcome.NOT_ATTEMPTED
    ]

    if regressed:
        reasons.append(
            f"{len(regressed)} patch(es) broke previously passing tests "
            f"({', '.join(p.public_id for p in regressed)})."
        )
        return OUTCOME_REGRESSION, reasons
    if exec_err:
        reasons.append(
            f"{len(exec_err)} finding(s) could not be executed "
            f"({', '.join(f.public_id for f in exec_err)}); an execution error is not a pass."
        )
        if not verified and not repro:
            return OUTCOME_EXECUTION_ERRORS, reasons
    if verified:
        reasons.append(
            f"{len(verified)} patch(es) verified by execution "
            f"({', '.join(p.public_id for p in verified)})."
        )
        if exec_err:
            reasons.append("Other findings still have unresolved execution errors.")
        return OUTCOME_FIX_VERIFIED, reasons
    if failed_verify:
        reasons.append(
            f"{len(failed_verify)} approved patch(es) did not verify "
            f"({', '.join(p.public_id for p in failed_verify)})."
        )
        return OUTCOME_FIX_FAILED, reasons
    if not_attempted:
        reasons.append(
            f"{len(not_attempted)} approved patch(es) have not been verified yet "
            f"({', '.join(p.public_id for p in not_attempted)})."
        )
        return OUTCOME_AWAITING_APPROVAL, reasons
    if repro and pending:
        reasons.append(
            f"{len(repro)} defect(s) reproduced and {len(pending)} patch(es) await human "
            f"approval ({', '.join(p.public_id for p in pending)})."
        )
        return OUTCOME_AWAITING_APPROVAL, reasons
    if repro:
        reasons.append(
            f"{len(repro)} defect(s) reproduced by test execution "
            f"({', '.join(f.public_id for f in repro)})."
        )
        return OUTCOME_AWAITING_APPROVAL, reasons
    if not findings:
        reasons.append("No findings were produced for this change.")
        return OUTCOME_NO_DEFECT, reasons
    if not_repro and not_repro and len(not_repro) == len(
        [f for f in findings if f.repro_outcome is not None]
    ):
        reasons.append(
            f"{len(not_repro)} reproduction test(s) executed and all intended assertions "
            "passed against the original revision; no defect was confirmed."
        )
        return OUTCOME_NO_DEFECT, reasons
    if statuses and statuses <= {VerificationStatus.INCONCLUSIVE, VerificationStatus.HYPOTHESIS}:
        reasons.append(
            "Every finding remained a hypothesis or inconclusive; no execution evidence "
            "supports or refutes a defect."
        )
        if any(s == VerificationStatus.INCONCLUSIVE for s in statuses):
            return OUTCOME_INCONCLUSIVE, reasons
        return OUTCOME_HYPOTHESES_ONLY, reasons
    reasons.append("Mixed evidence; see per-finding verification status for details.")
    return OUTCOME_INCONCLUSIVE, reasons


def _remaining_risks(
    findings: list[Finding], patches: list[PatchProposal], runs: list[TestRun]
) -> list[str]:
    risks: list[str] = []
    for f in findings:
        if f.verification_status in (
            VerificationStatus.HYPOTHESIS,
            VerificationStatus.INCONCLUSIVE,
            VerificationStatus.EXECUTION_ERROR,
        ):
            risks.append(
                f"{f.public_id} ({f.severity.value}, {f.category.value}) is "
                f"{f.verification_status.value}: unresolved. {f.recommended_action}"
            )
        if f.missing_information:
            risks.append(
                f"{f.public_id}: missing information -- " + "; ".join(f.missing_information[:3])
            )
    for p in patches:
        if p.approval_status == ApprovalStatus.PENDING:
            risks.append(
                f"{p.public_id} is proposed but not approved; the defect it addresses is unremediated."
            )
        if p.approval_status == ApprovalStatus.REJECTED:
            risks.append(
                f"{p.public_id} was rejected; the underlying defect remains in the change."
            )
        for se in p.side_effects or []:
            risks.append(f"{p.public_id} side effect: {se}")
    skipped = [r for r in runs if r.tests_skipped]
    if skipped:
        risks.append(
            f"{len(skipped)} run(s) contained skipped tests "
            f"({sum(r.tests_skipped for r in skipped)} skipped in total); skipped tests "
            "provide no verification evidence."
        )
    errored = [r for r in runs if r.execution_status in (TestRunStatus.ERROR, TestRunStatus.EXECUTION_ERROR, TestRunStatus.NOT_COLLECTED, TestRunStatus.TIMEOUT)]
    if errored:
        risks.append(
            f"{len(errored)} test run(s) ended in an execution error, timeout or "
            "collection failure; their subject matter is unverified."
        )
    non_container = [r for r in runs if r.isolation_level.value != "container"]
    if non_container:
        levels = sorted({r.isolation_level.value for r in non_container})
        risks.append(
            f"{len(non_container)} run(s) executed under non-container isolation "
            f"({', '.join(levels)}). Container isolation is recommended for production."
        )
    if not any(r.test_type == TestType.REGRESSION for r in runs) and not any(
        r.test_type == TestType.POST_FIX_REGRESSION for r in runs
    ):
        risks.append("No repository regression suite was executed during this audit.")
    return risks


def _next_steps(outcome: str, findings: list[Finding], patches: list[PatchProposal]) -> list[str]:
    steps: list[str] = []
    pending = [p for p in patches if p.approval_status == ApprovalStatus.PENDING]
    if pending:
        steps.append(
            f"Review and decide on {len(pending)} pending patch proposal(s): "
            + ", ".join(p.public_id for p in pending)
            + ". Approval only authorises execution in an isolated workspace."
        )
    approved_unverified = [
        p for p in patches
        if p.approval_status == ApprovalStatus.APPROVED
        and p.verification_outcome == VerificationOutcome.NOT_ATTEMPTED
    ]
    if approved_unverified:
        steps.append(
            "Run verification for approved patch(es) "
            + ", ".join(p.public_id for p in approved_unverified)
            + " (POST /api/patches/{patch_id}/verify)."
        )
    verified = [
        p for p in patches if p.verification_outcome == VerificationOutcome.VERIFIED_FOR_TESTED_BEHAVIOR
    ]
    if verified:
        steps.append(
            "Carry the verified patch into the merge request as a normal, reviewed "
            "commit. The auditor never pushes, merges or deploys on your behalf."
        )
        steps.append(
            "Add the generated reproduction test to the repository suite so the "
            "behaviour is pinned going forward."
        )
    for f in findings:
        if f.category.value == "missing_tests":
            steps.append(
                f"Close the coverage gap behind {f.public_id} by committing the "
                "generated contract test."
            )
            break
    unresolved = [
        f for f in findings
        if f.verification_status in (VerificationStatus.INCONCLUSIVE, VerificationStatus.EXECUTION_ERROR)
    ]
    if unresolved:
        steps.append(
            f"{len(unresolved)} finding(s) need more information or a working test "
            "environment before any conclusion can be drawn: "
            + ", ".join(f.public_id for f in unresolved)
        )
    if not steps:
        steps.append("No action required by the auditor; review the findings for awareness.")
    return steps


def build_report(
    session: Session,
    audit: AuditSession,
    bundle: ChangeBundle | None = None,
    context_notes: list[str] | None = None,
    duo_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble and persist the audit report from stored records only."""
    project = store.get_project(session, audit.project_id)
    findings = list(
        session.scalars(select(Finding).where(Finding.audit_id == audit.id).order_by(Finding.id)).all()
    )
    patches = list(
        session.scalars(
            select(PatchProposal).where(PatchProposal.audit_id == audit.id).order_by(PatchProposal.id)
        ).all()
    )
    runs = list(
        session.scalars(select(TestRun).where(TestRun.audit_id == audit.id).order_by(TestRun.id)).all()
    )
    events = list(
        session.scalars(
            select(AgentEvent).where(AgentEvent.audit_id == audit.id).order_by(AgentEvent.seq)
        ).all()
    )

    counts = ReportCounts()
    counts.findings = len(findings)
    for f in findings:
        counts.by_severity[f.severity.value] = counts.by_severity.get(f.severity.value, 0) + 1
        counts.by_category[f.category.value] = counts.by_category.get(f.category.value, 0) + 1
        key = f.verification_status.value
        counts.by_verification_status[key] = counts.by_verification_status.get(key, 0) + 1
        if f.repro_outcome == ReproOutcome.REPRODUCED:
            counts.reproduced += 1
        elif f.repro_outcome == ReproOutcome.NOT_REPRODUCED:
            counts.not_reproduced += 1
        elif f.repro_outcome == ReproOutcome.INCONCLUSIVE:
            counts.inconclusive += 1
        elif f.repro_outcome == ReproOutcome.EXECUTION_ERROR:
            counts.execution_error += 1
    counts.patches_proposed = len(patches)
    counts.patches_approved = sum(1 for p in patches if p.approval_status == ApprovalStatus.APPROVED)
    counts.patches_rejected = sum(1 for p in patches if p.approval_status == ApprovalStatus.REJECTED)
    counts.fixes_verified = sum(
        1 for p in patches
        if p.verification_outcome == VerificationOutcome.VERIFIED_FOR_TESTED_BEHAVIOR
    )
    counts.verification_failed = sum(
        1 for p in patches if p.verification_outcome == VerificationOutcome.VERIFICATION_FAILED
    )
    counts.regressions = sum(
        1 for p in patches if p.verification_outcome == VerificationOutcome.REGRESSION_DETECTED
    )
    counts.test_runs = len(runs)
    counts.tests_executed = sum(r.tests_total for r in runs)
    counts.tests_passed = sum(r.tests_passed for r in runs)
    counts.tests_failed = sum(r.tests_failed for r in runs)
    counts.tests_skipped = sum(r.tests_skipped for r in runs)
    counts.tests_errored = sum(r.tests_errored for r in runs)

    outcome, outcome_reasons = determine_outcome(findings, patches, runs)
    risks = _remaining_risks(findings, patches, runs)
    steps = _next_steps(outcome, findings, patches)

    runs_by_finding: dict[int, list[TestRun]] = {}
    patches_by_finding: dict[int, list[PatchProposal]] = {}
    for r in runs:
        if r.finding_id:
            runs_by_finding.setdefault(r.finding_id, []).append(r)
    for p in patches:
        if p.finding_id:
            patches_by_finding.setdefault(p.finding_id, []).append(p)

    approval_history = [
        {
            "patch_id": p.public_id,
            "status": p.approval_status.value,
            "decided_by": p.decided_by,
            "decided_at": store.iso(p.decided_at),
            "note": p.decision_note,
            "verification_outcome": p.verification_outcome.value,
        }
        for p in patches
        if p.approval_status != ApprovalStatus.PENDING or p.decided_at
    ]

    duration_seconds = None
    if audit.started_at and audit.completed_at:
        duration_seconds = round((audit.completed_at - audit.started_at).total_seconds(), 3)

    duo_info = audit.duo_availability or {}
    summary = _write_summary(audit, project, outcome, counts, findings, patches, duo_info)

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "audit": {
            "audit_id": audit.id,
            "status": audit.status.value,
            "stage": audit.stage,
            "progress_pct": audit.progress_pct,
            "mode": audit.mode,
            "engine": audit.engine.value,
            "started_at": store.iso(audit.started_at),
            "completed_at": store.iso(audit.completed_at),
            "duration_seconds": duration_seconds,
            "error_detail": audit.error_detail,
        },
        "project": {
            "name": project.name if project else "",
            "namespace": project.namespace if project else "",
            "path_with_namespace": project.path_with_namespace if project else "",
            "default_branch": project.default_branch if project else "",
            "gitlab_project_id": project.gitlab_project_id if project else None,
            "web_url": project.web_url if project else "",
            "origin": project.origin if project else "",
        },
        "change": {
            "merge_request_iid": audit.merge_request_iid,
            "merge_request_title": audit.merge_request_title,
            "source_branch": audit.source_branch,
            "target_branch": audit.target_branch,
            "source_commit_sha": audit.source_commit_sha,
            "target_commit_sha": audit.target_commit_sha,
            "changed_files": (bundle.changed_paths if bundle else []),
        },
        "outcome": {
            "label": outcome,
            "explanation": OUTCOME_EXPLANATIONS.get(outcome, ""),
            "reasons": outcome_reasons,
        },
        "counts": counts.to_dict(),
        "gitlab_duo_agent_platform": {
            "used_for_this_audit": audit.engine.value == "gitlab_duo_flow",
            "availability": duo_info,
            "session_id": audit.duo_session_id,
            "web_url": audit.duo_web_url,
            "trigger_and_poll_result": duo_result or {},
            "statement": (
                "The GitLab Duo Agent Platform flow was triggered and its session events "
                "were recorded for this audit."
                if audit.engine.value == "gitlab_duo_flow"
                else "The GitLab Duo Agent Platform was NOT used for this audit. "
                + str(duo_info.get("reason", ""))
                + " Missing prerequisite: "
                + str(duo_info.get("missing_prerequisite", ""))
            ),
        },
        "context_notes": context_notes or [],
        "findings": [
            _finding_payload(f, runs_by_finding.get(f.id, []), patches_by_finding.get(f.id, []))
            for f in findings
        ],
        "patches": [_patch_payload(p) for p in patches],
        "approval_history": approval_history,
        "test_runs": [_run_payload(r) for r in runs],
        "regression_results": {
            "baseline": [
                _run_payload(r) for r in runs if r.test_type == TestType.REGRESSION
            ],
            "post_fix": [
                _run_payload(r) for r in runs if r.test_type == TestType.POST_FIX_REGRESSION
            ],
            "regressions_detected": [
                reg
                for p in patches
                for reg in (p.verification_detail or {}).get("regressions", [])
            ],
        },
        "failed_skipped_or_inconclusive": {
            "runs_with_failures": [
                _run_payload(r) for r in runs if r.tests_failed or r.tests_errored
            ],
            "runs_with_skips": [_run_payload(r) for r in runs if r.tests_skipped],
            "inconclusive_findings": [
                f.public_id for f in findings
                if f.verification_status == VerificationStatus.INCONCLUSIVE
            ],
            "execution_error_findings": [
                f.public_id for f in findings
                if f.verification_status == VerificationStatus.EXECUTION_ERROR
            ],
        },
        "remaining_risks": risks,
        "recommended_next_steps": steps,
        "limitations": _limitations(audit, runs, duo_info),
        "provenance": {
            "events_recorded": len(events),
            "isolation_levels_used": sorted({r.isolation_level.value for r in runs}),
            "network_disabled": all(r.network_disabled for r in runs) if runs else None,
            "revisions_tested": sorted({r.code_revision for r in runs if r.code_revision}),
        },
    }

    report = AuditReport(
        audit_id=audit.id,
        public_id=store.next_public_id(session, AuditReport, "R", audit.id),
        summary=summary,
        outcome=outcome,
        report=payload,
        created_at=utcnow(),
    )
    session.add(report)
    session.commit()
    return {"report_id": report.id, "public_id": report.public_id, "outcome": outcome, "payload": payload, "summary": summary}


def _write_summary(
    audit: AuditSession,
    project: Project | None,
    outcome: str,
    counts: ReportCounts,
    findings: list[Finding],
    patches: list[PatchProposal],
    duo_info: dict[str, Any],
) -> str:
    mode = (
        "local demonstration mode (bundled example repository)"
        if audit.mode == "local_demo"
        else "live GitLab integration"
    )
    engine = (
        "GitLab Duo Agent Platform flow"
        if audit.engine.value == "gitlab_duo_flow"
        else "the local deterministic analysis engine"
    )
    critical = [f for f in findings if f.severity.value in ("critical", "high")]
    parts = [
        f"Audit of {project.path_with_namespace if project else 'project'} "
        f"merge request {('!' + str(audit.merge_request_iid)) if audit.merge_request_iid else audit.source_branch + ' -> ' + audit.target_branch} "
        f"ran in {mode} using {engine}.",
        f"Source revision {audit.source_commit_sha[:12]}, target revision "
        f"{audit.target_commit_sha[:12]}.",
        f"{counts.findings} finding(s): {counts.reproduced} reproduced by test execution, "
        f"{counts.not_reproduced} not reproduced, {counts.inconclusive} inconclusive, "
        f"{counts.execution_error} blocked by execution errors.",
        f"{counts.patches_proposed} patch proposal(s): {counts.patches_approved} approved, "
        f"{counts.patches_rejected} rejected, {counts.fixes_verified} verified by execution, "
        f"{counts.regressions} caused a regression.",
        f"Outcome: {outcome} -- {OUTCOME_EXPLANATIONS.get(outcome, '')}",
    ]
    if critical:
        parts.append(
            "Highest-severity findings: "
            + "; ".join(f"{f.public_id} [{f.severity.value}] {f.title}" for f in critical[:3])
        )
    if audit.engine.value != "gitlab_duo_flow":
        parts.append(
            "The mandatory GitLab Duo Agent Platform integration was NOT exercised for "
            "this audit: " + str(duo_info.get("reason", "unavailable"))
        )
    return " ".join(parts)


def _limitations(audit: AuditSession, runs: list[TestRun], duo_info: dict[str, Any]) -> list[str]:
    out = [
        "Verification establishes that specific contract assertions hold for the tested "
        "symbol under the tested inputs. It does not establish that the application as a "
        "whole is correct.",
        "Findings are produced by deterministic static rules plus execution evidence. "
        "Absence of findings is not proof of absence of defects.",
        "Confidence values are signal-agreement measures, not calibrated probabilities of "
        "defect. Only verification_status records whether a defect was demonstrated.",
    ]
    if audit.mode == "local_demo":
        out.append(
            "This audit ran in local demonstration mode against a bundled example "
            "repository. It did not use the GitLab REST API or the Duo Agent Platform, and "
            "must not be presented as a live-platform result."
        )
    if audit.engine.value != "gitlab_duo_flow":
        out.append(
            "The GitLab Duo Agent Platform was not used: "
            + str(duo_info.get("reason", "unavailable"))
            + " Prerequisite: "
            + str(duo_info.get("missing_prerequisite", ""))
        )
    levels = {r.isolation_level.value for r in runs}
    if levels and levels != {"container"}:
        out.append(
            f"Tests executed under isolation tier(s): {', '.join(sorted(levels))}. "
            "Only the container tier provides a hard security boundary for untrusted code."
        )
    if not runs:
        out.append("No tests were executed during this audit.")
    return out


__all__ = [
    "OUTCOME_EXPLANATIONS",
    "ReportCounts",
    "build_report",
    "determine_outcome",
]
