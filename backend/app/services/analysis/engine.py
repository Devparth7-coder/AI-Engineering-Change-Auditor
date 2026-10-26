"""Change-analysis engine.

Turns a diff plus the surrounding source at both revisions into structured,
evidence-backed :class:`FindingDraft` objects.

Design rules this module enforces:

* **Context, not snippets.** Rules receive the full function at both revisions,
  its docstring contract, and the whole-repo test corpus -- never an isolated
  hunk.
* **Deterministic.** No model calls. The same input always yields the same
  findings, which is what makes the benchmark in ``evaluation/`` reproducible.
* **Honest.** A draft is a hypothesis. Only test execution can promote it.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .astutil import (
    FileDiff,
    diff_from_sources,
    functions_of,
    functions_touching,
    parse_source,
    parse_unified_diff,
)
from .contracts import FunctionContract, extract_contract
from .rules import ALL_RULES, FileContext, FindingDraft, RuleContext
from app.models import EngineKind, Severity

logger = logging.getLogger(__name__)

PYTHON_SUFFIXES = (".py",)
TEST_FILE_HINTS = ("test_", "_test.py", "/tests/", "conftest.py")

SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


@dataclass
class ChangeBundle:
    """Everything known about one change, at both revisions."""

    source_commit_sha: str = ""
    target_commit_sha: str = ""
    source_branch: str = ""
    target_branch: str = ""
    # path -> full file content at each revision ("" when absent)
    old_sources: dict[str, str] = field(default_factory=dict)
    new_sources: dict[str, str] = field(default_factory=dict)
    # optional pre-computed unified diff (e.g. from the GitLab MR changes API)
    raw_diff: str = ""
    test_sources: dict[str, str] = field(default_factory=dict)
    repo_root: str = ""
    mode: str = "local_demo"

    @property
    def changed_paths(self) -> list[str]:
        paths = set(self.old_sources) | set(self.new_sources)
        return sorted(paths)


def _is_test_file(path: str) -> bool:
    name = Path(path).name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or "/tests/" in f"/{path}"
        or path.startswith("tests/")
        or name == "conftest.py"
    )


def _contracts_for(source: str, path: str) -> dict[str, FunctionContract]:
    tree = parse_source(source)
    if tree is None:
        return {}
    lines = source.splitlines()
    out: dict[str, FunctionContract] = {}
    for fn in functions_of(tree):
        out.setdefault(fn.name, extract_contract(fn, path, lines))
    return out


def _functions_by_name(tree: ast.Module | None) -> dict[str, Any]:
    if tree is None:
        return {}
    out: dict[str, Any] = {}
    for fn in functions_of(tree):
        out.setdefault(fn.name, fn)
    return out


def build_context(bundle: ChangeBundle) -> RuleContext:
    """Assemble the repository-wide context handed to every rule."""
    diffs: dict[str, FileDiff] = {}
    if bundle.raw_diff:
        for fd in parse_unified_diff(bundle.raw_diff):
            diffs[fd.path] = fd

    files: list[FileContext] = []
    for path in bundle.changed_paths:
        old_src = bundle.old_sources.get(path, "")
        new_src = bundle.new_sources.get(path, "")
        fd = diffs.get(path) or diff_from_sources(path, old_src, new_src)
        old_tree = parse_source(old_src) if old_src else None
        new_tree = parse_source(new_src) if new_src else None
        changed_fns = (
            functions_touching(new_tree, fd.changed_new_lines) if new_tree is not None else []
        )
        files.append(
            FileContext(
                path=path,
                diff=fd,
                old_source=old_src,
                new_source=new_src,
                old_tree=old_tree,
                new_tree=new_tree,
                changed_functions=changed_fns,
                old_functions=_functions_by_name(old_tree),
                new_functions=_functions_by_name(new_tree),
                contracts=_contracts_for(new_src, path),
                old_contracts=_contracts_for(old_src, path),
            )
        )

    test_sources = dict(bundle.test_sources)
    if not test_sources:
        # Fall back to treating any changed test file as part of the corpus.
        test_sources = {p: s for p, s in bundle.new_sources.items() if _is_test_file(p)}

    return RuleContext(
        files=files,
        repo_root=bundle.repo_root,
        all_sources_new=dict(bundle.new_sources),
        all_sources_old=dict(bundle.old_sources),
        test_file_paths=sorted(test_sources),
        test_sources=test_sources,
        source_commit_sha=bundle.source_commit_sha,
        target_commit_sha=bundle.target_commit_sha,
    )


@dataclass
class AnalysisResult:
    findings: list[FindingDraft]
    rules_run: list[str]
    rule_errors: dict[str, str]
    files_analyzed: int
    functions_analyzed: int
    engine: EngineKind = EngineKind.LOCAL_DETERMINISTIC

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules_run": self.rules_run,
            "rule_errors": self.rule_errors,
            "files_analyzed": self.files_analyzed,
            "functions_analyzed": self.functions_analyzed,
            "engine": self.engine.value,
            "finding_count": len(self.findings),
        }


def analyze(bundle: ChangeBundle, *, max_findings: int = 25) -> AnalysisResult:
    """Run every rule over the change and return de-duplicated drafts."""
    ctx = build_context(bundle)
    drafts: list[FindingDraft] = []
    errors: dict[str, str] = {}
    rules_run: list[str] = []

    for rule_id, _desc, fn in ALL_RULES:
        rules_run.append(rule_id)
        try:
            produced = fn(ctx) or []
        except Exception as exc:  # noqa: BLE001 - one bad rule must not kill the audit
            logger.exception("rule %s failed", rule_id)
            errors[rule_id] = f"{type(exc).__name__}: {exc}"
            continue
        for d in produced:
            d.engine = EngineKind.LOCAL_DETERMINISTIC
        drafts.extend(produced)

    drafts = dedupe(drafts)
    drafts.sort(
        key=lambda d: (
            SEVERITY_ORDER.get(d.severity, 9),
            -float(d.confidence.get("signal_agreement_score", 0)),
            d.file_path,
            d.line_start or 0,
        )
    )
    if len(drafts) > max_findings:
        dropped = drafts[max_findings:]
        drafts = drafts[:max_findings]
        logger.info("capped findings at %d (dropped %d)", max_findings, len(dropped))

    return AnalysisResult(
        findings=drafts,
        rules_run=rules_run,
        rule_errors=errors,
        files_analyzed=len(ctx.files),
        functions_analyzed=sum(len(fc.changed_functions) for fc in ctx.files),
    )


def dedupe(drafts: list[FindingDraft]) -> list[FindingDraft]:
    """Collapse duplicates: same rule + file + symbol + line range."""
    seen: dict[tuple[str, str, str, int | None], FindingDraft] = {}
    for d in drafts:
        key = (d.rule_id, d.file_path, d.symbol, d.line_start)
        existing = seen.get(key)
        if existing is None:
            seen[key] = d
            continue
        # Keep the higher-confidence draft, merging evidence.
        if float(d.confidence.get("signal_agreement_score", 0)) > float(
            existing.confidence.get("signal_agreement_score", 0)
        ):
            d.evidence = existing.evidence + d.evidence
            seen[key] = d
        else:
            existing.evidence = existing.evidence + d.evidence
    return list(seen.values())


def load_bundle_from_disk(
    repo_root: Path,
    *,
    changed_paths: list[str],
    test_paths: list[str] | None = None,
    source_commit_sha: str = "",
    target_commit_sha: str = "",
) -> ChangeBundle:
    """Build a bundle by reading the working tree (used by local demo mode)."""
    new_sources: dict[str, str] = {}
    for rel in changed_paths:
        p = repo_root / rel
        if p.is_file():
            try:
                new_sources[rel] = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    tests: dict[str, str] = {}
    for rel in test_paths or []:
        p = repo_root / rel
        if p.is_file():
            try:
                tests[rel] = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return ChangeBundle(
        source_commit_sha=source_commit_sha,
        target_commit_sha=target_commit_sha,
        new_sources=new_sources,
        old_sources={},
        test_sources=tests,
        repo_root=str(repo_root),
    )


__all__ = [
    "AnalysisResult",
    "ChangeBundle",
    "analyze",
    "build_context",
    "dedupe",
    "load_bundle_from_disk",
]
