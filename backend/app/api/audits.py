"""Audit session endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import (
    AgentEvent,
    AgentRole,
    EventType,
    AuditReport,
    AuditSession,
    AuditStatus,
    EngineKind,
    Finding,
    PatchProposal,
    Project,
    TestRun,
    TERMINAL_AUDIT_STATUSES,
    utcnow,
)
from app.schemas import (
    AckResponse,
    AgentEventResponse,
    AuditCreateRequest,
    AuditDetailResponse,
    AuditResponse,
    FindingSummary,
    PatchResponse,
    ReportResponse,
    TestRunResponse,
)
from app.services import gitrepo, orchestrator, store
from .findings import run_to_response
from app.services.reporting import OUTCOME_EXPLANATIONS
from .deps import (
    assert_audit_access,
    get_audit_or_404,
    get_project_or_404,
    require_auth,
    resolve_finding,
    resolve_patch,
)

router = APIRouter(tags=["audits"])


def _brief(a: AuditSession, session: Session) -> AuditResponse:
    report = store.latest_report(session, a.id)
    return AuditResponse(
        id=a.id,
        project_id=a.project_id,
        merge_request_iid=a.merge_request_iid,
        merge_request_title=a.merge_request_title,
        source_branch=a.source_branch,
        target_branch=a.target_branch,
        source_commit_sha=a.source_commit_sha,
        target_commit_sha=a.target_commit_sha,
        status=a.status.value,
        stage=a.stage,
        progress_pct=a.progress_pct,
        mode=a.mode,
        engine=a.engine.value,
        duo_session_id=a.duo_session_id,
        duo_web_url=a.duo_web_url,
        duo_availability=a.duo_availability or {},
        started_at=a.started_at,
        completed_at=a.completed_at,
        error_detail=a.error_detail,
        created_at=a.created_at,
        findings_count=len(a.findings),
        patches_count=len(a.patches),
        test_runs_count=len(a.test_runs),
        outcome=report.outcome if report else "",
    )


@router.post("", response_model=AuditResponse, status_code=201, summary="Create an audit session")
def create_audit(
    body: AuditCreateRequest,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> AuditResponse:
    """Create (and by default queue) an audit.

    Repeated identical requests are idempotent while an audit for the same
    target is still in flight: the existing session is returned instead of a
    duplicate being created.
    """
    project: Project | None = None
    if body.project_id:
        project = get_project_or_404(session, body.project_id)
    elif body.gitlab_project_id:
        project = session.scalar(
            select(Project).where(Project.gitlab_project_id == body.gitlab_project_id)
        )
        if project is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"GitLab project {body.gitlab_project_id} is not connected yet. "
                    "Call GET /api/projects?include_live=true first."
                ),
            )
    elif body.mode == "local_demo":
        from pathlib import Path

        project = store.find_or_create_project(
            session,
            name=Path(settings.demo_repo_path).name,
            namespace="local-demo",
            path_with_namespace=f"local-demo/{Path(settings.demo_repo_path).name}",
            default_branch=gitrepo.default_branch(Path(settings.demo_repo_path)),
            local_path=str(settings.demo_repo_path),
            origin="local_demo",
        )
    if project is None:
        raise HTTPException(status_code=400, detail="A project selector is required")

    if body.mode == "live_gitlab":
        if not settings.is_live_gitlab:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Live GitLab mode was requested but GITLAB_URL/GITLAB_TOKEN are not "
                    "configured. Use mode='local_demo' for the bundled demonstration, or "
                    "configure live credentials."
                ),
            )
        assert_audit_access_for_project(project, settings)
        if body.merge_request_iid is None:
            raise HTTPException(
                status_code=400, detail="merge_request_iid is required for live audits"
            )

    source_branch = body.source_branch or settings.demo_source_branch
    target_branch = body.target_branch or settings.demo_target_branch

    # Idempotency: an in-flight audit for the same target is reused.
    existing = session.scalar(
        select(AuditSession)
        .where(
            AuditSession.project_id == project.id,
            AuditSession.merge_request_iid == body.merge_request_iid,
            AuditSession.source_branch == source_branch,
            AuditSession.target_branch == target_branch,
            AuditSession.status.not_in([s.value for s in TERMINAL_AUDIT_STATUSES]),
        )
        .order_by(AuditSession.id.desc())
        .limit(1)
    )
    if existing is not None:
        return _brief(existing, session)

    audit = AuditSession(
        project_id=project.id,
        merge_request_iid=body.merge_request_iid,
        source_branch=source_branch,
        target_branch=target_branch,
        source_commit_sha=body.source_commit_sha,
        target_commit_sha=body.target_commit_sha,
        status=AuditStatus.CREATED,
        mode=body.mode,
        engine=EngineKind.LOCAL_DETERMINISTIC,
        stage="created",
    )
    session.add(audit)
    session.flush()
    store.record_event(
        session,
        audit.id,
        AgentRole.ORCHESTRATOR,
        EventType.AUDIT_CREATED,
        f"Audit session created in mode '{body.mode}' for "
        f"{project.path_with_namespace} ({source_branch} -> {target_branch}).",
        {
            "project_id": project.id,
            "mode": body.mode,
            "merge_request_iid": body.merge_request_iid,
            "source_branch": source_branch,
            "target_branch": target_branch,
        },
        commit=False,
    )
    session.commit()

    if body.auto_start:
        queued = orchestrator.submit_audit(audit.id, settings)
        if queued:
            store.set_stage(session, audit, AuditStatus.QUEUED)
            store.record_event(
                session, audit.id, AgentRole.ORCHESTRATOR, EventType.STAGE_STARTED,
                "Audit queued for asynchronous execution. Poll "
                "GET /api/audits/{id}/events for progress.",
                {},
            )
    return _brief(audit, session)


def assert_audit_access_for_project(project: Project, settings: Settings) -> None:
    allowed = settings.allowed_project_ids
    if allowed and project.gitlab_project_id not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Project {project.gitlab_project_id} is not in GITLAB_ALLOWED_PROJECT_IDS",
        )


@router.get("", response_model=list[AuditResponse], summary="List audits (history)")
def list_audits(
    status_filter: str | None = Query(default=None, alias="status", max_length=40),
    project_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
    _: None = Depends(require_auth),
) -> list[AuditResponse]:
    stmt = select(AuditSession).order_by(AuditSession.id.desc())
    if status_filter:
        try:
            stmt = stmt.where(AuditSession.status == AuditStatus(status_filter))
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"unknown status '{status_filter}'; valid: {[s.value for s in AuditStatus]}",
            ) from exc
    if project_id:
        stmt = stmt.where(AuditSession.project_id == project_id)
    audits = session.scalars(stmt.offset(offset).limit(limit)).all()
    return [_brief(a, session) for a in audits]


@router.get("/{audit_id}", response_model=AuditDetailResponse, summary="Full audit detail")
def get_audit(
    audit_id: int,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> AuditDetailResponse:
    audit = get_audit_or_404(session, audit_id)
    assert_audit_access(session, audit, settings)
    project = session.get(Project, audit.project_id)
    report = store.latest_report(session, audit_id)
    events = session.scalars(
        select(AgentEvent)
        .where(AgentEvent.audit_id == audit_id)
        .order_by(AgentEvent.seq.desc())
        .limit(25)
    ).all()
    base = _brief(audit, session)
    return AuditDetailResponse(
        **base.model_dump(),
        project=(
            {
                "id": project.id,
                "gitlab_project_id": project.gitlab_project_id,
                "name": project.name,
                "namespace": project.namespace,
                "path_with_namespace": project.path_with_namespace,
                "default_branch": project.default_branch,
                "web_url": project.web_url,
                "local_path": project.local_path,
                "origin": project.origin,
                "created_at": project.created_at,
            }
            if project
            else None
        ),
        findings=[
            FindingSummary(
                id=f.id, public_id=f.public_id, category=f.category.value,
                severity=f.severity.value, title=f.title, file_path=f.file_path,
                line_start=f.line_start, line_end=f.line_end, symbol=f.symbol,
                verification_status=f.verification_status.value,
                repro_outcome=f.repro_outcome.value if f.repro_outcome else None,
                rule_id=f.rule_id, engine=f.engine.value,
            )
            for f in audit.findings
        ],
        patches=[
            PatchResponse(
                id=p.id, public_id=p.public_id, audit_id=p.audit_id, finding_id=p.finding_id,
                base_commit_sha=p.base_commit_sha, file_path=p.file_path,
                original_code=p.original_code, proposed_code=p.proposed_code,
                patch_text=p.patch_text, explanation=p.explanation,
                side_effects=p.side_effects or [], related_tests=p.related_tests or [],
                approval_status=p.approval_status.value, decided_by=p.decided_by,
                decision_note=p.decision_note, decided_at=p.decided_at,
                engine=p.engine.value,
                verification_outcome=p.verification_outcome.value,
                verification_detail=p.verification_detail or {}, verified_at=p.verified_at,
                created_at=p.created_at,
            )
            for p in audit.patches
        ],
        test_runs=[run_to_response(r) for r in audit.test_runs],
        recent_events=[_event_response(e) for e in events],
        report=(report.report if report else None),
    )


def _event_response(e: AgentEvent) -> AgentEventResponse:
    return AgentEventResponse(
        id=e.id, seq=e.seq, agent_role=e.agent_role.value, event_type=e.event_type.value,
        status=e.status, message=e.message, payload=e.payload or {}, engine=e.engine,
        created_at=e.created_at,
    )


@router.post("/{audit_id}/start", response_model=AckResponse, summary="Start (or re-queue) an audit")
def start_audit(
    audit_id: int,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> AckResponse:
    audit = get_audit_or_404(session, audit_id)
    assert_audit_access(session, audit, settings)
    if audit.status in TERMINAL_AUDIT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Audit {audit_id} is already '{audit.status.value}' and cannot be restarted. "
                "Create a new audit session to re-run the analysis."
            ),
        )
    if orchestrator.is_running(audit_id):
        return AckResponse(
            detail=f"Audit {audit_id} is already running.",
            data={"audit_id": audit_id, "queued": False, "status": audit.status.value},
        )
    queued = orchestrator.submit_audit(audit_id, settings)
    store.set_stage(session, audit, AuditStatus.QUEUED)
    store.record_event(
        session, audit_id, AgentRole.ORCHESTRATOR, EventType.STAGE_STARTED,
        "Audit queued for asynchronous execution.",
        {"queued": queued},
    )
    return AckResponse(
        detail="Audit queued. Long-running audits never depend on this HTTP request.",
        data={"audit_id": audit_id, "queued": queued, "status": AuditStatus.QUEUED.value},
    )


@router.get(
    "/{audit_id}/events",
    response_model=list[AgentEventResponse],
    summary="Agent/audit event stream (provenance spine)",
)
def list_events(
    audit_id: int,
    after_seq: int = Query(default=0, ge=0, description="Return only events with seq > after_seq"),
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> list[AgentEventResponse]:
    """Poll-friendly event feed. Pass the last seen ``seq`` to fetch only new events."""
    audit = get_audit_or_404(session, audit_id)
    assert_audit_access(session, audit, settings)
    events = session.scalars(
        select(AgentEvent)
        .where(AgentEvent.audit_id == audit_id, AgentEvent.seq > after_seq)
        .order_by(AgentEvent.seq)
        .limit(limit)
    ).all()
    return [_event_response(e) for e in events]


@router.get("/{audit_id}/findings", summary="Findings for an audit")
def list_findings(
    audit_id: int,
    severity: str | None = Query(default=None, max_length=20),
    category: str | None = Query(default=None, max_length=40),
    verification_status: str | None = Query(default=None, alias="status", max_length=40),
    file_path: str | None = Query(default=None, max_length=512),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    audit = get_audit_or_404(session, audit_id)
    assert_audit_access(session, audit, settings)
    from .findings import finding_to_response, run_to_response

    stmt = select(Finding).where(Finding.audit_id == audit_id).order_by(Finding.id)
    if severity:
        stmt = stmt.where(Finding.severity == severity)
    if category:
        stmt = stmt.where(Finding.category == category)
    if verification_status:
        stmt = stmt.where(Finding.verification_status == verification_status)
    if file_path:
        stmt = stmt.where(Finding.file_path.like(f"%{file_path}%"))
    findings = session.scalars(stmt).all()
    return {
        "audit_id": audit_id,
        "count": len(findings),
        "filters": {
            "severity": severity, "category": category,
            "verification_status": verification_status, "file_path": file_path,
        },
        "findings": [finding_to_response(f).model_dump() for f in findings],
    }


@router.get("/{audit_id}/patches", summary="Patch proposals for an audit")
def list_patches(
    audit_id: int,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    audit = get_audit_or_404(session, audit_id)
    assert_audit_access(session, audit, settings)
    patches = session.scalars(
        select(PatchProposal).where(PatchProposal.audit_id == audit_id).order_by(PatchProposal.id)
    ).all()
    return {"audit_id": audit_id, "count": len(patches), "patches": [_patch_dict(p) for p in patches]}


def _patch_dict(p: PatchProposal) -> dict[str, Any]:
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
    ).model_dump()


@router.get("/{audit_id}/test-runs", summary="Executed test runs for an audit")
def list_test_runs(
    audit_id: int,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    audit = get_audit_or_404(session, audit_id)
    assert_audit_access(session, audit, settings)
    runs = session.scalars(
        select(TestRun).where(TestRun.audit_id == audit_id).order_by(TestRun.id)
    ).all()
    return {
        "audit_id": audit_id,
        "count": len(runs),
        "test_runs": [run_to_response(r).model_dump() for r in runs],
    }


@router.get("/{audit_id}/report", response_model=ReportResponse, summary="Final audit report")
def get_report(
    audit_id: int,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> ReportResponse:
    """Return the persisted report. Reports survive restarts."""
    audit = get_audit_or_404(session, audit_id)
    assert_audit_access(session, audit, settings)
    report = store.latest_report(session, audit_id)
    if report is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No report has been generated for audit {audit_id} yet "
                f"(status={audit.status.value})."
            ),
        )
    payload = report.report or {}
    return ReportResponse(
        report_id=report.id,
        public_id=report.public_id,
        audit_id=report.audit_id,
        outcome=report.outcome,
        outcome_explanation=OUTCOME_EXPLANATIONS.get(report.outcome, ""),
        summary=report.summary,
        created_at=report.created_at,
        report=payload,
    )


__all__ = ["router"]
