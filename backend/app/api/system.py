"""System endpoints: health, configuration, dashboard, Duo availability."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import REPO_ROOT, Settings, get_settings
from app.db import get_db, get_engine
from app.models import (
    AgentEvent,
    ApprovalStatus,
    AuditSession,
    AuditStatus,
    Finding,
    PatchProposal,
    Project,
    ReproOutcome,
    TestRun,
    VerificationOutcome,
    VerificationStatus,
)
from app.schemas import (
    AckResponse,
    DashboardResponse,
    DuoAvailabilityResponse,
    HealthResponse,
    SettingsUpdateRequest,
)
from app.services import demo_repo, sandbox
from app.services.duo import DuoAgentPlatformClient
from .deps import require_auth

router = APIRouter(tags=["system"])

APP_VERSION = "1.0.0"


@router.get("/health", response_model=HealthResponse, summary="Liveness and capability probe")
def health(
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_db),
) -> HealthResponse:
    """Reports real capability, not an optimistic constant.

    ``degraded`` means the API is serving but a documented capability (database,
    GitLab, Duo, or strong code isolation) is unavailable.
    """
    engine = get_engine()
    dialect = engine.dialect.name
    try:
        with engine.connect() as conn:
            conn.execute(select(func.count()).select_from(Project))
        db_ok, db_state = True, "connected"
    except Exception as exc:  # noqa: BLE001
        db_ok, db_state = False, f"unavailable: {type(exc).__name__}"

    isolation = sandbox.probe_isolation(settings)
    if settings.is_live_gitlab:
        from app.services.gitlab_client import GitLabClient, GitLabError

        try:
            gitlab_state = GitLabClient.from_settings(settings).probe()
        except GitLabError as exc:
            gitlab_state = {"configured": True, "reachable": False, "error": str(exc)}
    else:
        gitlab_state = {
            "configured": False,
            "reachable": False,
            "error": "",
            "note": (
                "GITLAB_URL/GITLAB_TOKEN are not set. Local demonstration mode is "
                "available and is clearly labelled as such."
            ),
        }
    duo_state = DuoAgentPlatformClient(settings).probe().to_dict()
    demo_state = demo_repo.ensure_demo_repo(settings)

    degraded = (
        (not db_ok)
        or isolation["selected_isolation_level"] == "process_restricted"
        or (settings.demo_enabled and not settings.is_live_gitlab and not demo_state.usable)
    )
    return HealthResponse(
        status="ok" if not degraded else "degraded",
        app=settings.app_name,
        version=APP_VERSION,
        environment=settings.environment,
        database=db_state,
        database_dialect=dialect,
        isolation=isolation,
        gitlab=gitlab_state,
        duo=duo_state,
        demo=demo_state.to_dict(),
        # Only claim demo mode when the fixture was actually verified usable.
        demo_mode_available=demo_state.usable,
    )


@router.get("/duo/availability", response_model=DuoAvailabilityResponse, summary="Duo Agent Platform readiness")
def duo_availability(settings: Settings = Depends(get_settings)) -> DuoAvailabilityResponse:
    """State plainly whether the mandatory platform integration can run."""
    info = DuoAgentPlatformClient(settings).probe()
    flow_path = REPO_ROOT / settings.duo_flow_config_path
    return DuoAvailabilityResponse(
        available=info.available,
        reason=info.reason,
        missing_prerequisite=info.prerequisite,
        checks=[c.to_dict() for c in info.checks],
        gitlab_version=info.gitlab_version,
        flow_definition=info.flow_definition,
        flow_config_path=info.flow_config_path,
        flow_yaml_present=flow_path.is_file(),
        flow_yaml_path=str(flow_path),
        engine_used_when_unavailable=info.to_dict()["engine_used_when_unavailable"],
    )


@router.get("/settings", summary="Effective configuration with secrets redacted")
def read_settings(
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    return {
        "config": settings.redacted(),
        "isolation": sandbox.probe_isolation(settings),
        "duo": DuoAgentPlatformClient(settings).probe().to_dict(),
        "demo_repository": {
            **demo_repo.ensure_demo_repo(settings).to_dict(),
            "source_branch": settings.demo_source_branch,
            "target_branch": settings.demo_target_branch,
            "merge_request_iid": settings.demo_merge_request_iid,
        },
        "test_framework": settings.test_framework,
        "notes": [
            "Secret values are never returned by this endpoint.",
            "Credentials are configured server-side through environment variables only.",
        ],
    }


@router.put("/settings/preferences", response_model=AckResponse, summary="Update non-secret runtime preferences")
def update_preferences(
    body: SettingsUpdateRequest,
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> AckResponse:
    """Runtime preferences only. Credentials cannot be changed through the API."""
    applied: dict[str, Any] = {}
    for name, value in body.model_dump(exclude_none=True).items():
        if name in Settings.model_fields and name not in {
            "gitlab_token",
            "api_auth_token",
            "database_url",
        }:
            setattr(settings, name, value)
            applied[name] = value
    sandbox._PROBE_CACHE.clear()  # re-probe isolation after a mode change
    if not applied:
        raise HTTPException(status_code=400, detail="No updatable preferences supplied")
    return AckResponse(detail="Preferences updated for this process.", data=applied)


@router.get("/dashboard", response_model=DashboardResponse, summary="Metrics computed from persisted records")
def dashboard(
    limit: int = 8,
    session: Session = Depends(get_db),
) -> DashboardResponse:
    """Every figure is a query result. No statistic here is ever hardcoded."""
    total_audits = int(session.scalar(select(func.count()).select_from(AuditSession)) or 0)
    active_statuses = [
        s.value for s in AuditStatus
        if s not in {AuditStatus.COMPLETED, AuditStatus.COMPLETED_WITH_ERRORS, AuditStatus.FAILED, AuditStatus.CANCELLED, AuditStatus.CREATED}
    ]
    active_audits = int(
        session.scalar(
            select(func.count()).select_from(AuditSession).where(AuditSession.status.in_(active_statuses))
        ) or 0
    )
    total_findings = int(session.scalar(select(func.count()).select_from(Finding)) or 0)

    sev_rows = session.execute(
        select(Finding.severity, func.count()).group_by(Finding.severity)
    ).all()
    by_severity = {str(k.value if hasattr(k, "value") else k): int(v) for k, v in sev_rows}

    vs_rows = session.execute(
        select(Finding.verification_status, func.count()).group_by(Finding.verification_status)
    ).all()
    by_status = {str(k.value if hasattr(k, "value") else k): int(v) for k, v in vs_rows}

    status_rows = session.execute(
        select(AuditSession.status, func.count()).group_by(AuditSession.status)
    ).all()
    audits_by_status = {str(k.value if hasattr(k, "value") else k): int(v) for k, v in status_rows}

    engine_rows = session.execute(
        select(AuditSession.engine, func.count()).group_by(AuditSession.engine)
    ).all()
    engine_usage = {str(k.value if hasattr(k, "value") else k): int(v) for k, v in engine_rows}

    verified_fixes = int(
        session.scalar(
            select(func.count())
            .select_from(PatchProposal)
            .where(
                PatchProposal.verification_outcome
                == VerificationOutcome.VERIFIED_FOR_TESTED_BEHAVIOR
            )
        )
        or 0
    )
    pending_approval = int(
        session.scalar(
            select(func.count())
            .select_from(PatchProposal)
            .where(PatchProposal.approval_status == ApprovalStatus.PENDING)
        )
        or 0
    )
    needing_attention = int(
        session.scalar(
            select(func.count())
            .select_from(Finding)
            .where(
                Finding.severity.in_(["critical", "high"]),
                Finding.verification_status.in_(
                    [
                        VerificationStatus.REPRODUCED,
                        VerificationStatus.FIX_PROPOSED,
                        VerificationStatus.FIX_APPROVED,
                        VerificationStatus.FIX_VERIFICATION_FAILED,
                        VerificationStatus.REGRESSION_DETECTED,
                        VerificationStatus.EXECUTION_ERROR,
                    ]
                ),
            )
        )
        or 0
    )
    reproduced = int(
        session.scalar(
            select(func.count()).select_from(Finding).where(Finding.repro_outcome == ReproOutcome.REPRODUCED)
        )
        or 0
    )

    run_rows = session.execute(
        select(TestRun.execution_status, func.count()).group_by(TestRun.execution_status)
    ).all()
    test_runs_by_status = {str(k.value if hasattr(k, "value") else k): int(v) for k, v in run_rows}
    iso_rows = session.execute(
        select(TestRun.isolation_level, func.count()).group_by(TestRun.isolation_level)
    ).all()
    isolation_used = {str(k.value if hasattr(k, "value") else k): int(v) for k, v in iso_rows}
    totals_runs = int(session.scalar(select(func.count()).select_from(TestRun)) or 0)
    sums = session.execute(
        select(
            func.coalesce(func.sum(TestRun.tests_total), 0),
            func.coalesce(func.sum(TestRun.tests_passed), 0),
            func.coalesce(func.sum(TestRun.tests_failed), 0),
            func.coalesce(func.sum(TestRun.tests_skipped), 0),
            func.coalesce(func.sum(TestRun.tests_errored), 0),
        )
    ).one()

    recent_audit_rows = session.scalars(
        select(AuditSession).order_by(AuditSession.id.desc()).limit(limit)
    ).all()
    recent_events = session.scalars(
        select(AgentEvent).order_by(AgentEvent.id.desc()).limit(limit * 2)
    ).all()

    # Real CI/CD results, when a live GitLab project is configured.
    recent_ci: list[dict[str, Any]] = []
    settings = get_settings()
    if settings.is_live_gitlab:
        try:
            from app.services.gitlab_client import GitLabClient

            client = GitLabClient.from_settings(settings)
            projects = session.scalars(
                select(Project).where(Project.gitlab_project_id.is_not(None)).limit(3)
            ).all()
            for project in projects:
                for pipe in client.list_pipelines(project.gitlab_project_id, limit=3):
                    recent_ci.append({"project": project.path_with_namespace, **pipe})
        except Exception as exc:  # noqa: BLE001
            recent_ci.append({"error": f"could not read pipelines: {type(exc).__name__}"})
    else:
        recent_ci.append(
            {
                "note": (
                    "No live GitLab project is configured, so no CI/CD results are "
                    "available. This panel never fabricates pipeline data."
                )
            }
        )

    def _audit_brief(a: AuditSession) -> dict[str, Any]:
        return {
            "id": a.id,
            "project_id": a.project_id,
            "merge_request_iid": a.merge_request_iid,
            "merge_request_title": a.merge_request_title,
            "source_branch": a.source_branch,
            "target_branch": a.target_branch,
            "source_commit_sha": a.source_commit_sha,
            "target_commit_sha": a.target_commit_sha,
            "status": a.status.value,
            "stage": a.stage,
            "progress_pct": a.progress_pct,
            "mode": a.mode,
            "engine": a.engine.value,
            "duo_session_id": a.duo_session_id,
            "duo_web_url": a.duo_web_url,
            "duo_availability": a.duo_availability or {},
            "started_at": a.started_at,
            "completed_at": a.completed_at,
            "error_detail": a.error_detail,
            "created_at": a.created_at,
            "findings_count": len(a.findings),
            "patches_count": len(a.patches),
            "test_runs_count": len(a.test_runs),
            "outcome": "",
        }

    return DashboardResponse(
        totals={
            "audits": total_audits,
            "active_audits": active_audits,
            "projects": int(session.scalar(select(func.count()).select_from(Project)) or 0),
            "findings": total_findings,
            "findings_reproduced": reproduced,
            "findings_requiring_attention": needing_attention,
            "patches_pending_approval": pending_approval,
            "verified_fixes": verified_fixes,
            "test_runs": totals_runs,
        },
        audits_by_status=audits_by_status,
        findings_by_severity=by_severity,
        findings_by_verification_status=by_status,
        verified_fixes=verified_fixes,
        patches_pending_approval=pending_approval,
        findings_requiring_attention=needing_attention,
        test_runs={
            "runs": totals_runs,
            "by_status": test_runs_by_status,
            "tests_total": int(sums[0]),
            "tests_passed": int(sums[1]),
            "tests_failed": int(sums[2]),
            "tests_skipped": int(sums[3]),
            "tests_errored": int(sums[4]),
        },
        isolation_levels_used=isolation_used,
        recent_audits=[_audit_brief(a) for a in recent_audit_rows],  # type: ignore[arg-type]
        recent_events=list(recent_events),
        recent_ci_results=recent_ci,
        engine_usage=engine_usage,
    )


__all__ = ["APP_VERSION", "router"]
