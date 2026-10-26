"""R-VALID-001 -- input validation removed, absent, or contradicted.

Grounded in two independent signals:

1. a guard that existed at the target revision and is gone at the source revision;
2. the contract's declared ``Raises`` clause not being honoured by the code
   (e.g. a documented ``ValueError`` for negative input that is never raised),
   or a new unguarded division/index that can fail on plausible input.

Because the expected exception comes from the *declared* contract, the
reproduction test this feeds is falsifiable rather than circular.
"""

from __future__ import annotations

import ast

from ..astutil import (
    guard_statements,
    lines_of,
    names_in,
    normalize_expr,
    raises_in,
)
from .base import (
    CHECK_ANALYSIS,
    CHECK_DOCSTRING_QUOTE,
    CHECK_SOURCE_LINES,
    Evidence,
    FindingDraft,
    RepairHint,
    RuleContext,
    confidence,
)
from app.models import FindingCategory, Severity

RULE_ID = "R-VALID-001"
DESCRIPTION = "Input validation removed, missing, or contradicting the declared contract"

_DOC_EXC_MAP = {
    "negative": "value < 0",
    "must not be negative": "value < 0",
    "outside": "value out of documented domain",
    "zero": "value == 0",
}


def _guard_signature(fn: ast.AST) -> set[str]:
    """Normalised shape of each top-level guard in a function."""
    sigs: set[str] = set()
    for g in guard_statements(fn):
        test = normalize_expr(g.test)
        raises_exc = ""
        for node in ast.walk(g):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                if isinstance(node.exc.func, ast.Name):
                    raises_exc = node.exc.func.id
        sigs.add(f"{test} -> raise {raises_exc or 'nothing'}")
    return sigs


def _divisions(fn: ast.AST) -> list[ast.BinOp]:
    return [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Div, ast.FloorDiv, ast.Mod))
    ]


def _exc_from_doc(contract) -> list[str]:  # noqa: ANN001
    return list(getattr(contract, "raises", []) or [])


def evaluate(ctx: RuleContext) -> list[FindingDraft]:
    drafts: list[FindingDraft] = []
    for fc in ctx.files:
        if fc.new_tree is None:
            continue
        changed = fc.diff.changed_new_lines
        for name, new_fn in fc.new_functions.items():
            end = getattr(new_fn, "end_lineno", new_fn.lineno) or new_fn.lineno
            touched = any(new_fn.lineno <= ln <= end for ln in changed)
            old_fn = fc.old_functions.get(name)
            contract = fc.contracts.get(name)

            # ---- (1) guards removed relative to the target revision ---------
            removed_guards: set[str] = set()
            if old_fn is not None and touched:
                removed_guards = _guard_signature(old_fn) - _guard_signature(new_fn)

            # ---- (2) declared exception is never raised ---------------------
            declared = set(_exc_from_doc(contract)) if contract else set()
            actually_raised = set()
            for r in raises_in(new_fn):
                if isinstance(r.exc, ast.Call) and isinstance(r.exc.func, ast.Name):
                    actually_raised.add(r.exc.func.id)
                elif isinstance(r.exc, ast.Name):
                    actually_raised.add(r.exc.id)
            unfulfilled = declared - actually_raised

            # ---- (3) new unguarded division ---------------------------------
            risky_div: list[str] = []
            for d in _divisions(new_fn):
                divisor_names = names_in(d.right)
                if not divisor_names:
                    continue
                guarded = any(
                    divisor_names & names_in(g.test) for g in guard_statements(new_fn)
                )
                if not guarded:
                    risky_div.append(
                        f"{normalize_expr(d)} divides by {normalize_expr(d.right)} "
                        f"with no guard on {sorted(divisor_names)}"
                    )

            if not (removed_guards or unfulfilled or risky_div):
                continue

            evidence: list[Evidence] = []
            facts: list[str] = []
            hypotheses: list[str] = []
            missing: list[str] = []
            basis: list[str] = []
            score = 0.3

            if removed_guards:
                score += 0.3
                basis.append(f"{len(removed_guards)} validation guard(s) present at the target revision were removed")
                facts.extend(f"removed guard: {g}" for g in sorted(removed_guards))
                hypotheses.append("inputs that were previously rejected are now silently accepted")
                evidence.append(
                    Evidence(
                        kind="prior_revision",
                        description=f"Guards in `{name}` at the target revision.",
                        file_path=fc.path,
                        line_start=old_fn.lineno if old_fn else None,
                        line_end=getattr(old_fn, "end_lineno", old_fn.lineno) if old_fn else None,
                        content=lines_of(fc.old_source, old_fn.lineno, getattr(old_fn, "end_lineno", old_fn.lineno))
                        if old_fn
                        else "",
                        note="removed: " + "; ".join(sorted(removed_guards)),
                        check=CHECK_SOURCE_LINES,
                    )
                )
            if unfulfilled:
                score += 0.25
                basis.append(
                    f"contract declares {sorted(unfulfilled)} but the implementation never raises them"
                )
                facts.append(f"declared exceptions: {sorted(declared)}")
                facts.append(f"exceptions actually raised: {sorted(actually_raised) or 'none'}")
                hypotheses.append("invalid input produces a wrong value instead of the documented error")
                missing.append(
                    "the exact input domain that should trigger the documented exception "
                    "is only partially described in the docstring"
                )
                evidence.append(
                    Evidence(
                        kind="contract_quote",
                        description=f"Declared `Raises` clause for `{name}`.",
                        file_path=fc.path,
                        line_start=contract.line_start if contract else None,
                        line_end=contract.line_end if contract else None,
                        content=(contract.docstring or "")[:1000] if contract else "",
                        note=f"declared={sorted(declared)} raised={sorted(actually_raised)}",
                        check=CHECK_DOCSTRING_QUOTE,
                    )
                )
            if risky_div:
                score += 0.2
                basis.append(f"{len(risky_div)} unguarded division/modulo operation(s)")
                facts.extend(risky_div)
                hypotheses.append("ZeroDivisionError on plausible input")
                missing.append("no documented domain excludes a zero divisor")
                evidence.append(
                    Evidence(
                        kind="ast_fact",
                        description="Unguarded division detected in the source revision.",
                        file_path=fc.path,
                        line_start=new_fn.lineno,
                        line_end=end,
                        content="\n".join(risky_div),
                        check=CHECK_ANALYSIS,
                    )
                )

            level = "high" if score >= 0.7 else "medium" if score >= 0.45 else "low"
            testable = bool(unfulfilled and contract and contract.examples) or bool(risky_div)

            expected = ""
            suspected = ""
            if unfulfilled and contract:
                expected = (
                    f"Per the declared contract, `{name}` must raise "
                    f"{sorted(unfulfilled)} for out-of-domain input."
                )
                suspected = f"`{name}` returns a value instead of raising {sorted(unfulfilled)}."
            elif removed_guards:
                expected = "Inputs rejected at the target revision remain rejected."
                suspected = "Inputs previously rejected are now accepted and produce undefined results."
            elif risky_div:
                expected = "Division by zero is prevented by an explicit guard."
                suspected = risky_div[0]

            drafts.append(
                FindingDraft(
                    rule_id=RULE_ID,
                    category=FindingCategory.INPUT_VALIDATION,
                    severity=Severity.HIGH if (removed_guards or unfulfilled) else Severity.MEDIUM,
                    title=f"Input validation gap in `{name}`",
                    description=(
                        "One or more validation signals disagree with the declared contract: "
                        + "; ".join(basis)
                        + ". The finding is recorded as a hypothesis until an execution proves it."
                    ),
                    file_path=fc.path,
                    symbol=name,
                    line_start=new_fn.lineno,
                    line_end=end,
                    evidence=evidence,
                    expected_behavior=expected,
                    suspected_behavior=suspected,
                    confidence=confidence(level, score, basis),
                    recommended_action=(
                        "Execute a boundary-input probe against the source revision and check "
                        "whether the documented exception is raised. If it is not, restore the "
                        "removed guard."
                        if testable
                        else "Manual review required: the contract does not declare the input "
                        "domain precisely enough to build a falsifiable probe."
                    ),
                    facts=facts,
                    hypotheses=hypotheses,
                    missing_information=missing,
                    contract=contract,
                    repair_hint=(
                        RepairHint(
                            strategy="restore_validation_guard",
                            file_path=fc.path,
                            symbol=name,
                            rationale="Re-add the guard(s) removed by this change.",
                            original_expr="",
                            replacement_expr="",
                        )
                        if removed_guards
                        else None
                    ),
                    testable=testable,
                )
            )
    return drafts


__all__ = ["DESCRIPTION", "RULE_ID", "evaluate"]
