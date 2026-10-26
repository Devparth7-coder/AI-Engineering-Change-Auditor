"""R-TEST-001 -- changed public behaviour with no test coverage.

A coverage gap is a *finding about the change*, not a defect in the code, so it
is reported in the ``missing_tests`` category at ``evidenced`` strength: the
evidence is a repository scan, not an execution.  It also drives the auditor to
generate the test that the repository lacks.
"""

from __future__ import annotations

import ast

from ..astutil import lines_of
from .base import (
    CHECK_DOCSTRING_QUOTE,
    CHECK_SCAN,
    CHECK_SOURCE_LINES,
    Evidence,
    FindingDraft,
    RuleContext,
    confidence,
)
from app.models import FindingCategory, Severity

RULE_ID = "R-TEST-001"
DESCRIPTION = "Changed public function is not referenced by any test in the repository"


def _test_references(ctx: RuleContext, symbol: str) -> tuple[list[str], list[str]]:
    """Return ``(mentioning_files, asserting_files)`` for a symbol.

    Detection is AST-based on purpose.  A prose mention in a test docstring
    ("these tests do NOT cover apply_discount") is not coverage, and counting it
    as such produced a false negative.  A file only counts as *asserting* when
    the symbol is imported or called inside a ``test_*`` function that contains
    an ``assert`` or ``pytest.raises``.
    """
    mentioning: list[str] = []
    asserting: list[str] = []
    for path, src in ctx.test_sources.items():
        if symbol not in src:
            continue
        mentioning.append(path)
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == symbol:
                        imported.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[-1] == symbol:
                        imported.add(alias.asname or symbol)

        def _uses(node: ast.AST) -> bool:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id in (imported or {symbol}):
                    return True
                if isinstance(sub, ast.Attribute) and sub.attr == symbol:
                    return True
                if isinstance(sub, ast.Call):
                    f = sub.func
                    if isinstance(f, ast.Name) and f.id in (imported or {symbol}):
                        return True
            return False

        def _asserts(node: ast.AST) -> bool:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assert):
                    return True
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "raises"
                ):
                    return True
            return False

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test") and _uses(node) and _asserts(node):
                    asserting.append(path)
                    break
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if (
                        isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and sub.name.startswith("test")
                        and _uses(sub)
                        and _asserts(sub)
                    ):
                        asserting.append(path)
                        break
    return sorted(set(mentioning)), sorted(set(asserting))


def evaluate(ctx: RuleContext) -> list[FindingDraft]:
    drafts: list[FindingDraft] = []
    for fc in ctx.files:
        if fc.new_tree is None or not fc.path.endswith(".py"):
            continue
        if "/tests/" in fc.path or fc.path.startswith("tests/") or fc.path.endswith("_test.py"):
            continue  # do not report coverage gaps about test files themselves
        changed = fc.diff.changed_new_lines
        for name, new_fn in fc.new_functions.items():
            if name.startswith("_"):
                continue  # private helper: coverage is implied by its callers
            end = getattr(new_fn, "end_lineno", new_fn.lineno) or new_fn.lineno
            if not any(new_fn.lineno <= ln <= end for ln in changed):
                continue
            contract = fc.contracts.get(name)
            referencing, asserting = _test_references(ctx, name)
            if asserting:
                continue  # covered

            facts = [
                f"`{name}` is modified by this change",
                f"test files scanned: {len(ctx.test_sources)}",
                f"test files referencing `{name}`: {len(referencing)}",
                f"test files asserting on `{name}`: 0",
            ]
            basis = ["no test in the repository asserts on the changed function"]
            score = 0.55
            if contract and contract.examples:
                score += 0.2
                basis.append(
                    "the contract documents a worked example that no test exercises"
                )
            if contract and contract.return_semantics != "unknown":
                score += 0.1
                basis.append("the contract declares unambiguous return semantics")
            level = "high" if score >= 0.75 else "medium" if score >= 0.5 else "low"

            evidence = [
                Evidence(
                    kind="repo_scan",
                    description=(
                        f"Scanned {len(ctx.test_sources)} test file(s); none asserts on `{name}`."
                    ),
                    content="\n".join(
                        sorted(ctx.test_file_paths)[:20]
                    )
                    or "(no test files discovered)",
                    note=(
                        f"files mentioning the symbol without asserting: {referencing or 'none'}"
                    ),
                    check=CHECK_SCAN,
                ),
                Evidence(
                    kind="source_excerpt",
                    description=f"Changed function `{name}` at the source revision.",
                    file_path=fc.path,
                    line_start=new_fn.lineno,
                    line_end=end,
                    content=lines_of(fc.new_source, new_fn.lineno, end),
                    check=CHECK_SOURCE_LINES,
                ),
            ]
            if contract and contract.examples:
                ex = contract.examples[0]
                evidence.append(
                    Evidence(
                        kind="contract_quote",
                        description="Documented example that no test covers.",
                        file_path=fc.path,
                        line_start=ex.source_line,
                        content=ex.source_text,
                        note="; ".join(ex.derivation),
                        check=CHECK_DOCSTRING_QUOTE,
                    )
                )

            drafts.append(
                FindingDraft(
                    rule_id=RULE_ID,
                    category=FindingCategory.MISSING_TESTS,
                    severity=Severity.MEDIUM,
                    title=f"No test asserts on the changed behaviour of `{name}`",
                    description=(
                        f"`{name}` is modified by this merge request, but no test in the "
                        f"repository asserts on it. The existing suite can therefore stay "
                        f"green while `{name}` is wrong -- which is exactly how an "
                        f"undocumented behaviour change reaches the default branch."
                    ),
                    file_path=fc.path,
                    symbol=name,
                    line_start=new_fn.lineno,
                    line_end=end,
                    evidence=evidence,
                    expected_behavior=(
                        contract.examples[0].source_text
                        if contract and contract.examples
                        else f"`{name}` is covered by at least one asserting test."
                    ),
                    suspected_behavior=(
                        f"No test would fail if `{name}` returned an incorrect value."
                    ),
                    confidence=confidence(level, score, basis),
                    recommended_action=(
                        "Generate and commit a test derived from the declared contract. "
                        "This finding is evidence-backed by a repository scan; it is not a "
                        "defect in itself and must not be reported as one."
                    ),
                    facts=facts,
                    hypotheses=[
                        "behaviour changes to this function would pass CI unnoticed",
                    ],
                    missing_information=(
                        []
                        if contract and contract.examples
                        else ["no documented example exists from which to derive an assertion"]
                    ),
                    contract=contract,
                    testable=False,  # a coverage gap is not executable as a defect probe
                )
            )
    return drafts


__all__ = ["DESCRIPTION", "RULE_ID", "evaluate"]
