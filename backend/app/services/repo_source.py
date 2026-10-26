"""Concrete RevisionSource implementations.

``GitRevisionSource`` serves the bundled demonstration repository and any local
clone.  ``StaticRevisionSource`` serves revisions fetched over the GitLab REST
API, where the backend already holds file contents and there is no local clone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import gitrepo


def is_test_path(path: str) -> bool:
    name = Path(path).name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or path.startswith("tests/")
        or "/tests/" in f"/{path}"
        or name == "conftest.py"
    )


class GitRevisionSource:
    """Reads exact revisions from a local git repository."""

    kind = "local_git"

    def __init__(self, repo: Path | str):
        self.repo = Path(repo)
        if not self.repo.is_dir():
            raise gitrepo.GitError(f"repository path does not exist: {self.repo}")

    def export(self, sha: str, dest: Path) -> None:
        gitrepo.export_revision(self.repo, sha, dest)

    def file_at(self, sha: str, path: str) -> str:
        return gitrepo.file_at(self.repo, sha, path)

    def test_paths(self, sha: str) -> list[str]:
        return [p for p in gitrepo.list_files(self.repo, sha) if is_test_path(p)]

    def all_python_files(self, sha: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in gitrepo.list_files(self.repo, sha):
            if p.endswith(".py"):
                out[p] = self.file_at(sha, p)
        return out

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "repo": str(self.repo)}


class StaticRevisionSource:
    """Serves revisions from content already fetched (e.g. via the GitLab API)."""

    kind = "static"

    def __init__(
        self,
        files_by_sha: dict[str, dict[str, str]],
        test_paths_by_sha: dict[str, list[str]] | None = None,
    ):
        self.files = files_by_sha
        self.test_paths_by_sha = test_paths_by_sha or {}

    def export(self, sha: str, dest: Path) -> None:
        files = self.files.get(sha)
        if files is None:
            raise KeyError(f"no content recorded for revision {sha[:12]}")
        dest.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def file_at(self, sha: str, path: str) -> str:
        return self.files.get(sha, {}).get(path, "")

    def test_paths(self, sha: str) -> list[str]:
        if sha in self.test_paths_by_sha:
            return self.test_paths_by_sha[sha]
        return [p for p in self.files.get(sha, {}) if is_test_path(p)]

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "revisions": list(self.files),
            "file_counts": {k: len(v) for k, v in self.files.items()},
        }


__all__ = ["GitRevisionSource", "StaticRevisionSource", "is_test_path"]
