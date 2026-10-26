"""GitLab REST API client.

Server-side only.  The frontend never receives the token and never calls
privileged GitLab endpoints directly; it talks to this backend, which enforces
the project allow-list and audits every access.

Endpoints used (GitLab REST ``/api/v4``):

* ``GET  /projects``                       -- discover connected projects
* ``GET  /projects/:id``                   -- metadata, default branch
* ``GET  /projects/:id/merge_requests``    -- list MRs
* ``GET  /projects/:id/merge_requests/:iid``          -- MR detail, SHAs, branches
* ``GET  /projects/:id/merge_requests/:iid/changes``  -- diffs
* ``GET  /projects/:id/repository/files/:path/raw``   -- file at a ref
* ``GET  /projects/:id/repository/compare``           -- diff between SHAs
* ``GET  /projects/:id/pipelines``                    -- CI results
* ``GET  /projects/:id/pipelines/:pid/jobs``
* ``GET  /version``                                   -- instance version / probe

Retries are limited to idempotent GETs on network errors and 429/5xx.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings, get_settings
from app.security.sanitize import register_secret, sanitize_text

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GitLabError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class GitLabNotConfigured(GitLabError):
    """Raised when no GitLab URL/token is configured (demo mode)."""


@dataclass
class GitLabClient:
    base_url: str
    token: str
    api_version: str = "v4"
    timeout: float = 20.0
    max_retries: int = 2
    allowed_project_ids: set[int] = field(default_factory=set)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "GitLabClient":
        settings = settings or get_settings()
        if not settings.is_live_gitlab:
            raise GitLabNotConfigured(
                "GitLab is not configured. Set GITLAB_URL and GITLAB_TOKEN to enable "
                "live integration, or use the local demonstration mode."
            )
        register_secret(settings.gitlab_token)
        return cls(
            base_url=settings.gitlab_url,
            token=settings.gitlab_token,
            api_version=settings.gitlab_api_version,
            timeout=settings.gitlab_timeout_seconds,
            max_retries=settings.gitlab_max_retries,
            allowed_project_ids=settings.allowed_project_ids,
        )

    # ------------------------------------------------------------------ http
    def _headers(self) -> dict[str, str]:
        return {
            "PRIVATE-TOKEN": self.token,
            "Accept": "application/json",
            "User-Agent": "ai-engineering-change-auditor/1.0",
        }

    @property
    def api_root(self) -> str:
        return f"{self.base_url}/api/{self.api_version}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.api_root}/{path.lstrip('/')}"
        attempt = 0
        last_exc: Exception | None = None
        while attempt <= self.max_retries:
            attempt += 1
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    resp = client.request(method, url, headers=self._headers(), params=params)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt > self.max_retries or method != "GET":
                    raise GitLabError(
                        f"network error calling GitLab ({type(exc).__name__})"
                    ) from exc
                time.sleep(min(2 ** attempt * 0.25, 2.0))
                continue

            if resp.status_code in RETRYABLE_STATUS and method == "GET" and attempt <= self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if (retry_after or "").isdigit() else min(2 ** attempt * 0.25, 2.0)
                logger.warning("GitLab %s on %s; retrying in %.1fs", resp.status_code, url, delay)
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise GitLabError(
                    f"GitLab API error {resp.status_code} for {method} {url}",
                    status=resp.status_code,
                    detail=sanitize_text(resp.text[:500]),
                )
            if raw:
                return resp.text
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as exc:
                raise GitLabError(f"GitLab returned non-JSON response from {url}") from exc
        raise GitLabError(f"GitLab request failed after {attempt} attempts: {last_exc}")

    # ------------------------------------------------------------ authorization
    def assert_project_allowed(self, project_id: int | str) -> None:
        if not self.allowed_project_ids:
            return
        try:
            pid = int(project_id)
        except (TypeError, ValueError):
            raise GitLabError(f"cannot authorise non-numeric project id {project_id!r}")
        if pid not in self.allowed_project_ids:
            raise GitLabError(
                f"project {pid} is not in GITLAB_ALLOWED_PROJECT_IDS; access denied",
                status=403,
            )

    # ------------------------------------------------------------------ probes
    def version(self) -> dict[str, Any]:
        data = self._request("GET", "/version")
        return data or {}

    def probe(self) -> dict[str, Any]:
        """Cheap connectivity/permission probe used by the settings screen."""
        result: dict[str, Any] = {
            "configured": True,
            "base_url": self.base_url,
            "reachable": False,
            "version": "",
            "error": "",
        }
        try:
            v = self.version()
            result["reachable"] = True
            result["version"] = str(v.get("version", ""))
            result["revision"] = str(v.get("revision", ""))[:12]
        except GitLabError as exc:
            result["error"] = str(exc)
        return result

    # ---------------------------------------------------------------- projects
    def list_projects(self, search: str = "", limit: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "membership": "true",
            "per_page": min(limit, 100),
            "order_by": "last_activity_at",
            "simple": "false",
        }
        if search:
            params["search"] = search
        data = self._request("GET", "/projects", params=params) or []
        return [self._slim_project(p) for p in data]

    def get_project(self, project_id: int | str) -> dict[str, Any]:
        pid = quote(str(project_id), safe="")
        if str(project_id).isdigit():
            self.assert_project_allowed(project_id)
        data = self._request("GET", f"/projects/{pid}") or {}
        return self._slim_project(data)

    @staticmethod
    def _slim_project(p: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": p.get("id"),
            "name": p.get("name", ""),
            "path_with_namespace": p.get("path_with_namespace", ""),
            "namespace": (p.get("namespace") or {}).get("full_path", ""),
            "default_branch": p.get("default_branch") or "main",
            "web_url": p.get("web_url", ""),
            "description": p.get("description") or "",
            "last_activity_at": p.get("last_activity_at", ""),
            "visibility": p.get("visibility", ""),
        }

    # ------------------------------------------------------------ merge requests
    def list_merge_requests(self, project_id: int | str, state: str = "opened", limit: int = 30) -> list[dict[str, Any]]:
        pid = quote(str(project_id), safe="")
        data = self._request(
            "GET",
            f"/projects/{pid}/merge_requests",
            params={"state": state, "per_page": min(limit, 100), "order_by": "updated_at"},
        ) or []
        return [self._slim_mr(m) for m in data]

    def get_merge_request(self, project_id: int | str, iid: int) -> dict[str, Any]:
        pid = quote(str(project_id), safe="")
        data = self._request("GET", f"/projects/{pid}/merge_requests/{iid}") or {}
        return self._slim_mr(data)

    def get_merge_request_changes(self, project_id: int | str, iid: int) -> dict[str, Any]:
        pid = quote(str(project_id), safe="")
        data = self._request("GET", f"/projects/{pid}/merge_requests/{iid}/changes") or {}
        changes = data.get("changes") or []
        return {
            "mr": self._slim_mr(data),
            "changes": [
                {
                    "old_path": c.get("old_path"),
                    "new_path": c.get("new_path"),
                    "diff": c.get("diff", ""),
                    "new_file": bool(c.get("new_file")),
                    "renamed_file": bool(c.get("renamed_file")),
                    "deleted_file": bool(c.get("deleted_file")),
                }
                for c in changes
            ],
            "diff_refs": data.get("diff_refs") or {},
        }

    @staticmethod
    def _slim_mr(m: dict[str, Any]) -> dict[str, Any]:
        return {
            "iid": m.get("iid"),
            "title": m.get("title", ""),
            "state": m.get("state", ""),
            "source_branch": m.get("source_branch", ""),
            "target_branch": m.get("target_branch", ""),
            "sha": m.get("sha", ""),
            "diff_head_sha": (m.get("diff_refs") or {}).get("head_sha", ""),
            "base_sha": (m.get("diff_refs") or {}).get("base_sha", ""),
            "start_sha": (m.get("diff_refs") or {}).get("start_sha", ""),
            "web_url": m.get("web_url", ""),
            "author": (m.get("author") or {}).get("username", ""),
            "created_at": m.get("created_at", ""),
            "updated_at": m.get("updated_at", ""),
            "draft": bool(m.get("draft")),
            "has_conflicts": bool(m.get("has_conflicts")),
            "pipeline_status": ((m.get("head_pipeline") or {}).get("status"))
            or ((m.get("pipeline") or {}).get("status"))
            or "",
        }

    # ------------------------------------------------------------------- files
    def file_raw(self, project_id: int | str, path: str, ref: str) -> str:
        pid = quote(str(project_id), safe="")
        fpath = quote(path, safe="")
        try:
            return self._request(
                "GET", f"/projects/{pid}/repository/files/{fpath}/raw", params={"ref": ref}, raw=True
            )
        except GitLabError as exc:
            if exc.status == 404:
                return ""
            raise

    def compare(self, project_id: int | str, from_ref: str, to_ref: str) -> dict[str, Any]:
        pid = quote(str(project_id), safe="")
        data = self._request(
            "GET", f"/projects/{pid}/repository/compare", params={"from": from_ref, "to": to_ref}
        ) or {}
        return {
            "commits": [
                {"id": c.get("id"), "title": c.get("title"), "created_at": c.get("created_at")}
                for c in (data.get("commits") or [])
            ],
            "diffs": [
                {
                    "old_path": d.get("old_path"),
                    "new_path": d.get("new_path"),
                    "diff": d.get("diff", ""),
                    "new_file": bool(d.get("new_file")),
                    "deleted_file": bool(d.get("deleted_file")),
                }
                for d in (data.get("diffs") or [])
            ],
        }

    def list_tree(self, project_id: int | str, ref: str, path: str = "", recursive: bool = True) -> list[dict[str, Any]]:
        pid = quote(str(project_id), safe="")
        data = self._request(
            "GET",
            f"/projects/{pid}/repository/tree",
            params={"ref": ref, "path": path, "recursive": str(recursive).lower(), "per_page": 100},
        ) or []
        return [{"path": t.get("path"), "type": t.get("type"), "name": t.get("name")} for t in data]

    # --------------------------------------------------------------- pipelines
    def list_pipelines(self, project_id: int | str, ref: str = "", limit: int = 10) -> list[dict[str, Any]]:
        pid = quote(str(project_id), safe="")
        params: dict[str, Any] = {"per_page": min(limit, 100)}
        if ref:
            params["ref"] = ref
        data = self._request("GET", f"/projects/{pid}/pipelines", params=params) or []
        return [
            {
                "id": p.get("id"),
                "status": p.get("status"),
                "ref": p.get("ref"),
                "sha": p.get("sha"),
                "web_url": p.get("web_url"),
                "created_at": p.get("created_at"),
                "updated_at": p.get("updated_at"),
            }
            for p in data
        ]

    def pipeline_jobs(self, project_id: int | str, pipeline_id: int) -> list[dict[str, Any]]:
        pid = quote(str(project_id), safe="")
        data = self._request("GET", f"/projects/{pid}/pipelines/{pipeline_id}/jobs", params={"per_page": 100}) or []
        return [
            {
                "id": j.get("id"),
                "name": j.get("name"),
                "stage": j.get("stage"),
                "status": j.get("status"),
                "duration": j.get("duration"),
                "web_url": j.get("web_url"),
            }
            for j in data
        ]


__all__ = [
    "GitLabClient",
    "GitLabError",
    "GitLabNotConfigured",
]
