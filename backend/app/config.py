"""Application configuration.

All credentials and deployment-specific values come from the environment.
Nothing secret is ever hardcoded here or logged (see ``app.security.sanitize``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ core
    app_name: str = "AI Engineering Change Auditor"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # -------------------------------------------------------------- database
    # PostgreSQL is the documented production target (JSONB).  SQLite is the
    # zero-dependency development/demo fallback so the audit history still
    # persists across restarts when Postgres is unavailable.
    database_url: str = Field(
        default_factory=lambda: f"sqlite:///{REPO_ROOT / 'var' / 'auditor.db'}"
    )
    database_echo: bool = False

    # ------------------------------------------------------ GitLab REST / Duo
    gitlab_url: str = ""
    gitlab_token: str = ""
    gitlab_api_version: str = "v4"
    gitlab_timeout_seconds: float = 20.0
    gitlab_max_retries: int = 2
    # Only these projects may be audited. Empty = allow any project the token
    # can read (still subject to server-side authorization checks).
    gitlab_allowed_project_ids: str = ""

    # GitLab Duo Agent Platform (Flow Registry v1 / Flows API).
    duo_enabled: bool = False
    duo_flow_definition: str = "change-audit/v1"
    duo_ai_catalog_item_consumer_id: int | None = None
    duo_environment: Literal["ambient", "web", "ide", "chat", "chat_partial"] = "ambient"
    duo_poll_interval_seconds: float = 3.0
    duo_poll_timeout_seconds: float = 900.0
    # Privilege IDs granted to the flow. 1=read_write_files, 2=read_only_gitlab,
    # 3=read_write_gitlab, 4=run_commands.  The audit flow is deliberately
    # read-mostly: it must never push to a default branch.
    duo_agent_privileges: str = "1,2,4"
    duo_pre_approved_privileges: str = "2"
    duo_flow_config_path: str = "agents/flows/change-audit/flow.yaml"

    # ------------------------------------------------------------ demo mode
    demo_enabled: bool = True
    demo_repo_path: str = str(REPO_ROOT / "examples" / "discount-service")
    demo_source_branch: str = "feature/refactor-discount"
    demo_target_branch: str = "main"
    demo_merge_request_iid: int = 1

    # -------------------------------------------------- execution / sandbox
    # container      -> docker (strongest, used automatically when available)
    # linux_namespace-> unshare user+net+mount ns, read-only binds, tmpfs mask
    # process        -> rlimits + scrubbed env + temp workspace (weakest)
    sandbox_mode: Literal["auto", "container", "linux_namespace", "process"] = "auto"
    sandbox_root: str = ""  # default: system temp dir
    sandbox_timeout_seconds: int = 120
    sandbox_memory_mb: int = 1024
    sandbox_cpu_seconds: int = 60
    sandbox_max_file_mb: int = 32
    sandbox_max_processes: int = 256
    sandbox_allow_network: bool = False
    sandbox_keep_workspaces: bool = False
    sandbox_docker_image: str = "python:3.12-slim"

    # -------------------------------------------------------- audit workflow
    test_framework: Literal["pytest"] = "pytest"
    max_findings_per_audit: int = 25
    auto_generate_tests: bool = True
    # Never auto-approve a patch. Human approval is a hard requirement.
    auto_approve_patches: bool = False
    audit_worker_concurrency: int = 2

    # ------------------------------------------------------------- api
    api_prefix: str = "/api"
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    # The UI polls an audit's event stream roughly once per second, so the
    # default must comfortably exceed a few concurrent viewers.
    rate_limit_per_minute: int = 600
    # Bearer token required for mutating endpoints when set.
    api_auth_token: str = ""

    @field_validator("gitlab_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_project_ids(self) -> set[int]:
        out: set[int] = set()
        for part in self.gitlab_allowed_project_ids.split(","):
            part = part.strip()
            if part.isdigit():
                out.add(int(part))
        return out

    @property
    def duo_privilege_ids(self) -> list[int]:
        return [int(x) for x in self.duo_agent_privileges.split(",") if x.strip().isdigit()]

    @property
    def duo_pre_approved_ids(self) -> list[int]:
        return [int(x) for x in self.duo_pre_approved_privileges.split(",") if x.strip().isdigit()]

    @property
    def is_live_gitlab(self) -> bool:
        return bool(self.gitlab_url and self.gitlab_token)

    def sandbox_root_path(self) -> Path:
        if self.sandbox_root:
            p = Path(self.sandbox_root)
        else:
            import tempfile

            p = Path(tempfile.gettempdir()) / "eca-sandboxes"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def redacted(self) -> dict[str, object]:
        """Configuration safe to expose over the API (secrets masked)."""
        secrets = {
            "gitlab_token",
            "api_auth_token",
            "database_url",
        }
        out: dict[str, object] = {}
        for name in self.model_fields:
            value = getattr(self, name)
            if name in secrets:
                out[name] = "***redacted***" if value else ""
            else:
                out[name] = value
        return out


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.split("///", 1)[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return settings
