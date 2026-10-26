"""Local git access: materialise exact revisions and read real diffs.

The auditor always works from *pinned commit SHAs*, never from "whatever is
checked out".  ``git archive`` is used to export a revision so the working copy
stays untouched and no worktree bookkeeping can leak between audits.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, timeout: int = 120, check: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GitError("git binary not available") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


@dataclass
class CommitInfo:
    sha: str
    short_sha: str
    branch: str = ""
    subject: str = ""
    author: str = ""
    date: str = ""


def resolve_sha(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).strip()


def commit_info(repo: Path, ref: str, branch: str = "") -> CommitInfo:
    sha = resolve_sha(repo, ref)
    out = _git(repo, "show", "-s", "--format=%H%x1f%s%x1f%an%x1f%aI", sha).strip()
    parts = out.split("\x1f")
    return CommitInfo(
        sha=parts[0],
        short_sha=parts[0][:12],
        branch=branch,
        subject=parts[1] if len(parts) > 1 else "",
        author=parts[2] if len(parts) > 2 else "",
        date=parts[3] if len(parts) > 3 else "",
    )


def export_revision(repo: Path, sha: str, dest: Path) -> Path:
    """Export a revision's tree into ``dest`` (created if needed)."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", sha],
        capture_output=True,
        timeout=180,
    )
    if archive.returncode != 0:
        raise GitError(
            f"git archive {sha[:12]} failed: {archive.stderr.decode(errors='replace')[:400]}"
        )
    extract = subprocess.run(
        ["tar", "-x", "-C", str(dest)],
        input=archive.stdout,
        capture_output=True,
        timeout=180,
    )
    if extract.returncode != 0:
        raise GitError(
            f"could not extract revision {sha[:12]}: "
            f"{extract.stderr.decode(errors='replace')[:400]}"
        )
    return dest


def file_at(repo: Path, sha: str, path: str) -> str:
    """Content of ``path`` at ``sha``; empty string when absent."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{sha}:{path}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout


def diff_between(repo: Path, base: str, head: str, paths: list[str] | None = None) -> str:
    args = ["diff", base, head]
    if paths:
        args += ["--", *paths]
    return _git(repo, *args, timeout=180)


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    out = _git(repo, "diff", "--name-only", base, head)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def list_files(repo: Path, sha: str, pattern: str = "*") -> list[str]:
    out = _git(repo, "ls-tree", "-r", "--name-only", sha)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def branches(repo: Path) -> list[str]:
    out = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def is_repo(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        _git(path, "rev-parse", "--is-inside-work-tree", check=False).strip() == "true"
        return (path / ".git").exists() or _git(
            path, "rev-parse", "--git-dir", check=False
        ).strip() not in ("", ".git")
    except GitError:
        return False


def merge_base(repo: Path, a: str, b: str) -> str:
    return _git(repo, "merge-base", a, b).strip()


def default_branch(repo: Path) -> str:
    """Resolve the repository's default branch.

    Deliberately does NOT consult ``HEAD``: that reports whichever branch
    happens to be checked out, which made the demo audit invert its own source
    and target branches.  Branch names may contain slashes
    (``feature/refactor-discount``), so only a leading remote prefix is stripped.
    """
    try:
        out = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False).strip()
        if out:
            # "origin/main" -> "main"; strip only the remote segment.
            return out.split("/", 1)[1] if out.count("/") >= 1 and out.split("/", 1)[0] == "origin" else out
    except GitError:
        pass
    existing = set(branches(repo))
    for name in ("main", "master", "trunk", "develop"):
        if name in existing:
            return name
    try:
        out = _git(repo, "config", "--get", "init.defaultBranch", check=False).strip()
        if out and out in existing:
            return out
    except GitError:
        pass
    return sorted(existing)[0] if existing else "main"


def repo_summary(repo: Path) -> dict[str, Any]:
    return {
        "path": str(repo),
        "default_branch": default_branch(repo),
        "branches": branches(repo),
        "head": commit_info(repo, "HEAD").sha,
    }


__all__ = [
    "CommitInfo",
    "GitError",
    "branches",
    "changed_files",
    "commit_info",
    "default_branch",
    "diff_between",
    "export_revision",
    "file_at",
    "is_repo",
    "list_files",
    "merge_base",
    "repo_summary",
    "resolve_sha",
]
