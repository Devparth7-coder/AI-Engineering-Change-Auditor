"""Persistence helpers used by the orchestrator and the API.

Kept separate from the models so that background workers and request handlers
share one implementation of "how an audit is written down".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AgentEvent,
    AgentRole,
    AuditReport,
    AuditSession,
    AuditStatus,
    EngineKind,
    EventType,
    Finding,
    PatchProposal,
    Project,
    TestRun,
    utcnow,
)

STAGE_PROGRESS = {
    AuditStatus.CREATED: 0,
    AuditStatus.QUEUED: 5,
    AuditStatus.FETCHING_CONTEXT: 15,
    AuditStatus.ANALYZING: 30,
    AuditStatus.INVESTIGATING: 45,
    AuditStatus.GENERATING_TESTS: 60,
    AuditStatus.AWAITING_APPROVAL: 72,
    AuditStatus.VERIFYING: 85,
    AuditStatus.REPORTING: 95,
    AuditStatus.COMPLETED: 100,
    AuditStatus.COMPLETED_WITH_ERRORS: 100,
    AuditStatus.FAILED: 100,
    AuditStatus.CANCELLED: 100,
}


def next_seq(session: Session, audit_id: int) -> int:
    current = session.scalar(
        select(func.coalesce(func.max(AgentEvent.seq), 0)).where(AgentEvent.audit_id == audit_id)
    )
    return int(current or 0) + 1


def record_event(
    session: Session,
    audit_id: int,
    role: AgentRole,
    event_type: EventType,
    message: str = "",
    payload: dict[str, Any] | None = None,
    *,
    status: str = "info",
    engine: EngineKind | str = EngineKind.LOCAL_DETERMINISTIC,
    commit: bool = True,
) -> AgentEvent:
    """Append an immutable audit event. This is the provenance spine."""
    event = AgentEvent(
        audit_id=audit_id,
        seq=next_seq(session, audit_id),
        agent_role=role,
        event_type=event_type,
        status=status,
        message=message[:4000],
        payload=payload or {},
        engine=engine.value if isinstance(engine, EngineKind) else str(engine),
    )
    session.add(event)
    if commit:
        session.commit()
    return event


def set_stage(
    session: Session,
    audit: AuditSession,
    status: AuditStatus,
    *,
    stage: str = "",
    commit: bool = True,
) -> AuditSession:
    audit.status = status
    if stage:
        audit.stage = stage
    else:
        audit.stage = status.value
    audit.progress_pct = STAGE_PROGRESS.get(status, audit.progress_pct)
    if status in (AuditStatus.COMPLETED, AuditStatus.COMPLETED_WITH_ERRORS, AuditStatus.FAILED, AuditStatus.CANCELLED):
        audit.completed_at = utcnow()
    if commit:
        session.commit()
    return audit


def next_public_id(session: Session, model: type[Any], prefix: str, audit_id: int) -> str:
    """Readable, stable, per-audit identifiers: ``F-12-003``."""
    count = session.scalar(
        select(func.count()).select_from(model).where(model.audit_id == audit_id)
    )
    return f"{prefix}-{audit_id}-{int(count or 0) + 1:03d}"


def get_audit(session: Session, audit_id: int) -> AuditSession | None:
    return session.get(AuditSession, audit_id)


def get_project(session: Session, project_id: int) -> Project | None:
    return session.get(Project, project_id)


def find_or_create_project(
    session: Session,
    *,
    name: str,
    namespace: str = "",
    path_with_namespace: str = "",
    default_branch: str = "main",
    gitlab_project_id: int | None = None,
    web_url: str = "",
    local_path: str | None = None,
    origin: str = "local_demo",
) -> Project:
    stmt = select(Project)
    if gitlab_project_id is not None:
        stmt = stmt.where(Project.gitlab_project_id == gitlab_project_id)
    elif local_path:
        stmt = stmt.where(Project.local_path == local_path, Project.origin == origin)
    else:
        stmt = stmt.where(Project.name == name, Project.origin == origin)
    existing = session.scalar(stmt)
    if existing is not None:
        existing.default_branch = default_branch or existing.default_branch
        existing.web_url = web_url or existing.web_url
        session.commit()
        return existing
    project = Project(
        name=name,
        namespace=namespace,
        path_with_namespace=path_with_namespace or name,
        default_branch=default_branch,
        gitlab_project_id=gitlab_project_id,
        web_url=web_url,
        local_path=local_path,
        origin=origin,
    )
    session.add(project)
    session.commit()
    return project


def latest_report(session: Session, audit_id: int) -> AuditReport | None:
    return session.scalar(
        select(AuditReport)
        .where(AuditReport.audit_id == audit_id)
        .order_by(AuditReport.id.desc())
        .limit(1)
    )


def iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


__all__ = [
    "STAGE_PROGRESS",
    "find_or_create_project",
    "get_audit",
    "get_project",
    "iso",
    "latest_report",
    "next_public_id",
    "next_seq",
    "record_event",
    "set_stage",
]
