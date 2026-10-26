"""FastAPI application entry point.

Run locally:

    cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Interactive API documentation is served at ``/api/docs`` (Swagger UI) and
``/api/redoc``; the machine-readable schema is at ``/api/openapi.json``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import audits, findings, patches, projects, system
from .config import get_settings
from .db import init_db
from .security.sanitize import register_secret, sanitize_text
from .services import demo_repo, sandbox

logger = logging.getLogger("auditor")

APP_VERSION = "1.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001, ANN201
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    if settings.gitlab_token:
        register_secret(settings.gitlab_token)
    if settings.api_auth_token:
        register_secret(settings.api_auth_token)
    init_db()
    isolation = sandbox.probe_isolation(settings)
    demo = demo_repo.ensure_demo_repo(settings, refresh=True)
    app.state.demo_repo = demo.to_dict()
    logger.info(
        "startup: env=%s db=%s isolation=%s gitlab=%s duo=%s demo=%s",
        settings.environment,
        settings.database_url.split("://", 1)[0],
        isolation["selected_isolation_level"],
        "configured" if settings.is_live_gitlab else "not configured (demo mode available)",
        "enabled" if settings.duo_enabled else "disabled",
        demo.status,
    )
    if settings.demo_enabled and not demo.usable:
        logger.warning(
            "Demonstration mode is NOT usable (%s): %s. Run "
            "examples/discount-service/make_demo_repo.py to rebuild the fixture.",
            demo.status, demo.detail,
        )
    elif demo.status == demo_repo.STATUS_BOOTSTRAPPED:
        logger.info(
            "Generated the demonstration repository history at %s (%s -> %s).",
            demo.path, demo.main_sha, demo.source_sha,
        )
    if isolation["selected_isolation_level"] == "process_restricted":
        logger.warning(
            "Code-execution isolation is limited to the process_restricted tier: "
            "resource limits and a scrubbed environment only. Filesystem writes "
            "outside the workspace are NOT prevented and network egress is NOT "
            "blocked. Install Docker or enable unprivileged user namespaces before "
            "auditing untrusted repositories."
        )
    if not settings.is_live_gitlab:
        logger.warning(
            "GitLab credentials are not configured. The mandatory GitLab Duo Agent "
            "Platform integration cannot run; audits will use the local "
            "deterministic engine and are labelled as demonstration mode."
        )
    yield
    logger.info("shutdown: audit worker pool closing")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AI Engineering Change Auditor",
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        summary=(
            "Evidence-driven audit of software changes: findings backed by "
            "repository evidence, defects reproduced by executing tests, fixes "
            "proposed as reviewable patches, and verification performed by real "
            "test execution in an isolated workspace. A generated patch is never "
            "reported as a fix until tests have actually passed."
        ),
        contact={"name": "AI Engineering Change Auditor", "url": "https://gitlab.com/"},
        license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Audit-Engine"],
    )

    prefix = settings.api_prefix
    app.include_router(system.router, prefix=prefix)
    app.include_router(projects.router, prefix=f"{prefix}/projects")
    app.include_router(audits.router, prefix=f"{prefix}/audits")
    app.include_router(findings.router, prefix=f"{prefix}/findings")
    app.include_router(patches.router, prefix=f"{prefix}/patches")

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, Any]:
        return {
            "name": settings.app_name,
            "version": APP_VERSION,
            "docs": "/api/docs",
            "health": f"{prefix}/health",
            "license": "MIT",
            "notice": (
                "This service never pushes commits, merges merge requests or deploys "
                "patches. Patch application requires explicit human approval and "
                "happens only inside an isolated audit workspace."
            ),
        }

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": "validation_error",
                "detail": "The request body or parameters did not match the required schema.",
                "code": "request_validation",
                "status_code": 422,
                "issues": sanitize_text(str(exc.errors()))[:2000],
                "path": str(request.url.path),
            },
        )

    @app.exception_handler(KeyError)
    async def key_error_handler(request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": "not_found",
                "detail": sanitize_text(str(exc)),
                "code": "resource_not_found",
                "status_code": 404,
                "path": str(request.url.path),
            },
        )

    @app.exception_handler(PermissionError)
    async def permission_handler(request: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "state_transition_rejected",
                "detail": sanitize_text(str(exc)),
                "code": "invalid_state_transition",
                "status_code": 409,
                "path": str(request.url.path),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "internal_error",
                "detail": sanitize_text(f"{type(exc).__name__}: {exc}")[:500],
                "code": "unhandled",
                "status_code": 500,
                "path": str(request.url.path),
            },
        )

    return app


app = create_app()

__all__ = ["APP_VERSION", "app", "create_app"]
