"""Reproduction-test generation.

Converts an evidence-backed finding into a focused, executable pytest file.

The governing rule: **expectations come from the declared contract, never from
the implementation under suspicion.**  Each generated assertion records where its
expected value came from, so a reader can audit the test itself:

``documented_example``        parsed verbatim from the docstring's worked example
``derived_from_semantics``    implied by the documented return semantics
                              (net result => rate 0 leaves the base unchanged)
``declared_error_contract``   taken from the docstring's ``Raises`` clause
``target_revision_reference`` the pre-change behaviour, used only for
                              differential probes and labelled as such

If none of these is available the generator refuses to invent an expectation and
returns ``testable=False`` so the finding is reported **inconclusive**.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analysis.contracts import FunctionContract
from .analysis.rules.base import FindingDraft
from app.models import FindingCategory

SAFE_NAME_RE = re.compile(r"[^0-9a-zA-Z_]+")


@dataclass
class Expectation:
    """One assertion, with the provenance of its expected value."""

    description: str
    call: str
    expected: str
    provenance: str
    source_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class GeneratedTest:
    finding_public_id: str
    file_name: str
    content: str
    conftest_content: str
    intended_node_ids: list[str]
    expectations: list[Expectation]
    rationale: str
    module_import: str
    symbol: str
    testable: bool = True
    refusal_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_public_id": self.finding_public_id,
            "file_name": self.file_name,
            "intended_node_ids": self.intended_node_ids,
            "expectations": [e.to_dict() for e in self.expectations],
            "rationale": self.rationale,
            "module_import": self.module_import,
            "symbol": self.symbol,
            "testable": self.testable,
            "refusal_reason": self.refusal_reason,
        }


def module_name_for(file_path: str, repo_files: list[str] | None = None) -> str:
    """Derive an importable module name from a repository-relative path."""
    p = Path(file_path)
    parts = list(p.with_suffix("").parts)
    # Drop common source roots so `src/pkg/mod.py` -> `pkg.mod`.
    while parts and parts[0] in {"src", "source", "lib", "app", "."}:
        parts = parts[1:]
    # Drop leading test dirs (not expected here, but be safe).
    while parts and parts[0] in {"tests", "test"}:
        parts = parts[1:]
    if not parts:
        parts = [p.stem]
    return ".".join(parts)


def _fmt(value: float) -> str:
    if float(value).is_integer():
        return f"{value:.1f}"
    return repr(value)


def _safe(name: str) -> str:
    return SAFE_NAME_RE.sub("_", name).strip("_").lower() or "case"


def _refusal(draft: FindingDraft, reason: str) -> GeneratedTest:
    return GeneratedTest(
        finding_public_id="",
        file_name="",
        content="",
        conftest_content="",
        intended_node_ids=[],
        expectations=[],
        rationale="",
        module_import="",
        symbol=draft.symbol,
        testable=False,
        refusal_reason=reason,
    )


def build_expectations(draft: FindingDraft) -> list[Expectation]:
    """Derive every assertion this finding can honestly support."""
    contract: FunctionContract | None = draft.contract
    out: list[Expectation] = []
    sym = draft.symbol
    if not sym:
        return out

    # 1. Documented worked examples (highest-value provenance).
    if contract:
        for ex in contract.examples:
            args = ", ".join(f"{k}={_fmt(v)}" for k, v in sorted(ex.inputs.items(), key=lambda kv: kv[0]))
            out.append(
                Expectation(
                    description=f"Documented contract example: {ex.source_text}",
                    call=f"{sym}({args})",
                    expected=_fmt(ex.expected_output),
                    provenance="documented_example",
                    source_ref=(
                        f"{contract.file_path}:{ex.source_line}" if ex.source_line else contract.file_path
                    ),
                )
            )

        # 2. Semantics-derived identities.
        rate_params = [
            p.name for p in contract.params if p.is_fraction or _is_rate_name(p.name)
        ]
        base_params = [p.name for p in contract.params if p.name not in rate_params]
        if contract.return_semantics == "net_remaining" and rate_params and base_params:
            r, b = rate_params[0], base_params[0]
            out.append(
                Expectation(
                    description=(
                        f"A zero {r} must leave {b} unchanged "
                        f"(implied by the declared net-result semantics)."
                    ),
                    call=f"{sym}({b}=250.0, {r}=0.0)",
                    expected="250.0",
                    provenance="derived_from_semantics",
                    source_ref=f"{contract.file_path}:{contract.line_start}",
                )
            )
            out.append(
                Expectation(
                    description=(
                        f"A full ({r}=1.0) adjustment must reduce the result to zero "
                        f"(implied by the declared net-result semantics)."
                    ),
                    call=f"{sym}({b}=250.0, {r}=1.0)",
                    expected="0.0",
                    provenance="derived_from_semantics",
                    source_ref=f"{contract.file_path}:{contract.line_start}",
                )
            )
            out.append(
                Expectation(
                    description=(
                        f"The result must never exceed the input {b} for a non-negative {r}."
                    ),
                    call=f"{sym}({b}=1000.0, {r}=0.35)",
                    expected="650.0",
                    provenance="derived_from_semantics",
                    source_ref=f"{contract.file_path}:{contract.line_start}",
                )
            )
        elif contract.return_semantics == "component_delta" and rate_params and base_params:
            r, b = rate_params[0], base_params[0]
            out.append(
                Expectation(
                    description=f"A zero {r} must produce a zero adjustment.",
                    call=f"{sym}({b}=250.0, {r}=0.0)",
                    expected="0.0",
                    provenance="derived_from_semantics",
                    source_ref=f"{contract.file_path}:{contract.line_start}",
                )
            )

        # 3. Declared error contract.
        if contract.raises:
            exc = contract.raises[0]
            doc = (contract.docstring or "").lower()
            if "negative" in doc and base_params:
                b = base_params[0]
                others = ", ".join(
                    f"{p.name}={_fmt(0.1) if p.is_fraction or _is_rate_name(p.name) else '1.0'}"
                    for p in contract.params
                    if p.name != b
                )
                suffix = f", {others}" if others else ""
                out.append(
                    Expectation(
                        description=f"Declared `Raises: {exc}` for a negative {b}.",
                        call=f"{sym}({b}=-1.0{suffix})",
                        expected=f"raises:{exc}",
                        provenance="declared_error_contract",
                        source_ref=f"{contract.file_path}:{contract.line_start}",
                    )
                )
            if any(p.is_fraction or _is_rate_name(p.name) for p in contract.params) and base_params:
                r = next(
                    p.name for p in contract.params if p.is_fraction or _is_rate_name(p.name)
                )
                b = base_params[0]
                out.append(
                    Expectation(
                        description=f"Declared `Raises: {exc}` for an out-of-domain {r}.",
                        call=f"{sym}({b}=100.0, {r}=1.5)",
                        expected=f"raises:{exc}",
                        provenance="declared_error_contract",
                        source_ref=f"{contract.file_path}:{contract.line_start}",
                    )
                )

    # 4. Category-specific structural probes that need no numeric contract.
    if draft.category == FindingCategory.INCORRECT_ASSUMPTION and "mutable default" in draft.title:
        out.append(
            Expectation(
                description="Two calls must not share mutable default state.",
                call=f"__mutable_default_probe__({sym})",
                expected="independent",
                provenance="derived_from_semantics",
                source_ref=draft.file_path,
            )
        )
    return out


def _is_rate_name(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in ("rate", "pct", "percent", "ratio", "discount", "factor"))


def _render_test_body(exp: Expectation, idx: int, symbol: str) -> str:
    fn_name = f"test_{_safe(symbol)}_{idx:02d}_{_safe(exp.provenance)}"
    doc = exp.description.replace('"""', "'''")
    header = (
        f"def {fn_name}():\n"
        f'    """{doc}\n\n'
        f"    Expected value provenance: {exp.provenance}\n"
        f"    Source: {exp.source_ref}\n"
        f'    """\n'
    )
    if exp.expected.startswith("raises:"):
        exc = exp.expected.split(":", 1)[1]
        return (
            header
            + f"    with pytest.raises({exc}):\n"
            + f"        {exp.call}\n"
        )
    if exp.expected == "independent":
        return (
            header
            + "    first = "
            + f"{symbol}()\n"
            + "    if isinstance(first, list):\n"
            + "        first.append('sentinel')\n"
            + "    elif isinstance(first, dict):\n"
            + "        first['sentinel'] = True\n"
            + "    second = "
            + f"{symbol}()\n"
            + "    assert not (\n"
            + "        (isinstance(second, list) and 'sentinel' in second)\n"
            + "        or (isinstance(second, dict) and 'sentinel' in second)\n"
            + "    ), 'mutable default argument is shared between calls'\n"
        )
    return (
        header
        + f"    result = {exp.call}\n"
        + f"    assert result == pytest.approx({exp.expected}), (\n"
        + f'        f"contract requires {exp.expected}, got {{result!r}}"\n'
        + "    )\n"
    )


CONFTEST_TEMPLATE = '''"""Path bootstrap for auditor-generated reproduction tests.

Generated by the AI Engineering Change Auditor. Adds the repository under audit
(and its ``src/`` layout, if present) to ``sys.path`` so the module under test is
importable regardless of how the workspace was assembled.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _candidate in (_ROOT, _ROOT / "src", _ROOT / "repo", _ROOT / "repo" / "src"):
    if _candidate.is_dir():
        _p = str(_candidate)
        if _p not in sys.path:
            sys.path.insert(0, _p)
'''


def generate(draft: FindingDraft, finding_public_id: str, *, repo_files: list[str] | None = None) -> GeneratedTest:
    """Produce the reproduction test for one finding."""
    if not draft.testable:
        return _refusal(
            draft,
            "Finding is not mechanically testable: "
            + (draft.recommended_action or "manual review required"),
        )
    expectations = build_expectations(draft)
    if not expectations:
        return _refusal(
            draft,
            "No expectation could be derived from the declared contract. "
            "Refusing to invent one; the finding stays inconclusive.",
        )

    module_import = module_name_for(draft.file_path, repo_files)
    symbol = draft.symbol or "target"
    test_file = f"test_audit_{_safe(finding_public_id)}_{_safe(symbol)}.py"

    bodies: list[str] = []
    node_ids: list[str] = []
    for idx, exp in enumerate(expectations, start=1):
        body = _render_test_body(exp, idx, symbol)
        bodies.append(body)
        fn_name = body.split("(", 1)[0].replace("def ", "").strip()
        node_ids.append(f"{test_file}::{fn_name}")

    provenance_lines = "\n".join(
        f"#   [{e.provenance}] {e.call} -> {e.expected}   ({e.source_ref})"
        for e in expectations
    )
    content = f'''"""Reproduction test for finding {finding_public_id}.

GENERATED BY THE AI ENGINEERING CHANGE AUDITOR -- DO NOT EDIT BY HAND.

Finding
-------
Rule        : {draft.rule_id}
Category    : {draft.category.value}
Severity    : {draft.severity.value}
Title       : {draft.title}
Location    : {draft.file_path}:{draft.line_start}-{draft.line_end} ({symbol})

Expected behaviour (from the DECLARED CONTRACT, not from the implementation):
    {draft.expected_behavior or "(see per-test provenance below)"}

Suspected behaviour:
    {draft.suspected_behavior or "(see finding description)"}

Assertion provenance
--------------------
{provenance_lines}

This test is expected to FAIL against the original (source) revision if the
finding is a real defect, and to PASS once a correct patch is applied.
"""

import pytest

from {module_import} import {symbol}


{chr(10).join(bodies)}
'''
    rationale = (
        f"{len(expectations)} assertion(s) derived from the declared contract of "
        f"`{symbol}` in `{draft.file_path}`. Expected values come from "
        + ", ".join(sorted({e.provenance for e in expectations}))
        + ". None were read from the implementation under suspicion."
    )
    return GeneratedTest(
        finding_public_id=finding_public_id,
        file_name=test_file,
        content=content,
        conftest_content=CONFTEST_TEMPLATE,
        intended_node_ids=node_ids,
        expectations=expectations,
        rationale=rationale,
        module_import=module_import,
        symbol=symbol,
        testable=True,
    )


__all__ = [
    "CONFTEST_TEMPLATE",
    "Expectation",
    "GeneratedTest",
    "build_expectations",
    "generate",
    "module_name_for",
]
