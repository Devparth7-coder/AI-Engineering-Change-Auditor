"""R-ASSUME-001 / R-LEAK-001 -- incorrect assumptions and resource leaks.

Small, high-precision structural checks that are cheap to verify and commonly
wrong in practice:

* mutable default arguments (shared across calls),
* identity comparison (``is``) against literals,
* exact ``==`` comparison against a float literal,
* truthiness tests that treat ``0``/``0.0`` as absent,
* files/sockets opened without a context manager.
"""

from __future__ import annotations

import ast

from ..astutil import lines_of, normalize_expr
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

ASSUME_RULE_ID = "R-ASSUME-001"
LEAK_RULE_ID = "R-LEAK-001"
DESCRIPTION = "Incorrect assumptions (mutable defaults, identity/float equality, truthiness) and resource leaks"

_MUTABLE_DEFAULTS = (ast.List, ast.Dict, ast.Set)
_RESOURCE_CALLS = {"open", "socket", "connect", "create_connection", "mkstemp", "NamedTemporaryFile"}


def _touched(fc, fn) -> bool:  # noqa: ANN001
    end = getattr(fn, "end_lineno", fn.lineno) or fn.lineno
    return any(fn.lineno <= ln <= end for ln in fc.diff.changed_new_lines)


def _check_mutable_defaults(fc, name, fn) -> list[FindingDraft]:  # noqa: ANN001
    out: list[FindingDraft] = []
    defaults = list(fn.args.defaults) + [d for d in fn.args.kw_defaults if d is not None]
    for d in defaults:
        if isinstance(d, _MUTABLE_DEFAULTS):
            arg_names = [a.arg for a in fn.args.args]
            out.append(
                FindingDraft(
                    rule_id=ASSUME_RULE_ID,
                    category=FindingCategory.INCORRECT_ASSUMPTION,
                    severity=Severity.HIGH,
                    title=f"`{name}` uses a mutable default argument",
                    description=(
                        f"A new mutable object ({normalize_expr(d)}) is created once at "
                        f"function-definition time and shared by every call. Mutating it "
                        f"leaks state between invocations."
                    ),
                    file_path=fc.path,
                    symbol=name,
                    line_start=d.lineno,
                    line_end=getattr(d, "end_lineno", d.lineno),
                    evidence=[
                        Evidence(
                            kind="ast_fact",
                            description="Default argument is a mutable literal.",
                            file_path=fc.path,
                            line_start=d.lineno,
                            line_end=getattr(d, "end_lineno", d.lineno),
                            content=f"def {name}({', '.join(arg_names)}, ...={normalize_expr(d)})",
                            check=CHECK_ANALYSIS,
                        ),
                        Evidence(
                            kind="source_excerpt",
                            description="Function signature.",
                            file_path=fc.path,
                            line_start=fn.lineno,
                            line_end=getattr(fn, "end_lineno", fn.lineno),
                            content=lines_of(fc.new_source, fn.lineno, min(fn.lineno + 3, getattr(fn, "end_lineno", fn.lineno))),
                            check=CHECK_SOURCE_LINES,
                        ),
                    ],
                    expected_behavior="Each call receives an independent container.",
                    suspected_behavior="State accumulates across calls through the shared default.",
                    confidence=confidence(
                        "high",
                        0.85,
                        ["mutable literal used directly as a default argument"],
                    ),
                    recommended_action=(
                        "Reproduce by calling the function twice and asserting the second call "
                        "is unaffected by the first; then switch to `None` sentinel + in-body default."
                    ),
                    facts=[f"default literal: {normalize_expr(d)}"],
                    hypotheses=["cross-call state contamination"],
                    missing_information=[],
                    contract=fc.contracts.get(name),
                    testable=True,
                )
            )
    return out


def _check_identity_and_float_eq(fc, name, fn) -> list[FindingDraft]:  # noqa: ANN001
    out: list[FindingDraft] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare):
            continue
        for op, comparator in zip(node.ops, node.comparators):
            literal = None
            if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(comparator, ast.Constant):
                if comparator.value is not None and comparator.value is not Ellipsis:
                    literal = ("identity comparison against a literal", comparator)
            elif isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant) and isinstance(
                comparator.value, float
            ):
                literal = ("exact float equality", comparator)
            elif isinstance(op, (ast.Is, ast.IsNot)) and isinstance(node.left, ast.Constant):
                if node.left.value is not None:
                    literal = ("identity comparison against a literal", node.left)
            if literal is None:
                continue
            why, const = literal
            out.append(
                FindingDraft(
                    rule_id=ASSUME_RULE_ID,
                    category=FindingCategory.INCORRECT_ASSUMPTION,
                    severity=Severity.MEDIUM if why == "exact float equality" else Severity.HIGH,
                    title=f"`{name}` uses {why}",
                    description=(
                        f"`{normalize_expr(node)}` relies on {why}. "
                        + (
                            "Identity is an implementation detail of interning and is not "
                            "guaranteed for equal-valued objects."
                            if "identity" in why
                            else "Binary floating point rarely compares exactly equal; "
                            "computed values will spuriously mismatch."
                        )
                    ),
                    file_path=fc.path,
                    symbol=name,
                    line_start=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno),
                    evidence=[
                        Evidence(
                            kind="source_excerpt",
                            description=why,
                            file_path=fc.path,
                            line_start=node.lineno,
                            line_end=getattr(node, "end_lineno", node.lineno),
                            content=lines_of(fc.new_source, node.lineno, getattr(node, "end_lineno", node.lineno)),
                            check=CHECK_SOURCE_LINES,
                        )
                    ],
                    expected_behavior=(
                        "Value equality (`==`) is used for literals."
                        if "identity" in why
                        else "An approximate comparison (e.g. math.isclose / pytest.approx) is used."
                    ),
                    suspected_behavior=f"{normalize_expr(node)} may evaluate incorrectly.",
                    confidence=confidence("medium", 0.6, [why]),
                    recommended_action="Confirm intent; prefer value or approximate comparison.",
                    facts=[f"comparison: {normalize_expr(node)}"],
                    hypotheses=[f"{why} produces an incorrect result for some inputs"],
                    missing_information=["the value range reaching this comparison is not documented"],
                    contract=fc.contracts.get(name),
                    testable=True,
                )
            )
    return out


def _check_resource_leaks(fc, name, fn) -> list[FindingDraft]:  # noqa: ANN001
    out: list[FindingDraft] = []
    with_items: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.With):
            for item in node.items:
                with_items.add(id(item.context_expr))
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or id(node) in with_items:
            continue
        fname = ""
        if isinstance(node.func, ast.Name):
            fname = node.func.id
        elif isinstance(node.func, ast.Attribute):
            fname = node.func.attr
        if fname not in _RESOURCE_CALLS:
            continue
        # Is the call the context expression of a `with`?
        if any(node is it.context_expr for it in _with_items(fn)):
            continue
        # Is it assigned and later closed on every path? Simple heuristic: any
        # `.close()` in the function counts as an explicit release.
        has_close = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "close"
            for n in ast.walk(fn)
        )
        if has_close:
            continue
        out.append(
            FindingDraft(
                rule_id=LEAK_RULE_ID,
                category=FindingCategory.RESOURCE_LEAK,
                severity=Severity.MEDIUM,
                title=f"`{name}` acquires a resource without guaranteed release",
                description=(
                    f"`{fname}(...)` is called outside a `with` block and no `.close()` "
                    f"exists in the function, so an exception before the end of the "
                    f"function leaks the resource."
                ),
                file_path=fc.path,
                symbol=name,
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno),
                evidence=[
                    Evidence(
                        kind="source_excerpt",
                        description=f"`{fname}` called without a context manager.",
                        file_path=fc.path,
                        line_start=node.lineno,
                        line_end=getattr(node, "end_lineno", node.lineno),
                        content=lines_of(fc.new_source, node.lineno, getattr(node, "end_lineno", node.lineno)),
                        check=CHECK_SOURCE_LINES,
                    )
                ],
                expected_behavior="The resource is released even when the body raises.",
                suspected_behavior="The resource leaks on the exception path.",
                confidence=confidence("medium", 0.55, [f"`{fname}` outside `with`, no `.close()`"]),
                recommended_action="Wrap in a `with` statement.",
                facts=[f"call: {normalize_expr(node)}"],
                hypotheses=["file-descriptor or socket leak on the error path"],
                missing_information=["the exception path is not exercised by any test"],
                contract=fc.contracts.get(name),
                testable=False,
            )
        )
    return out


def _with_items(fn: ast.AST) -> list[ast.withitem]:
    return [item for n in ast.walk(fn) if isinstance(n, ast.With) for item in n.items]


def evaluate(ctx: RuleContext) -> list[FindingDraft]:
    drafts: list[FindingDraft] = []
    for fc in ctx.files:
        if fc.new_tree is None or not fc.path.endswith(".py"):
            continue
        for name, fn in fc.new_functions.items():
            if not _touched(fc, fn):
                continue
            drafts.extend(_check_mutable_defaults(fc, name, fn))
            drafts.extend(_check_identity_and_float_eq(fc, name, fn))
            drafts.extend(_check_resource_leaks(fc, name, fn))
    return drafts


__all__ = ["ASSUME_RULE_ID", "DESCRIPTION", "LEAK_RULE_ID", "evaluate"]
