"""Project and merge-request discovery endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import Project
from app.schemas import MergeRequestResponse, ProjectResponse
from app.services import gitrepo, store
from app.services.gitlab_client import GitLabClient, GitLabError, GitLabNotConfigured
from .deps import get_project_or_404, require_auth

router = APIRouter(tags=["projects"])


def _to_response(p: Project) -> ProjectResponse:
    return ProjectResponse(
        id=p.id,
        gitlab_project_id=p.gitlab_project_id,
        name=p.name,
        namespace=p.namespace,
        path_with_namespace=p.path_with_namespace,
        default_branch=p.default_branch,
        web_url=p.web_url,
        local_path=p.local_path,
        origin=p.origin,
        created_at=p.created_at,
    )


@router.get("", response_model=list[ProjectResponse], summary="List known projects")
def list_projects(
    include_live: bool = Query(
        default=False,
        description="Also query GitLab for projects the token can access.",
    ),
    search: str = Query(default="", max_length=120),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> list[ProjectResponse]:
    """Return persisted projects, and optionally merge in live GitLab results."""
    out: list[ProjectResponse] = []
    if settings.demo_enabled and Path(settings.demo_repo_path).is_dir():
        demo = store.find_or_create_project(
            session,
            name=Path(settings.demo_repo_path).name,
            namespace="local-demo",
            path_with_namespace=f"local-demo/{Path(settings.demo_repo_path).name}",
            default_branch=gitrepo.default_branch(Path(settings.demo_repo_path)),
            local_path=str(settings.demo_repo_path),
            origin="local_demo",
        )
        out.append(_to_response(demo))

    stmt = select(Project).order_by(Project.id)
    for p in session.scalars(stmt).all():
        if any(o.id == p.id for o in out):
            continue
        out.append(_to_response(p))

    if include_live:
        try:
            client = GitLabClient.from_settings(settings)
            for raw in client.list_projects(search=search):
                project = store.find_or_create_project(
                    session,
                    name=raw["name"],
                    namespace=raw["namespace"],
                    path_with_namespace=raw["path_with_namespace"],
                    default_branch=raw["default_branch"],
                    gitlab_project_id=raw["id"],
                    web_url=raw["web_url"],
                    origin="gitlab",
                )
                if not any(o.id == project.id for o in out):
                    out.append(_to_response(project))
        except GitLabNotConfigured:
            pass  # demo-only environment; the flag is reported by /api/health
        except GitLabError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return out


@router.get("/{project_id}", response_model=ProjectResponse, summary="Project metadata")
def get_project(
    project_id: int,
    session: Session = Depends(get_db),
    _: None = Depends(require_auth),
) -> ProjectResponse:
    return _to_response(get_project_or_404(session, project_id))


@router.get(
    "/{project_id}/merge-requests",
    response_model=list[MergeRequestResponse],
    summary="Merge requests for a project",
)
def list_merge_requests(
    project_id: int,
    state: str = Query(default="opened", pattern="^(opened|closed|merged|all)$"),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> list[MergeRequestResponse]:
    """Live MRs come from GitLab; demo projects expose their local branches.

    The demonstration mode is labelled explicitly so a viewer cannot mistake a
    bundled example for a live merge request.
    """
    project = get_project_or_404(session, project_id)
    if project.origin == "local_demo" and project.local_path:
        repo = Path(project.local_path)
        if not repo.is_dir():
            raise HTTPException(status_code=404, detail="demonstration repository is missing")
        try:
            branches = gitrepo.branches(repo)
        except gitrepo.GitError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        default = gitrepo.default_branch(repo)
        out: list[MergeRequestResponse] = []
        for branch in branches:
            if branch == default:
                continue
            try:
                head = gitrepo.commit_info(repo, branch, branch)
                base = gitrepo.commit_info(repo, default, default)
                changed = gitrepo.changed_files(repo, base.sha, head.sha)
            except gitrepo.GitError:
                continue
            out.append(
                MergeRequestResponse(
                    iid=settings.demo_merge_request_iid,
                    title=f"[DEMO] {branch} -> {default} ({len(changed)} file(s) changed)",
                    state="opened",
                    source_branch=branch,
                    target_branch=default,
                    sha=head.sha,
                    diff_head_sha=head.sha,
                    base_sha=base.sha,
                    web_url="",
                    author="local-demo",
                    pipeline_status="not_available_in_demo_mode",
                    draft=False,
                    has_conflicts=False,
                )
            )
        return out

    if project.gitlab_project_id is None:
        raise HTTPException(status_code=400, detail="Project has no GitLab id and no local path")
    try:
        client = GitLabClient.from_settings(settings)
        raw = client.list_merge_requests(project.gitlab_project_id, state=state)
    except GitLabNotConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GitLabError as exc:
        code = 502 if exc.status is None else (403 if exc.status == 403 else 502)
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return [MergeRequestResponse(**m) for m in raw]


@router.get(
    "/{project_id}/branches",
    summary="Branches and pinned commit SHAs",
)
def list_branches(
    project_id: int,
    session: Session = Depends(get_db),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    project = get_project_or_404(session, project_id)
    if project.origin != "local_demo" or not project.local_path:
        raise HTTPException(
            status_code=409,
            detail="Branch listing is available for local demonstration repositories.",
        )
    repo = Path(project.local_path)
    try:
        return {
            "default_branch": gitrepo.default_branch(repo),
            "branches": [
                {
                    "name": b,
                    "sha": gitrepo.commit_info(repo, b, b).sha,
                    "subject": gitrepo.commit_info(repo, b, b).subject,
                }
                for b in gitrepo.branches(repo)
            ],
        }
    except gitrepo.GitError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


__all__ = ["router"]
