"""R-BOUND-001 -- boundary/off-by-one changes introduced by the diff.

Detects comparison-operator flips (``<`` <-> ``<=``), changed numeric bounds in
``range()``/slices, and modified offset constants.  A boundary flip is often
deliberate, so this rule reports a *hypothesis* and, where a contract exists,
derives the boundary value to probe.  It never asserts intent.
"""

from __future__ import annotations

import ast
from typing import Any

from ..astutil import (
    comparisons_in,
    function_at_line,
    lines_of,
    normalize_expr,
)
from .base import (
    CHECK_ANALYSIS,
    CHECK_SOURCE_LINES,
    Evidence,
    FindingDraft,
    RepairHint,
    RuleContext,
    confidence,
)
from app.models import FindingCategory, Severity

RULE_ID = "R-BOUND-001"
DESCRIPTION = "Boundary comparison or numeric bound changed by this diff"

_OP_NAME = {
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Eq: "==",
    ast.NotEq: "!=",
}
_INCLUSIVE = {ast.LtE, ast.GtE, ast.Eq}
_EXCLUSIVE = {ast.Lt, ast.Gt, ast.NotEq}


def _op_types(cmp_node: ast.Compare) -> set[type]:
    return {type(op) for op in cmp_node.ops}


def _consts(node: ast.AST) -> list[Any]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))]


def evaluate(ctx: RuleContext) -> list[FindingDraft]:
    drafts: list[FindingDraft] = []
    for fc in ctx.files:
        if fc.new_tree is None or fc.old_tree is None:
            continue
        changed = fc.diff.changed_new_lines
        for name, new_fn in fc.new_functions.items():
            old_fn = fc.old_functions.get(name)
            if old_fn is None:
                continue
            if not any(new_fn.lineno <= ln <= (getattr(new_fn, "end_lineno", new_fn.lineno) or new_fn.lineno) for ln in changed):
                continue

            new_cmps = comparisons_in(new_fn)
            old_cmps = comparisons_in(old_fn)
            for nc in new_cmps:
                for oc in old_cmps:
                    if normalize_expr(nc.left) != normalize_expr(oc.left):
                        continue
                    if len(nc.ops) != len(oc.ops):
                        continue
                    n_ops, o_ops = _op_types(nc), _op_types(oc)
                    flipped = bool(
                        (n_ops & _INCLUSIVE and o_ops & _EXCLUSIVE)
                        or (n_ops & _EXCLUSIVE and o_ops & _INCLUSIVE)
                    )
                    n_consts, o_consts = _consts(nc), _consts(oc)
                    bound_shift = n_consts != o_consts and bool(n_consts) and bool(o_consts)
                    if not (flipped or bound_shift):
                        continue

                    what = (
                        f"comparison operator changed from "
                        f"{'/'.join(_OP_NAME.get(o, '?') for o in oc.ops)} to "
                        f"{'/'.join(_OP_NAME.get(o, '?') for o in nc.ops)}"
                        if flipped
                        else f"numeric bound changed from {o_consts} to {n_consts}"
                    )
                    contract = fc.contracts.get(name)
                    boundary_probe = _boundary_probe(nc, oc, flipped, n_consts, o_consts)
                    basis = [f"{what} inside `{name}`"]
                    score = 0.4
                    if contract and contract.examples:
                        score += 0.15
                        basis.append("a documented example exists, so the boundary can be probed")
                    if boundary_probe:
                        score += 0.15
                        basis.append(f"boundary value identified: {boundary_probe}")
                    if fc.old_contracts.get(name) and fc.old_contracts[name].docstring != (contract.docstring if contract else ""):
                        score += 0.1
                        basis.append("the docstring changed alongside the comparison")
                    level = "high" if score >= 0.7 else "medium" if score >= 0.5 else "low"

                    missing: list[str] = []
                    if not _has_boundary_test(ctx, name):
                        missing.append("no test in the repository pins this boundary")
                    if not contract or not contract.examples:
                        missing.append(
                            "the change author's intent is not recorded in the diff or docstring"
                        )

                    drafts.append(
                        FindingDraft(
                            rule_id=RULE_ID,
                            category=FindingCategory.BOUNDARY_CONDITION,
                            severity=Severity.HIGH if flipped else Severity.MEDIUM,
                            title=f"Boundary condition changed in `{name}`: {what}",
                            description=(
                                f"This diff changes an inclusive/exclusive boundary in `{name}`. "
                                f"Such a change shifts which inputs are accepted by exactly one "
                                f"step and is a common source of off-by-one regressions. "
                                f"It may be intentional; the audit records it as a hypothesis "
                                f"until a boundary probe is executed."
                            ),
                            file_path=fc.path,
                            symbol=name,
                            line_start=nc.lineno,
                            line_end=getattr(nc, "end_lineno", nc.lineno),
                            evidence=[
                                Evidence(
                                    kind="source_excerpt",
                                    description="Boundary comparison at the source revision.",
                                    file_path=fc.path,
                                    line_start=nc.lineno,
                                    line_end=getattr(nc, "end_lineno", nc.lineno),
                                    content=lines_of(fc.new_source, nc.lineno, getattr(nc, "end_lineno", nc.lineno)),
                                    check=CHECK_SOURCE_LINES,
                                ),
                                Evidence(
                                    kind="prior_revision",
                                    description="The same comparison at the target revision.",
                                    file_path=fc.path,
                                    line_start=oc.lineno,
                                    line_end=getattr(oc, "end_lineno", oc.lineno),
                                    content=lines_of(fc.old_source, oc.lineno, getattr(oc, "end_lineno", oc.lineno)),
                                    check=CHECK_SOURCE_LINES,
                                ),
                                Evidence(
                                    kind="ast_fact",
                                    description=what,
                                    file_path=fc.path,
                                    line_start=nc.lineno,
                                    content=f"new: {normalize_expr(nc)}\nold: {normalize_expr(oc)}",
                                    check=CHECK_ANALYSIS,
                                ),
                            ],
                            expected_behavior=(
                                f"Boundary handling for `{name}` matches the behaviour documented "
                                f"in its contract."
                                if contract
                                else "Boundary behaviour is not declared; the target-revision "
                                "behaviour is the only available reference."
                            ),
                            suspected_behavior=(
                                f"Inputs at the boundary value {boundary_probe} are now "
                                f"{'accepted' if flipped and (_op_types(nc) & _INCLUSIVE) else 'rejected'} "
                                f"where they were previously "
                                f"{'rejected' if flipped and (_op_types(nc) & _INCLUSIVE) else 'accepted'}."
                                if boundary_probe
                                else f"Boundary behaviour of `{name}` differs from the target revision."
                            ),
                            confidence=confidence(level, score, basis),
                            recommended_action=(
                                "Confirm whether the boundary change is intentional. If it is not, "
                                "restore the target-revision comparison; if it is, add a test that "
                                "pins the new boundary so it cannot silently drift again."
                            ),
                            facts=[
                                f"target revision: {normalize_expr(oc)}",
                                f"source revision: {normalize_expr(nc)}",
                                what,
                            ],
                            hypotheses=[
                                "an off-by-one regression at the boundary value",
                                "an intentional relaxation/tightening that was not documented",
                            ],
                            missing_information=missing,
                            contract=contract,
                            testable=bool(boundary_probe) or bool(contract and contract.examples),
                        )
                    )
                    break  # one finding per new comparison
    return drafts


def _boundary_probe(
    nc: ast.Compare, oc: ast.Compare, flipped: bool, n_consts: list[Any], o_consts: list[Any]
) -> str:
    consts = n_consts or o_consts
    if consts:
        return str(consts[0])
    return ""


def _has_boundary_test(ctx: RuleContext, symbol: str) -> bool:
    for src in ctx.test_sources.values():
        if symbol in src:
            return True
    return False
