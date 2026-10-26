"""Rule framework: shared types for evidence-backed findings.

Every rule produces a :class:`FindingDraft` that separates three things the
product must never blur together:

``facts``        things observed directly in the repository or by execution,
``hypotheses``   interpretations that still need verification,
``missing_information``  what would be needed to raise or refute confidence.

A draft is only a *hypothesis* until a reproduction test runs.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..astutil import FileDiff
from ..contracts import FunctionContract
from app.models import EngineKind, FindingCategory, Severity


# How the investigator should corroborate an evidence item. Treating every item
# as "text that must appear at these lines" wrongly refutes diff hunks, AST
# facts and contract derivations, which are analysis artefacts rather than
# source excerpts.
CHECK_SOURCE_LINES = "source_lines"      # content must match the cited line range
CHECK_DOCSTRING_QUOTE = "docstring_quote"  # content must appear in the docstring
CHECK_DIFF_MENTIONS = "diff_mentions"    # diff must mention the file and symbol
CHECK_ANALYSIS = "analysis"              # derived artefact: cited range must exist
CHECK_SCAN = "repo_scan"                 # repository-wide scan result
EVIDENCE_CHECKS = (
    CHECK_SOURCE_LINES,
    CHECK_DOCSTRING_QUOTE,
    CHECK_DIFF_MENTIONS,
    CHECK_ANALYSIS,
    CHECK_SCAN,
)


@dataclass
class Evidence:
    """A single, checkable piece of support for a finding."""

    kind: str  # diff_hunk|source_excerpt|prior_revision|contract_quote|ast_fact|repo_scan|execution
    description: str
    file_path: str = ""
    line_start: int | None = None
    line_end: int | None = None
    content: str = ""
    note: str = ""
    # Declares how this item can be independently corroborated.
    check: str = CHECK_SOURCE_LINES

    def __post_init__(self) -> None:
        if self.check not in EVIDENCE_CHECKS:
            raise ValueError(f"unknown evidence check mode: {self.check!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "description": self.description,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "content": self.content,
            "note": self.note,
            "check": self.check,
        }


@dataclass
class RepairHint:
    """Machine-usable guidance for the repair designer. Not a patch itself."""

    strategy: str
    file_path: str
    symbol: str
    replacement_expr: str = ""
    original_expr: str = ""
    rationale: str = ""
    minimal: bool = True

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class FindingDraft:
    rule_id: str
    category: FindingCategory
    severity: Severity
    title: str
    description: str
    file_path: str
    symbol: str = ""
    line_start: int | None = None
    line_end: int | None = None
    evidence: list[Evidence] = field(default_factory=list)
    expected_behavior: str = ""
    suspected_behavior: str = ""
    confidence: dict[str, Any] = field(default_factory=dict)
    recommended_action: str = ""
    facts: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    contract: FunctionContract | None = None
    repair_hint: RepairHint | None = None
    testable: bool = True
    engine: EngineKind = EngineKind.LOCAL_DETERMINISTIC

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category.value,
            "severity": self.severity.value,
            "title": self.title,
            "description": self.description,
            "file_path": self.file_path,
            "symbol": self.symbol,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "evidence": [e.to_dict() for e in self.evidence],
            "expected_behavior": self.expected_behavior,
            "suspected_behavior": self.suspected_behavior,
            "confidence": self.confidence,
            "recommended_action": self.recommended_action,
            "facts": self.facts,
            "hypotheses": self.hypotheses,
            "missing_information": self.missing_information,
            "contract": self.contract.to_dict() if self.contract else None,
            "repair_hint": self.repair_hint.to_dict() if self.repair_hint else None,
            "testable": self.testable,
            "engine": self.engine.value,
        }


@dataclass
class FileContext:
    """Everything a rule may need about one changed file."""

    path: str
    diff: FileDiff
    old_source: str
    new_source: str
    old_tree: ast.Module | None
    new_tree: ast.Module | None
    changed_functions: list[ast.FunctionDef | ast.AsyncFunctionDef]
    old_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    new_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    contracts: dict[str, FunctionContract]
    old_contracts: dict[str, FunctionContract]


@dataclass
class RuleContext:
    """Repository-wide context handed to every rule."""

    files: list[FileContext]
    repo_root: str
    all_sources_new: dict[str, str] = field(default_factory=dict)
    all_sources_old: dict[str, str] = field(default_factory=dict)
    test_file_paths: list[str] = field(default_factory=list)
    test_sources: dict[str, str] = field(default_factory=dict)
    source_commit_sha: str = ""
    target_commit_sha: str = ""

    def file(self, path: str) -> FileContext | None:
        for fc in self.files:
            if fc.path == path:
                return fc
        return None


class Rule(Protocol):
    rule_id: str
    description: str

    def evaluate(self, ctx: RuleContext) -> list[FindingDraft]: ...


def confidence(
    level: str,
    score: float,
    basis: list[str],
    *,
    calibrated: bool = False,
    note: str = "",
) -> dict[str, Any]:
    """Build a confidence block that is honest about what the number means.

    The score is a *signal-agreement* measure: how many independent pieces of
    repository evidence point the same way.  It is NOT a probability of defect,
    and unless ``calibrated`` is True it has not been fitted against a labelled
    corpus.  The report repeats this caveat.
    """
    return {
        "level": level,
        "signal_agreement_score": round(max(0.0, min(1.0, score)), 3),
        "basis": basis,
        "calibrated_on_labelled_corpus": calibrated,
        "interpretation": (
            "signal_agreement_score counts independent repository signals that "
            "agree. It is not a probability that a defect exists; verification "
            "status, not this score, determines whether a defect is confirmed."
        ),
        "note": note
        or (
            "Uncalibrated: no labelled corpus was used to fit this score. "
            "Treat as a triage ordering signal only."
        ),
    }
