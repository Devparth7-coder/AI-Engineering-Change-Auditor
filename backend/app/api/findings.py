"""Finding endpoints, including on-demand test and patch generation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import (
    AgentRole,
    ApprovalStatus,
    AuditStatus,
    EngineKind,
    EventType,
    Finding,
    IsolationLevel,
    PatchProposal,
    Project,
    ReproOutcome,
    TestRun,
    TestType,
    VerificationOutcome,
    VerificationStatus,
    utcnow,
)
from app.schemas import (
    FindingResponse,
    GeneratePatchRequest,
    GenerateTestRequest,
    PatchResponse,
    TestRunResponse,
)
from app.services import orchestrator, patching, sandbox, store, testgen
from app.services.analysis.engine import ChangeBundle
from app.services.pytest_runner import classify_reproduction, run_pytest, summarise_output
from .deps import require_auth, resolve_finding

router = APIRouter(tags=["findings"])


def finding_to_response(f: Finding) -> FindingResponse:
    return FindingResponse(
        id=f.id,
        public_id=f.public_id,
        audit_id=f.audit_id,
        category=f.category.value,
        severity=f.severity.value,
        title=f.title,
        description=f.description,
        file_path=f.file_path,
        line_start=f.line_start,
        line_end=f.line_end,
        symbol=f.symbol,
        evidence=f.evidence or [],
        expected_behavior=f.expected_behavior,
        suspected_behavior=f.suspected_behavior,
        confidence=f.confidence or {},
        verification_status=f.verification_status.value,
        repro_outcome=f.repro_outcome.value if f.repro_outcome else None,
        recommended_action=f.recommended_action,
        rule_id=f.rule_id,
        engine=f.engine.value,
        facts=f.facts or [],
        hypotheses=f.hypotheses or [],
        missing_information=f.missing_information or [],
        investigation=f.investigation or {},
        repro_test_file=f.repro_test_file,
        repro_expectations=f.repro_expectations or [],
        repro_test_rationale=f.repro_test_rationale,
        created_at=f.created_at,
        test_runs=[run_to_response(r) for r in f.test_runs],
        patches=[patch_to_response(p) for p in f.patches],
    )


def run_to_response(r: TestRun) -> TestRunResponse:
    return TestRunResponse(
        id=r.id, public_id=r.public_id, finding_id=r.finding_id, patch_id=r.patch_id,
        code_revision=r.code_revision, revision_label=r.revision_label,
        test_type=r.test_type.value, execution_status=r.execution_status.value,
        exit_code=r.exit_code, tests_total=r.tests_total, tests_passed=r.tests_passed,
        tests_failed=r.tests_failed, tests_skipped=r.tests_skipped,
        tests_errored=r.tests_errored, intended_tests_executed=r.intended_tests_executed,
        intended_assertion_failed=r.intended_assertion_failed,
        assertion_signature=r.assertion_signature, isolation_level=r.isolation_level.value,
        network_disabled=r.network_disabled, duration_ms=r.duration_ms,
        test_file_name=r.test_file_name, patch_applied=r.patch_applied,
        sanitized_output=r.sanitized_output, failures=r.failures or [],
        started_at=r.started_at, completed_at=r.completed_at,
        # What was actually executed and what it was meant to prove. Without
        # these a reviewer cannot tell a real reproduction from an assertion.
        test_source=r.test_source, expectations=r.expectations or [],
        collected_ids=r.collected_ids or [], executed_ids=r.executed_ids or [],
        intended_ids=r.intended_ids or [], workspace_ref=r.workspace_ref,
    )


def patch_to_response(p: PatchProposal) -> PatchResponse:
    return PatchResponse(
        id=p.id, public_id=p.public_id, audit_id=p.audit_id, finding_id=p.finding_id,
        base_commit_sha=p.base_commit_sha, file_path=p.file_path,
        original_code=p.original_code, proposed_code=p.proposed_code,
        patch_text=p.patch_text, explanation=p.explanation, side_effects=p.side_effects or [],
        related_tests=p.related_tests or [], approval_status=p.approval_status.value,
        decided_by=p.decided_by, decision_note=p.decision_note, decided_at=p.decided_at,
        engine=p.engine.value, verification_outcome=p.verification_outcome.value,
        verification_detail=p.verification_detail or {}, verified_at=p.verified_at,
        created_at=p.created_at,
    )


@router.get("/{finding_id}", response_model=FindingResponse, summary="One finding with its evidence")
def get_finding(
    finding_id: str,
    session: Session = Depends(get_db),
    _: None = Depends(require_auth),
) -> FindingResponse:
    """Accepts the integer id or the readable public id (``F-3-001``)."""
    return finding_to_response(resolve_finding(session, finding_id))


@router.post(
    "/{finding_id}/tests",
    summary="Generate and execute a reproduction test for a finding",
)
def generate_test(
    finding_id: str,
    body: GenerateTestRequest | None = None,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    """Regenerate the reproduction test and (by default) execute it in isolation.

    Refuses to invent an expectation: if the declared contract yields no
    falsifiable assertion, the finding is marked inconclusive and no test is run.
    """
    body = body or GenerateTestRequest()
    finding = resolve_finding(session, finding_id)
    audit = store.get_audit(session, finding.audit_id)
    if audit is None:
        raise HTTPException(status_code=404, detail="Finding's audit no longer exists")
    project = session.get(Project, audit.project_id)
    orch = orchestrator.AuditOrchestrator(settings)
    ctx = orch._stage_fetch_context(session, audit, project, settings)  # noqa: SLF001

    drafts = _rebuild_draft(ctx, finding)
    if drafts is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "The finding could not be re-derived from the current change context. "
                "Re-run the audit to regenerate it."
            ),
        )

    generated = testgen.generate(drafts, finding.public_id, repo_files=list(ctx.bundle.new_sources))
    if not generated.testable:
        finding.verification_status = VerificationStatus.INCONCLUSIVE
        finding.repro_outcome = ReproOutcome.INCONCLUSIVE
        store.record_event(
            session, audit.id, AgentRole.REPAIR_DESIGNER, EventType.TEST_GENERATED,
            f"{finding.public_id}: reproduction test NOT generated. {generated.refusal_reason}",
            {"finding_id": finding.id, "refusal_reason": generated.refusal_reason},
            status="warning",
        )
        session.commit()
        return {
            "ok": False,
            "testable": False,
            "reason": generated.refusal_reason,
            "finding": finding_to_response(finding).model_dump(),
        }

    finding.repro_test_file = generated.file_name
    finding.repro_expectations = [e.to_dict() for e in generated.expectations]
    finding.repro_test_rationale = generated.rationale
    session.commit()

    if not body.execute:
        return {
            "ok": True,
            "testable": True,
            "executed": False,
            "generated_test": generated.to_dict(),
            "test_source": generated.content,
            "finding": finding_to_response(finding).model_dump(),
        }

    run = orch._execute_reproduction(session, audit, ctx, finding, generated, settings)  # noqa: SLF001
    return {
        "ok": True,
        "testable": True,
        "executed": True,
        "generated_test": generated.to_dict(),
        "test_source": generated.content,
        # TestRun has no repro_outcome column; the outcome lives on the Finding
        # (set by _execute_reproduction). Reading it off the run raised
        # AttributeError and turned a legitimate action into a 500.
        "repro_outcome": (
            finding.repro_outcome.value if finding.repro_outcome else None
        ),
        "test_run": run_to_response(run).model_dump(),
        "finding": finding_to_response(finding).model_dump(),
    }


@router.post(
    "/{finding_id}/patches",
    summary="Propose a patch for a finding",
)
def generate_patch(
    finding_id: str,
    body: GeneratePatchRequest | None = None,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    """Propose the smallest contract-satisfying correction.

    A proposal is created in ``pending`` state.  It is never applied, pushed,
    merged or deployed by this endpoint.
    """
    finding = resolve_finding(session, finding_id)
    audit = store.get_audit(session, finding.audit_id)
    if audit is None:
        raise HTTPException(status_code=404, detail="Finding's audit no longer exists")
    project = session.get(Project, audit.project_id)
    orch = orchestrator.AuditOrchestrator(settings)
    ctx = orch._stage_fetch_context(session, audit, project, settings)  # noqa: SLF001
    draft = _rebuild_draft(ctx, finding)
    if draft is None:
        raise HTTPException(
            status_code=409, detail="Finding could not be re-derived from the change context."
        )
    all_sources = orchestrator._all_sources(ctx)  # noqa: SLF001
    source_text = ctx.bundle.new_sources.get(finding.file_path) or all_sources.get(
        finding.file_path, ""
    )
    proposed = patching.propose_from_draft(
        draft, source_text, all_sources,
        [f"audit_tests/{finding.repro_test_file}"] if finding.repro_test_file else [],
    )
    if proposed is None:
        store.record_event(
            session, audit.id, AgentRole.REPAIR_DESIGNER, EventType.FINDING_UPDATED,
            f"{finding.public_id}: no patch proposed. No mechanical correction is "
            "justified by the declared contract.",
            {"finding_id": finding.id},
            status="warning",
        )
        session.commit()
        raise HTTPException(
            status_code=422,
            detail=(
                "No mechanically justified correction exists for this finding. The auditor "
                "will not invent a patch it cannot ground in the declared contract."
            ),
        )

    # Supersede any earlier pending proposal for the same finding.
    for old in session.scalars(
        select(PatchProposal).where(
            PatchProposal.finding_id == finding.id,
            PatchProposal.approval_status == ApprovalStatus.PENDING,
        )
    ).all():
        old.approval_status = ApprovalStatus.SUPERSEDED

    patch = PatchProposal(
        audit_id=audit.id,
        finding_id=finding.id,
        public_id=store.next_public_id(session, PatchProposal, "P", audit.id),
        base_commit_sha=audit.source_commit_sha,
        file_path=proposed.file_path,
        patch_text=proposed.patch_text,
        original_code=proposed.original_code,
        proposed_code=proposed.proposed_code,
        explanation=proposed.explanation,
        side_effects=proposed.side_effects,
        related_tests=proposed.related_tests,
        approval_status=ApprovalStatus.PENDING,
        engine=finding.engine,
        verification_outcome=VerificationOutcome.NOT_ATTEMPTED,
    )
    session.add(patch)
    session.flush()
    orchestrator._PATCH_CONTENT[(audit.id, patch.id)] = proposed.proposed_full_content  # noqa: SLF001
    orchestrator._PATCH_ORIGINAL[(audit.id, patch.id)] = proposed.original_full_content  # noqa: SLF001
    if finding.verification_status == VerificationStatus.REPRODUCED:
        finding.verification_status = VerificationStatus.FIX_PROPOSED
    store.record_event(
        session, audit.id, AgentRole.REPAIR_DESIGNER, EventType.PATCH_PROPOSED,
        f"{patch.public_id} proposed for {finding.public_id} "
        f"(strategy: {proposed.strategy}). Awaiting explicit human approval.",
        {
            "patch_id": patch.id, "finding_id": finding.id,
            "strategy": proposed.strategy, "file_path": proposed.file_path,
            "call_sites": proposed.call_sites, "side_effects": proposed.side_effects,
            "patch_text": proposed.patch_text,
            "approval_status": ApprovalStatus.PENDING.value,
        },
    )
    session.commit()
    return {
        "ok": True,
        "patch": patch_to_response(patch).model_dump(),
        "call_sites": proposed.call_sites,
        "requires_approval": True,
        "note": (
            "The patch is stored as a proposal only. Nothing has been applied, pushed, "
            "merged or deployed. Use POST /api/patches/{patch_id}/approve, then "
            "/verify to execute it inside an isolated workspace."
        ),
    }


def _rebuild_draft(ctx: Any, finding: Finding) -> Any:  # noqa: ANN401
    """Re-derive the analysis draft for one persisted finding."""
    from app.services.analysis.engine import analyze

    cached = orchestrator._DRAFTS.get((finding.audit_id, finding.id))  # noqa: SLF001
    if cached is not None:
        return cached
    result = analyze(ctx.bundle)
    for draft in result.findings:
        if draft.rule_id == finding.rule_id and draft.symbol == finding.symbol \
                and draft.file_path == finding.file_path and draft.line_start == finding.line_start:
            orchestrator._DRAFTS[(finding.audit_id, finding.id)] = draft  # noqa: SLF001
            return draft
    return None


__all__ = ["finding_to_response", "patch_to_response", "router", "run_to_response"]
