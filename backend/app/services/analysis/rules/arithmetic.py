"""R-ARITH-001 -- return-value semantics contradict the declared contract.

This is the rule that catches the bundled acceptance-test defect: a function
documented to return the **final/net** amount whose implementation returns only
the **component** amount (``price * discount_rate`` instead of
``price * (1 - discount_rate)``).

Detection is purely structural + contract-driven.  No model is involved, and the
expected value used later for test generation comes from the docstring's
declared contract -- never from the body of the function under suspicion.
"""

from __future__ import annotations

import ast
from typing import Any

from ..astutil import (
    function_at_line,
    lines_of,
    normalize_expr,
    return_expressions,
    source_segment,
)
from ..contracts import FunctionContract
from .base import (
    CHECK_ANALYSIS,
    CHECK_DIFF_MENTIONS,
    CHECK_DOCSTRING_QUOTE,
    CHECK_SOURCE_LINES,
    Evidence,
    FindingDraft,
    RepairHint,
    RuleContext,
    confidence,
)
from app.models import FindingCategory, Severity

RULE_ID = "R-ARITH-001"
DESCRIPTION = "Returned arithmetic contradicts the declared net/component contract"

_RATE_HINTS = ("rate", "pct", "percent", "discount", "ratio", "factor", "margin")
_BASE_HINTS = ("price", "amount", "cost", "total", "value", "base", "sum", "balance")


def _is_one(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == 1


def _classify_arithmetic(
    expr: ast.expr, rate_params: set[str], base_params: set[str]
) -> tuple[str, list[str], dict[str, Any]]:
    """Classify a return expression as ``net``, ``component`` or ``unknown``."""
    reasons: list[str] = []
    has_complement = False
    has_sub_from_base = False
    mults: list[ast.BinOp] = []
    subs: list[ast.BinOp] = []

    for node in ast.walk(expr):
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Mult):
                mults.append(node)
            elif isinstance(node.op, (ast.Sub, ast.Add)):
                subs.append(node)
                if isinstance(node.op, ast.Sub):
                    if _is_one(node.left):
                        has_complement = True
                        reasons.append("expression contains a (1 - x) complement factor")
                    left_names = _leaf_names(node.left)
                    if left_names & base_params:
                        has_sub_from_base = True
                        reasons.append(
                            f"expression subtracts from the base value ({normalize_expr(node.left)})"
                        )

    names = _leaf_names(expr)
    uses_rate = bool(names & rate_params)
    top_is_product = isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Mult)

    info = {
        "normalized": normalize_expr(expr),
        "has_complement": has_complement,
        "has_subtraction_from_base": has_sub_from_base,
        "top_level_is_product": top_is_product,
        "multiplications": len(mults),
        "subtractions": len([s for s in subs if isinstance(s.op, ast.Sub)]),
        "references_rate_param": uses_rate,
        "referenced_params": sorted(names),
    }

    if has_complement or has_sub_from_base:
        return "net", reasons, info
    if top_is_product and uses_rate and not subs:
        reasons.append(
            "expression is a bare product of the base value and the rate "
            f"({normalize_expr(expr)}), which yields the adjustment amount, "
            "not the remaining amount"
        )
        return "component", reasons, info
    if uses_rate and not has_complement and mults and not subs:
        reasons.append("expression applies the rate without a complement or subtraction")
        return "component", reasons, info
    return "unknown", reasons or ["expression shape is not a recognisable net/component form"], info


def _leaf_names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _role_params(contract: FunctionContract) -> tuple[set[str], set[str]]:
    rate: set[str] = set()
    base: set[str] = set()
    for p in contract.params:
        low = p.name.lower()
        if p.is_fraction or any(h in low for h in _RATE_HINTS):
            rate.add(p.name)
        elif any(h in low for h in _BASE_HINTS):
            base.add(p.name)
    # Fallback: first non-rate parameter is treated as the base.
    if not base:
        for p in contract.params:
            if p.name not in rate:
                base.add(p.name)
                break
    return rate, base


def _expected_value(contract: FunctionContract, rate: set[str], base: set[str]) -> tuple[str, list[str]]:
    """Derive the expected result: prefer the documented worked example."""
    if contract.examples:
        ex = contract.examples[0]
        bits = [f"{k}={v:g}" for k, v in sorted(ex.inputs.items())]
        return (
            f"For {', '.join(bits)} the declared contract requires {ex.expected_output:g}.",
            [f"worked example at docstring line {ex.source_line}: {ex.source_text!r}"]
            + ex.derivation,
        )
    if rate and base:
        b = sorted(base)[0]
        r = sorted(rate)[0]
        return (
            f"The contract declares a net result, so the return value must be "
            f"`{b} * (1 - {r})` rather than `{b} * {r}`.",
            ["derived from return_semantics=net_remaining; no numeric example is documented"],
        )
    return ("", ["insufficient contract detail to derive an expected value"])


def evaluate(ctx: RuleContext) -> list[FindingDraft]:
    drafts: list[FindingDraft] = []
    for fc in ctx.files:
        if fc.new_tree is None:
            continue
        changed_lines = fc.diff.changed_new_lines
        candidates = {
            fn.name: fn
            for fn in (function_at_line(fc.new_tree, ln) for ln in changed_lines)
            if fn is not None
        }
        for name, fn in candidates.items():
            contract = fc.contracts.get(name)
            if contract is None:
                continue
            if contract.return_semantics != "net_remaining":
                continue  # nothing in the declared contract to contradict
            rets = return_expressions(fn)
            if not rets:
                continue
            rate, base = _role_params(contract)
            # Evaluate every return path; a mismatch on any path is a defect.
            worst: tuple[str, list[str], dict[str, Any], ast.expr] | None = None
            for r in rets:
                klass, reasons, info = _classify_arithmetic(r, rate, base)
                if klass == "component":
                    worst = (klass, reasons, info, r)
                    break
            if worst is None:
                continue
            klass, reasons, info, offending = worst

            old_fn = fc.old_functions.get(name)
            old_klass = ""
            old_expr_src = ""
            prior_evidence: list[Evidence] = []
            if old_fn is not None:
                old_rets = return_expressions(old_fn)
                if old_rets:
                    old_klass, old_reasons, old_info = _classify_arithmetic(
                        old_rets[0], rate, base
                    )
                    old_expr_src = normalize_expr(old_rets[0])
                    prior_evidence.append(
                        Evidence(
                            kind="prior_revision",
                            description=(
                                f"At the target revision `{name}` returned "
                                f"`{old_expr_src}` (classified '{old_klass}')."
                            ),
                            file_path=fc.path,
                            # Cite the SAME range that `content` quotes. Citing the
                            # enclosing function while quoting one return statement
                            # makes the excerpt unverifiable against its own lines.
                            line_start=old_rets[0].lineno,
                            line_end=getattr(old_rets[0], "end_lineno", old_rets[0].lineno),
                            content=lines_of(
                                fc.old_source,
                                old_rets[0].lineno,
                                getattr(old_rets[0], "end_lineno", old_rets[0].lineno),
                            ),
                            note=(
                                f"enclosing definition spans lines {old_fn.lineno}-"
                                f"{getattr(old_fn, 'end_lineno', old_fn.lineno)}"
                                + (f"; {'; '.join(old_reasons)}" if old_reasons else "")
                            ),
                            check=CHECK_SOURCE_LINES,
                        )
                    )

            expected_text, expected_basis = _expected_value(contract, rate, base)
            basis = [
                f"declared return semantics = net_remaining ({len(contract.semantics_evidence)} doc signal(s))",
                f"implementation classified as '{klass}' ({len(reasons)} structural signal(s))",
            ]
            if old_klass == "net":
                basis.append(
                    "the target revision computed the net result and this change removed it"
                )
            if contract.examples:
                basis.append("a numeric worked example in the contract contradicts the code")

            score = 0.45
            if contract.semantics_evidence:
                score += 0.15
            if klass == "component":
                score += 0.15
            if old_klass == "net":
                score += 0.15
            if contract.examples:
                score += 0.10
            level = "high" if score >= 0.75 else "medium" if score >= 0.5 else "low"

            evidence = [
                Evidence(
                    kind="diff_hunk",
                    description=f"Return expression of `{name}` changed in this merge request.",
                    file_path=fc.path,
                    line_start=offending.lineno,
                    line_end=getattr(offending, "end_lineno", offending.lineno),
                    content=fc.diff.unified(),
                    note="changed lines: "
                    + ", ".join(str(x) for x in sorted(changed_lines)[:12]),
                    check=CHECK_DIFF_MENTIONS,
                ),
                Evidence(
                    kind="source_excerpt",
                    description="Current (source-revision) implementation of the offending return.",
                    file_path=fc.path,
                    line_start=offending.lineno,
                    line_end=getattr(offending, "end_lineno", offending.lineno),
                    content=lines_of(
                        fc.new_source,
                        offending.lineno,
                        getattr(offending, "end_lineno", offending.lineno),
                    ),
                    check=CHECK_SOURCE_LINES,
                ),
                Evidence(
                    kind="contract_quote",
                    description="Declared contract states a net/final result.",
                    file_path=fc.path,
                    line_start=contract.line_start,
                    line_end=contract.line_end,
                    content=(contract.docstring or "")[:1200],
                    note=" | ".join(contract.semantics_evidence),
                    check=CHECK_DOCSTRING_QUOTE,
                ),
                Evidence(
                    kind="ast_fact",
                    description="Structural classification of the return expression.",
                    file_path=fc.path,
                    line_start=offending.lineno,
                    line_end=getattr(offending, "end_lineno", offending.lineno),
                    content=str(info),
                    note="; ".join(reasons),
                    check=CHECK_ANALYSIS,
                ),
                *prior_evidence,
            ]
            if expected_basis:
                evidence.append(
                    Evidence(
                        kind="contract_quote",
                        description="Derivation of the expected value from the declared contract.",
                        file_path=fc.path,
                        line_start=contract.docstring_line,
                        # Quote the docstring line VERBATIM so the claim is
                        # checkable; the reasoning belongs in `note`, not in the
                        # quoted text.
                        content=(
                            contract.examples[0].source_text
                            if contract.examples
                            else expected_text
                        ),
                        note=f"{expected_text} | " + " | ".join(expected_basis),
                        check=CHECK_DOCSTRING_QUOTE,
                    )
                )

            missing: list[str] = []
            if not contract.examples:
                missing.append(
                    "no numeric worked example is documented; the expected value was "
                    "derived from the stated return semantics instead"
                )
            if old_fn is None:
                missing.append(
                    "function is new in this change, so there is no prior behaviour to compare against"
                )
            if not contract.raises:
                missing.append("no documented error contract for invalid inputs")

            b = sorted(base)[0] if base else "value"
            r = sorted(rate)[0] if rate else "rate"
            drafts.append(
                FindingDraft(
                    rule_id=RULE_ID,
                    category=FindingCategory.LOGIC_ERROR,
                    severity=Severity.CRITICAL if old_klass == "net" else Severity.HIGH,
                    title=(
                        f"`{name}` returns the adjustment amount instead of the "
                        f"final amount required by its contract"
                    ),
                    description=(
                        f"The declared contract for `{name}` promises a net/final result "
                        f"({contract.semantics_evidence[0] if contract.semantics_evidence else 'name/docstring signal'}), "
                        f"but the implementation at the source revision returns "
                        f"`{info['normalized']}`, which is the size of the adjustment only. "
                        + (
                            f"The target revision returned `{old_expr_src}`, so this change "
                            f"silently inverted the meaning of the return value."
                            if old_expr_src
                            else ""
                        )
                    ),
                    file_path=fc.path,
                    symbol=name,
                    line_start=fn.lineno,
                    line_end=getattr(fn, "end_lineno", fn.lineno),
                    evidence=evidence,
                    expected_behavior=expected_text,
                    suspected_behavior=(
                        f"`{name}` returns `{info['normalized']}`, i.e. the adjustment "
                        f"amount, not the amount remaining after the adjustment."
                    ),
                    confidence=confidence(level, score, basis),
                    recommended_action=(
                        "Generate a reproduction test from the documented worked example, "
                        "execute it against the source revision, and if it fails restore the "
                        "net-result expression."
                    ),
                    facts=[
                        f"return expression at source revision: {info['normalized']}",
                        f"structural classification: {klass}",
                        f"declared return semantics: {contract.return_semantics}",
                    ]
                    + ([f"return expression at target revision: {old_expr_src}"] if old_expr_src else []),
                    hypotheses=[
                        "the author intended a percentage-style API and converted the "
                        "call sites, but the docstring was not updated",
                        "the simplification dropped the complement factor by accident",
                    ],
                    missing_information=missing,
                    contract=contract,
                    repair_hint=RepairHint(
                        strategy="restore_net_result",
                        file_path=fc.path,
                        symbol=name,
                        original_expr=info["normalized"],
                        replacement_expr=f"{b} * (1 - {r})",
                        rationale=(
                            "Smallest correction that satisfies the declared contract: "
                            f"return the base value minus the rate portion, i.e. `{b} * (1 - {r})`."
                        ),
                    ),
                    testable=bool(expected_text),
                )
            )
    return drafts


__all__ = ["DESCRIPTION", "RULE_ID", "evaluate", "source_segment"]
