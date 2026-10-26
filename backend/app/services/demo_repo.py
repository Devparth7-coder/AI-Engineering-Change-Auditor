"""Bootstrap and verify the local demonstration repository.

The demo fixture needs real git history (two revisions, one of them defective)
because the auditor reads revisions from git objects rather than from the
working tree. That history is a generated artefact, so a fresh clone has none.
This module makes the demo work on a fresh clone without a manual step, and --
importantly -- never pretends the demo is ready when it is not.

Every outcome is reported as an explicit status string that surfaces in
``GET /api/health`` and the startup log. Nothing here fabricates success.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings

logger = logging.getLogger("auditor.demo")

GENERATOR = "make_demo_repo.py"

# Reported to the operator; these strings appear in /api/health.
STATUS_DISABLED = "disabled"
STATUS_READY = "ready"
STATUS_BOOTSTRAPPED = "bootstrapped"
STATUS_MISSING = "missing"
STATUS_BROKEN = "broken"
STATUS_ERROR = "error"


@dataclass
class DemoRepoStatus:
    """Result of ensuring the demonstration repository is usable."""

    status: str = STATUS_DISABLED
    path: str = ""
    detail: str = ""
    main_sha: str = ""
    source_sha: str = ""
    branches: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.status in (STATUS_READY, STATUS_BOOTSTRAPPED)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "path": self.path,
            "detail": self.detail,
            "main_sha": self.main_sha,
            "source_sha": self.source_sha,
            "branches": self.branches,
            "usable": self.usable,
        }


def _generator_path(repo: Path) -> Path:
    return repo / GENERATOR


def _run_generator(repo: Path, *args: str, timeout: int = 120) -> tuple[int, str]:
    script = _generator_path(repo)
    if not script.is_file():
        return 2, f"generator script not found at {script}"
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(repo.parent),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output.strip()


def _read_git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True,
            timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _describe(repo: Path, settings: Settings) -> DemoRepoStatus:
    status = DemoRepoStatus(path=str(repo))
    status.branches = [
        b for b in _read_git(repo, "branch", "--format=%(refname:short)").splitlines() if b
    ]
    status.main_sha = _read_git(repo, "rev-parse", settings.demo_target_branch)[:12]
    status.source_sha = _read_git(repo, "rev-parse", settings.demo_source_branch)[:12]
    return status


_STATUS_CACHE: dict[str, DemoRepoStatus] = {}


def ensure_demo_repo(settings: Settings, *, refresh: bool = False) -> DemoRepoStatus:
    """Make the demo repository usable, reporting honestly what happened.

    Idempotent: an already-valid repository is verified and left untouched.
    The result is cached because ``/api/health`` is polled; pass ``refresh=True``
    (as startup does) to re-probe the filesystem.
    """
    repo = Path(settings.demo_repo_path)
    if not refresh:
        cached = _STATUS_CACHE.get(str(repo))
        if cached is not None and cached.status != STATUS_ERROR:
            return cached
    result = _ensure_demo_repo(settings, repo)
    _STATUS_CACHE[str(repo)] = result
    return result


def _ensure_demo_repo(settings: Settings, repo: Path) -> DemoRepoStatus:
    if not settings.demo_enabled:
        return DemoRepoStatus(status=STATUS_DISABLED, path=str(repo),
                              detail="DEMO_ENABLED is false")
    if not repo.is_dir():
        return DemoRepoStatus(
            status=STATUS_MISSING, path=str(repo),
            detail=f"demonstration repository directory does not exist at {repo}",
        )
    if not _generator_path(repo).is_file():
        return DemoRepoStatus(
            status=STATUS_MISSING, path=str(repo),
            detail=f"{GENERATOR} is absent, so the demo history cannot be built",
        )

    if not (repo / ".git").is_dir():
        code, output = _run_generator(repo)
        if code != 0:
            logger.error("demo bootstrap failed (exit %s): %s", code, output[-500:])
            return DemoRepoStatus(
                status=STATUS_ERROR, path=str(repo),
                detail=f"{GENERATOR} exited {code}: {output[-300:]}",
            )
        status = _describe(repo, settings)
        status.status = STATUS_BOOTSTRAPPED
        status.detail = (
            "git history was generated on startup; a fresh clone contains the "
            "fixture sources but not the generated .git directory"
        )
        logger.info("demo repository bootstrapped at %s", repo)
        return status

    # Already present: verify rather than assume.
    code, output = _run_generator(repo, "--check")
    status = _describe(repo, settings)
    if code != 0:
        status.status = STATUS_BROKEN
        status.detail = f"fixture verification failed: {output[-300:]}"
        logger.warning("demo repository present but invalid: %s", output[-300:])
        return status
    status.status = STATUS_READY
    status.detail = "fixture verified against both revisions"
    return status


__all__ = [
    "DemoRepoStatus",
    "STATUS_BOOTSTRAPPED",
    "STATUS_BROKEN",
    "STATUS_DISABLED",
    "STATUS_ERROR",
    "STATUS_MISSING",
    "STATUS_READY",
    "ensure_demo_repo",
]
