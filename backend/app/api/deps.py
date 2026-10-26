"""Shared API dependencies: authorization, rate limiting, identifier resolution.

Authorization is server-side only.  The frontend never receives a GitLab token
and never calls GitLab directly; every privileged action passes through here.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import AuditSession, Finding, PatchProposal, Project

_window: dict[str, deque[float]] = defaultdict(deque)


def require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Enforce the optional bearer token and a per-client rate limit."""
    if settings.api_auth_token:
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
        if token != settings.api_auth_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A valid bearer token is required for this endpoint.",
                headers={"WWW-Authenticate": "Bearer"},
            )
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _window[client]
    while bucket and now - bucket[0] > 60.0:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded ({limit} requests/minute).",
        )
    bucket.append(now)


def get_project_or_404(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return project


def get_audit_or_404(session: Session, audit_id: int) -> AuditSession:
    audit = session.get(AuditSession, audit_id)
    if audit is None:
        raise HTTPException(status_code=404, detail=f"Audit {audit_id} not found")
    return audit


def resolve_finding(session: Session, finding_id: str | int) -> Finding:
    """Accept either the integer id or the readable public id (``F-3-001``)."""
    finding: Finding | None = None
    if isinstance(finding_id, int) or str(finding_id).isdigit():
        finding = session.get(Finding, int(finding_id))
    if finding is None:
        finding = session.scalar(select(Finding).where(Finding.public_id == str(finding_id)))
    if finding is None:
        raise HTTPException(
            status_code=404, detail=f"Finding '{finding_id}' not found"
        )
    return finding


def resolve_patch(session: Session, patch_id: str | int) -> PatchProposal:
    patch: PatchProposal | None = None
    if isinstance(patch_id, int) or str(patch_id).isdigit():
        patch = session.get(PatchProposal, int(patch_id))
    if patch is None:
        patch = session.scalar(
            select(PatchProposal).where(PatchProposal.public_id == str(patch_id))
        )
    if patch is None:
        raise HTTPException(status_code=404, detail=f"Patch '{patch_id}' not found")
    return patch


def assert_audit_access(session: Session, audit: AuditSession, settings: Settings) -> None:
    """Confirm the audit's project is permitted under the configured allow-list."""
    project = session.get(Project, audit.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Audit references a missing project")
    allowed = settings.allowed_project_ids
    if allowed and project.gitlab_project_id not in allowed:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Project {project.gitlab_project_id} is not in GITLAB_ALLOWED_PROJECT_IDS"
            ),
        )


def enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


__all__ = [
    "assert_audit_access",
    "enum_value",
    "get_audit_or_404",
    "get_project_or_404",
    "require_auth",
    "resolve_finding",
    "resolve_patch",
]
