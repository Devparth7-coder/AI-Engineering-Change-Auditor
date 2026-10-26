"""R-ERR-001 -- error-handling defects introduced by the diff.

Flags swallowed exceptions, bare ``except:``, broad catches without re-raise or
logging, and ``try`` blocks removed relative to the target revision.

These are usually *not* mechanically reproducible, so the rule is explicit about
that: ``testable`` is False unless the repository already exercises the failing
path, and the finding stays at ``hypothesis``/``inconclusive`` rather than being
promoted on the strength of an explanation alone.
"""

from __future__ import annotations

import ast

from ..astutil import lines_of, normalize_expr
from .base import (
    CHECK_SOURCE_LINES,
    Evidence,
    FindingDraft,
    RepairHint,
    RuleContext,
    confidence,
)
from app.models import FindingCategory, Severity

RULE_ID = "R-ERR-001"
DESCRIPTION = "Exception handling swallows, hides, or drops errors"

_LOG_CALLS = {"log", "logger", "logging", "warn", "warning", "error", "exception", "print", "debug", "info", "critical"}


def _handler_is_swallowed(handler: ast.excepthandler) -> tuple[bool, str]:
    body = handler.body
    if len(body) == 1 and isinstance(body[0], ast.Pass):
        return True, "`except ...: pass` discards the error entirely"
    if len(body) == 1 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        return True, "handler body is only a constant/ellipsis; the error is discarded"
    names = {
        n.id for n in ast.walk(handler) if isinstance(n, ast.Name)
    } | {
        n.attr for n in ast.walk(handler) if isinstance(n, ast.Attribute)
    } | {
        n.func.id
        for n in ast.walk(handler)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    re_raises = any(isinstance(n, ast.Raise) for n in ast.walk(handler))
    logs = bool(names & _LOG_CALLS)
    returns_default = any(isinstance(n, ast.Return) for n in ast.walk(handler))
    if not re_raises and not logs and returns_default:
        return True, "handler returns a default value without re-raising or logging"
    if not re_raises and not logs and not returns_default:
        return True, "handler neither re-raises nor logs the error"
    return False, ""


def _handler_type_name(handler: ast.excepthandler) -> str:
    if handler.type is None:
        return "<bare except>"
    return normalize_expr(handler.type)


def evaluate(ctx: RuleContext) -> list[FindingDraft]:
    drafts: list[FindingDraft] = []
    for fc in ctx.files:
        if fc.new_tree is None:
            continue
        changed = fc.diff.changed_new_lines
        for name, new_fn in fc.new_functions.items():
            end = getattr(new_fn, "end_lineno", new_fn.lineno) or new_fn.lineno
            if not any(new_fn.lineno <= ln <= end for ln in changed):
                continue
            old_fn = fc.old_functions.get(name)
            old_handlers = (
                len([h for n in ast.walk(old_fn) if isinstance(n, ast.Try) for h in n.handlers])
                if old_fn is not None
                else 0
            )
            problems: list[tuple[str, ast.excepthandler]] = []
            for node in ast.walk(new_fn):
                if isinstance(node, ast.Try):
                    for handler in node.handlers:
                        tname = _handler_type_name(handler)
                        if tname == "<bare except>":
                            problems.append(
                                ("bare `except:` catches SystemExit/KeyboardInterrupt too", handler)
                            )
                            continue
                        swallowed, why = _handler_is_swallowed(handler)
                        if swallowed:
                            problems.append((f"`except {tname}` {why}", handler))
                        elif tname in ("Exception", "BaseException"):
                            problems.append(
                                (f"broad `except {tname}` may mask unrelated failures", handler),
                            )
            new_handler_count = len(
                [h for n in ast.walk(new_fn) if isinstance(n, ast.Try) for h in n.handlers]
            )
            dropped_try = old_handlers > new_handler_count and old_handlers > 0

            if not problems and not dropped_try:
                continue

            evidence: list[Evidence] = []
            facts: list[str] = []
            basis: list[str] = []
            score = 0.25
            for why, handler in problems:
                facts.append(why)
                basis.append(why)
                score += 0.2
                evidence.append(
                    Evidence(
                        kind="source_excerpt",
                        description=why,
                        file_path=fc.path,
                        line_start=handler.lineno,
                        line_end=getattr(handler, "end_lineno", handler.lineno),
                        content=lines_of(fc.new_source, handler.lineno, getattr(handler, "end_lineno", handler.lineno)),
                        check=CHECK_SOURCE_LINES,
                    )
                )
            if dropped_try:
                score += 0.2
                facts.append(
                    f"handler count dropped from {old_handlers} (target revision) to "
                    f"{new_handler_count} (source revision)"
                )
                basis.append("error handling was removed by this change")
                evidence.append(
                    Evidence(
                        kind="prior_revision",
                        description=f"Error handling in `{name}` at the target revision.",
                        file_path=fc.path,
                        line_start=old_fn.lineno if old_fn else None,
                        line_end=getattr(old_fn, "end_lineno", old_fn.lineno) if old_fn else None,
                        content=lines_of(fc.old_source, old_fn.lineno, getattr(old_fn, "end_lineno", old_fn.lineno))
                        if old_fn
                        else "",
                        check=CHECK_SOURCE_LINES,
                    )
                )
            score = min(score, 0.9)
            level = "high" if score >= 0.7 else "medium" if score >= 0.45 else "low"
            severity = Severity.HIGH if any("bare" in f for f in facts) else Severity.MEDIUM

            drafts.append(
                FindingDraft(
                    rule_id=RULE_ID,
                    category=FindingCategory.ERROR_HANDLING,
                    severity=severity,
                    title=f"Error handling in `{name}` hides failures",
                    description=(
                        "This change alters how exceptions are handled: "
                        + "; ".join(basis)
                        + ". Failures in this path may be reported as success."
                    ),
                    file_path=fc.path,
                    symbol=name,
                    line_start=new_fn.lineno,
                    line_end=end,
                    evidence=evidence,
                    expected_behavior=(
                        "Errors on this path surface to the caller (re-raised, logged, or "
                        "returned as an explicit failure)."
                    ),
                    suspected_behavior=(
                        "Errors on this path are discarded, so callers cannot distinguish "
                        "success from failure."
                    ),
                    confidence=confidence(level, score, basis),
                    recommended_action=(
                        "Manual review. A mechanical reproduction test is not available unless "
                        "the repository can drive this path into a failing state; do not treat "
                        "this finding as reproduced."
                    ),
                    facts=facts,
                    hypotheses=[
                        "a real failure would be silently converted into a default/success result",
                        "the broad handler may catch errors unrelated to the intended case",
                    ],
                    missing_information=[
                        "no test in the repository drives this error path",
                        "the intended failure mode is not documented in the contract",
                    ],
                    contract=fc.contracts.get(name),
                    testable=False,
                )
            )
    return drafts


__all__ = ["DESCRIPTION", "RULE_ID", "evaluate"]
