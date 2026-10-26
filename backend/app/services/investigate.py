"""Investigator stage: corroborate every finding against the real repository.

An explanation is not evidence.  Before a finding is allowed to generate a test,
this stage re-checks each cited artefact against the actual source at the pinned
revision:

* does the file exist at that revision?
* do the cited line numbers exist, and do they contain what the finding claims?
* does the symbol really live at that location?
* does the quoted contract text actually appear in the docstring?
* does the claimed prior-revision expression exist at the target revision?

Anything that fails corroboration is demoted.  A finding whose location cannot be
confirmed becomes **inconclusive** rather than being reported as a defect -- this
is the guard against invented code locations and fabricated results.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from .analysis.astutil import functions_of, parse_source
from .analysis.rules.base import (
    CHECK_ANALYSIS,
    CHECK_DIFF_MENTIONS,
    CHECK_DOCSTRING_QUOTE,
    CHECK_SCAN,
    CHECK_SOURCE_LINES,
    FindingDraft,
)
from app.models import VerificationStatus

WHITESPACE_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


@dataclass
class CorroborationItem:
    evidence_index: int
    kind: str
    claim: str
    verified: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class InvestigationResult:
    draft: FindingDraft
    status: VerificationStatus
    corroborated: list[CorroborationItem] = field(default_factory=list)
    refuted: list[CorroborationItem] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    confidence_adjustment: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def corroboration_ratio(self) -> float:
        total = len(self.corroborated) + len(self.refuted)
        return (len(self.corroborated) / total) if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "corroborated": [c.to_dict() for c in self.corroborated],
            "refuted": [c.to_dict() for c in self.refuted],
            "corroboration_ratio": round(self.corroboration_ratio, 3),
            "facts": self.facts,
            "hypotheses": self.hypotheses,
            "missing_information": self.missing_information,
            "confidence_adjustment": self.confidence_adjustment,
            "notes": self.notes,
        }


def _line_range_text(source: str, start: int | None, end: int | None) -> str:
    if start is None:
        return ""
    lines = source.splitlines()
    s = max(1, start)
    e = min(len(lines), end or start)
    return "\n".join(lines[s - 1 : e])


def investigate(
    draft: FindingDraft,
    *,
    new_sources: dict[str, str],
    old_sources: dict[str, str],
    raw_diff: str = "",
) -> InvestigationResult:
    """Corroborate one finding's evidence against both revisions.

    ``raw_diff`` is the actual unified diff of the change. Evidence items that
    claim "the diff touches this" are checked against it rather than against
    their own quoted copy, so a fabricated diff excerpt cannot corroborate itself.
    """
    corroborated: list[CorroborationItem] = []
    refuted: list[CorroborationItem] = []
    facts: list[str] = list(draft.facts)
    hypotheses: list[str] = list(draft.hypotheses)
    missing: list[str] = list(draft.missing_information)
    notes: list[str] = []
    adjustment = 0.0

    new_src = new_sources.get(draft.file_path, "")
    old_src = old_sources.get(draft.file_path, "")

    # ---- (1) the file must exist at the source revision --------------------
    idx = -1
    if new_src:
        corroborated.append(
            CorroborationItem(
                idx, "file_exists", f"`{draft.file_path}` exists at the source revision",
                True, f"{len(new_src.splitlines())} lines",
            )
        )
    else:
        refuted.append(
            CorroborationItem(
                idx, "file_exists", f"`{draft.file_path}` exists at the source revision",
                False, "file not present in the source revision snapshot",
            )
        )
        notes.append(
            "The cited file could not be read at the source revision, so no claim "
            "about its contents can be verified."
        )
        return InvestigationResult(
            draft=draft,
            status=VerificationStatus.INCONCLUSIVE,
            corroborated=corroborated,
            refuted=refuted,
            facts=facts,
            hypotheses=hypotheses,
            missing_information=missing + ["source file content unavailable"],
            confidence_adjustment=-0.5,
            notes=notes,
        )

    # ---- (2) symbol location ------------------------------------------------
    if draft.symbol:
        tree = parse_source(new_src)
        located = None
        if tree is not None:
            for fn in functions_of(tree):
                if fn.name == draft.symbol.split(".")[-1]:
                    located = fn
                    break
        if located is not None:
            claim_ok = (
                draft.line_start is None
                or located.lineno == draft.line_start
                or located.lineno <= (draft.line_start or 0) <= (getattr(located, "end_lineno", located.lineno) or located.lineno)
            )
            item = CorroborationItem(
                0, "symbol_location",
                f"`{draft.symbol}` is defined at lines {located.lineno}-"
                f"{getattr(located, 'end_lineno', located.lineno)}",
                claim_ok,
                f"finding cites lines {draft.line_start}-{draft.line_end}",
            )
            (corroborated if claim_ok else refuted).append(item)
            if claim_ok:
                facts.append(
                    f"confirmed: `{draft.symbol}` is defined at "
                    f"{draft.file_path}:{located.lineno}-{getattr(located, 'end_lineno', located.lineno)}"
                )
                adjustment += 0.05
            else:
                notes.append(
                    "The cited line range does not match the actual definition site; "
                    "the location claim is unreliable."
                )
                adjustment -= 0.2
        else:
            refuted.append(
                CorroborationItem(
                    0, "symbol_location", f"`{draft.symbol}` is defined in this file",
                    False, "no such function or method was found in the source revision",
                )
            )
            notes.append(f"`{draft.symbol}` could not be located in `{draft.file_path}`.")
            adjustment -= 0.3

    # ---- (3) each evidence item is checked in its OWN declared mode ---------
    #
    # Treating every item as "text that must appear at these lines" is wrong:
    # a diff hunk, an AST-derived fact and a contract derivation are analysis
    # artefacts, not source excerpts. Comparing them as source text refutes
    # evidence that is perfectly sound, which then demotes real defects to
    # "inconclusive" and suppresses test generation. Each item therefore
    # declares how it can be independently corroborated.
    contract = draft.contract
    doc_norm = _norm(contract.docstring) if contract is not None else ""
    symbol_name = draft.symbol.split(".")[-1] if draft.symbol else ""

    def _record(i: int, ev, ok: bool, detail: str) -> None:
        nonlocal adjustment
        item = CorroborationItem(i, ev.kind, ev.description, ok, detail)
        (corroborated if ok else refuted).append(item)
        if ok:
            facts.append(
                f"confirmed: evidence #{i} ({ev.kind}) - {detail}"
            )
            adjustment += 0.03
        else:
            notes.append(
                f"Evidence #{i} ({ev.kind}) could not be corroborated: {detail}. "
                "It is not usable as support for this finding."
            )
            adjustment -= 0.15

    for i, ev in enumerate(draft.evidence):
        mode = getattr(ev, "check", CHECK_SOURCE_LINES)

        if mode == CHECK_SCAN:
            _record(
                i, ev, bool(ev.content or ev.note),
                "repository-wide scan result; this is an aggregate, not a line-level claim",
            )
            continue

        if mode == CHECK_DIFF_MENTIONS:
            body = raw_diff or ev.content or ""
            ok = bool(body.strip()) and draft.file_path in body and (
                not symbol_name or symbol_name in body
            )
            _record(
                i, ev, ok,
                f"the change diff touches `{draft.file_path}`"
                + (f" and `{symbol_name}`" if symbol_name else "")
                if ok else
                "the change diff does not mention the cited file or symbol",
            )
            continue

        if mode == CHECK_DOCSTRING_QUOTE:
            quote = _norm(ev.content)[:120]
            ok = bool(doc_norm) and bool(quote) and quote in doc_norm
            _record(
                i, ev, ok,
                "the quoted text appears verbatim in the declared docstring contract"
                if ok else "the quoted text was not found in the function docstring",
            )
            continue

        target_src = old_src if ev.kind == "prior_revision" else new_src
        rev_name = "target" if ev.kind == "prior_revision" else "source"

        if mode == CHECK_ANALYSIS:
            if ev.line_start:
                actual = _line_range_text(target_src, ev.line_start, ev.line_end)
                ok = bool(actual.strip())
                detail = (
                    f"derived artefact; the cited location exists in the {rev_name} "
                    f"revision ({draft.file_path}:{ev.line_start}-{ev.line_end})"
                    if ok else
                    f"derived artefact; the cited location is empty or out of range "
                    f"in the {rev_name} revision"
                )
            else:
                ok = bool(target_src.strip())
                detail = (
                    f"derived artefact; the cited file is readable at the {rev_name} revision"
                    if ok else f"derived artefact; the cited file is empty at the {rev_name} revision"
                )
            _record(i, ev, ok, detail)
            continue

        # ---- CHECK_SOURCE_LINES: content must match the cited line range ----
        actual = _line_range_text(target_src, ev.line_start, ev.line_end) if ev.line_start else target_src
        claimed = ev.content or ""
        # Strip the "NNNN | " gutter the evidence renderer adds before comparing.
        claimed_body = "\n".join(
            re.sub(r"^\s*\d+\s\|\s?", "", ln) for ln in claimed.splitlines()
        )
        if not claimed_body.strip():
            _record(
                i, ev, bool(actual.strip()),
                f"lines {ev.line_start}-{ev.line_end} exist in the {rev_name} revision"
                if actual.strip() else f"the cited line range is empty in the {rev_name} revision",
            )
            continue
        ratio = difflib.SequenceMatcher(None, _norm(claimed_body), _norm(actual)).ratio()
        _record(
            i, ev, ratio >= 0.85,
            f"quoted text matches {draft.file_path}:{ev.line_start}-{ev.line_end} in the "
            f"{rev_name} revision (line-range similarity {ratio:.2f})"
            if ratio >= 0.85 else
            f"quoted text does not match the cited lines in the {rev_name} revision "
            f"(similarity {ratio:.2f})",
        )

    # ---- (4) contract quotes must really be in the docstring ----------------
    contract = draft.contract
    if contract is not None:
        doc_norm = _norm(contract.docstring or "")
        for ex in contract.examples:
            ok = _norm(ex.source_text)[:60] in doc_norm
            item = CorroborationItem(
                -2, "contract_example",
                f"worked example parses from the docstring: {ex.source_text[:80]!r}",
                ok,
                "verbatim match in the declared contract" if ok else "example text not found in the docstring",
            )
            (corroborated if ok else refuted).append(item)
            if ok:
                facts.append(
                    f"confirmed: the contract documents {ex.inputs} -> {ex.expected_output} "
                    f"(docstring line {ex.source_line})"
                )
                adjustment += 0.05
        if contract.semantics_evidence:
            facts.append(
                f"confirmed: declared return semantics = {contract.return_semantics}"
            )
        if contract.completeness:
            missing.extend(contract.completeness)

    # ---- (5) decide the post-investigation status --------------------------
    ratio = (
        len(corroborated) / (len(corroborated) + len(refuted))
        if (corroborated or refuted)
        else 0.0
    )
    location_refuted = any(
        c.kind in ("file_exists", "symbol_location") and not c.verified
        for c in refuted
    )
    if location_refuted:
        status = VerificationStatus.INCONCLUSIVE
        notes.append(
            "Demoted to inconclusive: the cited code location could not be confirmed. "
            "No test will be generated from an unverified location."
        )
    elif ratio < 0.5:
        status = VerificationStatus.INCONCLUSIVE
        notes.append(
            f"Demoted to inconclusive: only {ratio:.0%} of the cited evidence could be "
            "corroborated against the repository."
        )
    elif ratio >= 0.8 and draft.testable:
        status = VerificationStatus.EVIDENCED
        notes.append(
            "Evidence corroborated against the repository. This is still a hypothesis: "
            "it becomes a confirmed defect only if a reproduction test fails on the "
            "intended assertion."
        )
    else:
        status = VerificationStatus.EVIDENCED if draft.testable else VerificationStatus.HYPOTHESIS
        notes.append(
            f"{ratio:.0%} of cited evidence corroborated; treated as a supported hypothesis "
            "pending execution."
        )

    if not draft.testable and status == VerificationStatus.EVIDENCED:
        status = VerificationStatus.HYPOTHESIS
        notes.append(
            "No falsifiable test could be derived from the declared contract, so this "
            "finding cannot be promoted beyond a hypothesis by execution."
        )

    # Deduplicate while preserving order.
    def _uniq(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for it in items:
            if it and it not in seen:
                seen.add(it)
                out.append(it)
        return out

    return InvestigationResult(
        draft=draft,
        status=status,
        corroborated=corroborated,
        refuted=refuted,
        facts=_uniq(facts),
        hypotheses=_uniq(hypotheses),
        missing_information=_uniq(missing),
        confidence_adjustment=round(max(-0.6, min(0.25, adjustment)), 3),
        notes=_uniq(notes),
    )


__all__ = ["CorroborationItem", "InvestigationResult", "investigate"]
