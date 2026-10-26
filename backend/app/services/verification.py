"""Verification service.

Executes the full evidence chain for one approved patch:

1. build an isolated workspace from the **exact original revision**;
2. run the reproduction test there  -> baseline (must show the defect);
3. apply the approved patch to a second isolated workspace;
4. run the reproduction test again  -> must pass, and the *intended* tests must
   actually have executed (a skip or a collection error is not a pass);
5. run the repository's regression suite at both revisions and compare;
6. capture real exit codes, per-test results and sanitised logs.

Outcome labels are deliberately narrow.  ``VERIFIED_FOR_TESTED_BEHAVIOR`` says
only that the specific contract assertions now hold -- it never claims the
application as a whole is correct, and the report repeats that limitation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import sandbox
from .pytest_runner import (
    PytestReport,
    classify_reproduction,
    match_intended,
    run_pytest,
    summarise_output,
)
from app.config import Settings, get_settings
from app.models import ReproOutcome, TestRunStatus, VerificationOutcome

logger = logging.getLogger(__name__)

REGRESSION_TEST_HINTS = ("tests/", "test_", "_test.py")


class RevisionSource(Protocol):
    """Supplies exact revisions to the verifier."""

    def export(self, sha: str, dest: Path) -> None: ...
    def file_at(self, sha: str, path: str) -> str: ...
    def test_paths(self, sha: str) -> list[str]: ...


@dataclass
class RunRecord:
    """One executed test run, ready for persistence."""

    label: str
    test_type: str
    revision: str
    revision_label: str
    report: PytestReport
    repro_outcome: ReproOutcome | None = None
    repro_reason: str = ""
    intended_match: dict[str, Any] = field(default_factory=dict)
    patch_applied: bool = False

    @property
    def summary_line(self) -> str:
        r = self.report
        return (
            f"{self.label}: status={r.status.value} exit={r.exit_code} "
            f"total={r.total} passed={r.passed} failed={r.failed} "
            f"skipped={r.skipped} errors={r.errors} "
            f"isolation={r.isolation_level} ({r.duration_ms} ms)"
        )

    def to_dict(self) -> dict[str, Any]:
        r = self.report
        return {
            "label": self.label,
            "test_type": self.test_type,
            "revision": self.revision,
            "revision_label": self.revision_label,
            "patch_applied": self.patch_applied,
            "execution_status": r.status.value,
            "exit_code": r.exit_code,
            "tests_total": r.total,
            "tests_passed": r.passed,
            "tests_failed": r.failed,
            "tests_skipped": r.skipped,
            "tests_errored": r.errors,
            "timed_out": r.timed_out,
            "duration_ms": r.duration_ms,
            "isolation_level": r.isolation_level,
            "network_disabled": r.network_disabled,
            "intended_tests_executed": bool(self.intended_match.get("all_intended_executed")),
            "intended_match": self.intended_match,
            "repro_outcome": self.repro_outcome.value if self.repro_outcome else None,
            "repro_reason": self.repro_reason,
            "sanitized_output": summarise_output(r),
            "notes": r.notes,
        }


@dataclass
class VerificationResult:
    outcome: VerificationOutcome
    summary: str
    reasons: list[str]
    remaining_concerns: list[str]
    runs: list[RunRecord]
    regressions: list[dict[str, Any]]
    intended_tests_executed: bool
    scope_statement: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "summary": self.summary,
            "reasons": self.reasons,
            "remaining_concerns": self.remaining_concerns,
            "runs": [r.to_dict() for r in self.runs],
            "regressions": self.regressions,
            "intended_tests_executed": self.intended_tests_executed,
            "scope_statement": self.scope_statement,
        }


def _prepare_workspace(
    source: RevisionSource,
    sha: str,
    settings: Settings,
    *,
    generated_files: dict[str, str],
    patch: tuple[str, str] | None = None,
    prefix: str = "verify",
) -> tuple[Path, dict[str, Any]]:
    """Materialise a revision, optionally apply a patch, and stage audit tests."""
    staging = sandbox.make_workspace(settings, prefix=prefix)
    repo_dir = staging / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    source.export(sha, repo_dir)

    apply_info: dict[str, Any] = {"applied": False}
    if patch is not None:
        rel_path, proposed = patch
        from .patching import apply_to_workspace

        apply_info = apply_to_workspace(
            staging / "repo", rel_path, proposed_full_content=proposed
        )

    tests_dir = staging / "audit_tests"
    tests_dir.mkdir(exist_ok=True)
    for name, content in generated_files.items():
        (tests_dir / name).write_text(content, encoding="utf-8")
    return staging, apply_info


def _pythonpath(ws: Path) -> list[str]:
    repo = ws / "repo"
    candidates = [repo, repo / "src"]
    return [str(c) for c in candidates if c.is_dir()]


def _regression_targets(source: RevisionSource, sha: str) -> list[str]:
    paths = source.test_paths(sha)
    targets = [f"repo/{p}" for p in paths if any(h in p for h in REGRESSION_TEST_HINTS)]
    return targets or ["repo/tests"]


def _compare_regressions(
    baseline: PytestReport, patched: PytestReport
) -> list[dict[str, Any]]:
    """Tests that passed before the patch and no longer pass after it."""
    before = {c.name: c.outcome for c in baseline.cases}
    after = {c.name: c.outcome for c in patched.cases}
    out: list[dict[str, Any]] = []
    for name, outcome in after.items():
        prev = before.get(name)
        if prev == "passed" and outcome in ("failed", "error"):
            case = next((c for c in patched.cases if c.name == name), None)
            out.append(
                {
                    "test": name,
                    "baseline_outcome": prev,
                    "patched_outcome": outcome,
                    "message": (case.message[:400] if case else ""),
                }
            )
    disappeared = [n for n in before if n not in after]
    for name in disappeared:
        if before[name] == "passed":
            out.append(
                {
                    "test": name,
                    "baseline_outcome": "passed",
                    "patched_outcome": "missing",
                    "message": "test no longer reported after the patch",
                }
            )
    return out


def verify_patch(
    *,
    source: RevisionSource,
    original_sha: str,
    file_path: str,
    original_full_content: str,
    proposed_full_content: str,
    generated_files: dict[str, str],
    repro_test_target: str,
    intended_node_ids: list[str],
    settings: Settings | None = None,
    symbol: str = "",
) -> VerificationResult:
    """Run the complete original-vs-patched comparison."""
    settings = settings or get_settings()
    runs: list[RunRecord] = []
    reasons: list[str] = []
    concerns: list[str] = []

    # ---- 1/2. baseline: reproduction test against the ORIGINAL revision ----
    ws_base, _ = _prepare_workspace(
        source, original_sha, settings, generated_files=generated_files, prefix="base"
    )
    try:
        base_repro_report = run_pytest(
            ws_base,
            [repro_test_target],
            settings=settings,
            label="baseline_reproduction",
            pythonpath=_pythonpath(ws_base),
        )
        base_outcome, base_reason, base_match = classify_reproduction(
            base_repro_report, intended_node_ids
        )
        runs.append(
            RunRecord(
                label="baseline_reproduction",
                test_type="reproduction",
                revision=original_sha,
                revision_label="original",
                report=base_repro_report,
                repro_outcome=base_outcome,
                repro_reason=base_reason,
                intended_match=base_match,
                patch_applied=False,
            )
        )
    finally:
        sandbox.cleanup_workspace(ws_base, settings)

    # ---- baseline regression suite (same revision, unpatched) ----
    ws_breg, _ = _prepare_workspace(
        source, original_sha, settings, generated_files={}, prefix="basereg"
    )
    try:
        base_reg_report = run_pytest(
            ws_breg,
            _regression_targets(source, original_sha),
            settings=settings,
            label="baseline_regression",
            pythonpath=_pythonpath(ws_breg),
        )
        runs.append(
            RunRecord(
                label="baseline_regression",
                test_type="regression",
                revision=original_sha,
                revision_label="original",
                report=base_reg_report,
            )
        )
    finally:
        sandbox.cleanup_workspace(ws_breg, settings)

    # ---- 3. apply the patch into a fresh isolated workspace ----
    ws_patch, apply_info = _prepare_workspace(
        source,
        original_sha,
        settings,
        generated_files=generated_files,
        patch=(file_path, proposed_full_content),
        prefix="patched",
    )
    try:
        if not apply_info.get("applied"):
            reasons.append(
                f"Patch could not be applied: {apply_info.get('reason', 'unknown error')}. "
                "No verification was performed."
            )
            return VerificationResult(
                outcome=VerificationOutcome.EXECUTION_ERROR,
                summary="Patch application failed; verification not performed.",
                reasons=reasons,
                remaining_concerns=["the proposed patch was never executed"],
                runs=runs,
                regressions=[],
                intended_tests_executed=False,
                scope_statement=_scope(symbol, intended_node_ids),
            )

        # ---- 4. reproduction test against the PATCHED revision ----
        post_repro_report = run_pytest(
            ws_patch,
            [repro_test_target],
            settings=settings,
            label="post_fix_reproduction",
            pythonpath=_pythonpath(ws_patch),
        )
        post_outcome, post_reason, post_match = classify_reproduction(
            post_repro_report, intended_node_ids
        )
        runs.append(
            RunRecord(
                label="post_fix_reproduction",
                test_type="post_fix_reproduction",
                revision=original_sha,
                revision_label="patched",
                report=post_repro_report,
                repro_outcome=post_outcome,
                repro_reason=post_reason,
                intended_match=post_match,
                patch_applied=True,
            )
        )

        # ---- 5. regression suite against the PATCHED revision ----
        post_reg_report = run_pytest(
            ws_patch,
            _regression_targets(source, original_sha),
            settings=settings,
            label="post_fix_regression",
            pythonpath=_pythonpath(ws_patch),
        )
        runs.append(
            RunRecord(
                label="post_fix_regression",
                test_type="post_fix_regression",
                revision=original_sha,
                revision_label="patched",
                report=post_reg_report,
                patch_applied=True,
            )
        )
    finally:
        sandbox.cleanup_workspace(ws_patch, settings)

    # ---- 6/7. compare and decide ----
    regressions = _compare_regressions(base_reg_report, post_reg_report)
    intended_executed = bool(post_match.get("all_intended_executed"))

    if post_repro_report.timed_out or post_repro_report.status in (
        TestRunStatus.TIMEOUT,
        TestRunStatus.EXECUTION_ERROR,
        TestRunStatus.NOT_COLLECTED,
    ):
        outcome = VerificationOutcome.EXECUTION_ERROR
        reasons.append(
            f"The post-fix reproduction run did not execute successfully "
            f"(status={post_repro_report.status.value}, timed_out={post_repro_report.timed_out}). "
            "An execution error, timeout or missing test is NEVER treated as a successful verification."
        )
    elif post_repro_report.status == TestRunStatus.SKIPPED or (
        post_repro_report.skipped and post_repro_report.skipped == post_repro_report.total
    ):
        outcome = VerificationOutcome.INCONCLUSIVE
        reasons.append(
            "The post-fix reproduction tests were skipped. A skipped test provides "
            "no evidence that the fix works."
        )
    elif not intended_executed:
        outcome = VerificationOutcome.INCONCLUSIVE
        reasons.append(
            f"The intended tests did not all execute (missing: {post_match.get('missing')}). "
            "Verification requires the intended assertions to actually run."
        )
    elif post_repro_report.status == TestRunStatus.FAILED or post_outcome == ReproOutcome.REPRODUCED:
        outcome = VerificationOutcome.VERIFICATION_FAILED
        reasons.append(
            "The contract assertions still fail after the patch "
            f"({', '.join(post_match.get('intended_assertion_failures', [])) or 'see log'}). "
            "The proposed correction does not satisfy the declared contract."
        )
    elif post_match.get("unrelated_failures"):
        outcome = VerificationOutcome.INCONCLUSIVE
        reasons.append(
            "The post-fix run failed for reasons unrelated to the intended assertion "
            f"({', '.join(post_match['unrelated_failures'])}); the result is inconclusive."
        )
    elif regressions:
        outcome = VerificationOutcome.REGRESSION_DETECTED
        reasons.append(
            f"{len(regressions)} previously passing regression test(s) no longer pass "
            f"after the patch: {', '.join(r['test'] for r in regressions[:5])}."
        )
    elif post_repro_report.status == TestRunStatus.PASSED and base_outcome == ReproOutcome.REPRODUCED:
        outcome = VerificationOutcome.VERIFIED_FOR_TESTED_BEHAVIOR
        reasons.append(
            f"Baseline: the contract assertion failed on the original revision "
            f"({base_reason.split('.')[0]}). "
            f"After the patch: {post_repro_report.passed}/{post_repro_report.total} "
            f"intended assertions pass and the intended tests executed. "
            f"Regression suite: {post_reg_report.passed}/{post_reg_report.total} pass "
            f"with no previously-passing test broken."
        )
    elif post_repro_report.status == TestRunStatus.PASSED and base_outcome != ReproOutcome.REPRODUCED:
        outcome = VerificationOutcome.INCONCLUSIVE
        reasons.append(
            "The patch makes the reproduction test pass, but the defect was NOT "
            f"reproduced on the original revision (baseline outcome: {base_outcome.value}). "
            "A test that passes both before and after proves nothing about the fix."
        )
    else:
        outcome = VerificationOutcome.INCONCLUSIVE
        reasons.append(
            f"Unclassified combination (baseline={base_outcome.value}, "
            f"post-fix={post_repro_report.status.value}); treated as inconclusive."
        )

    # ---- remaining concerns (always populated; never implied away) ----
    concerns.append(
        f"Scope: verification covers {len(intended_node_ids)} contract assertion(s) about "
        f"`{symbol or 'the changed symbol'}`. It does NOT establish that the application "
        "as a whole is correct."
    )
    if base_reg_report.total == 0:
        concerns.append(
            "No pre-existing regression tests were discovered, so the patch was not "
            "checked against the repository's own suite."
        )
    else:
        concerns.append(
            f"The regression suite contains {base_reg_report.total} test(s); behaviour outside "
            "their coverage is unverified."
        )
    if post_repro_report.isolation_level != "container":
        concerns.append(
            f"Tests ran under the '{post_repro_report.isolation_level}' isolation tier. "
            "For production use, run verification in the container tier."
        )
    if not regressions and post_reg_report.total:
        concerns.append(
            "Passing regression tests show no *detected* breakage, not the absence of breakage."
        )
    concerns.append(
        "The patch was applied only inside a throwaway audit workspace. Nothing was "
        "pushed, merged or deployed."
    )

    summary = _summarise(outcome, runs, regressions, symbol)
    return VerificationResult(
        outcome=outcome,
        summary=summary,
        reasons=reasons,
        remaining_concerns=concerns,
        runs=runs,
        regressions=regressions,
        intended_tests_executed=intended_executed,
        scope_statement=_scope(symbol, intended_node_ids),
    )


def _scope(symbol: str, intended: list[str]) -> str:
    return (
        f"Verified behaviour is limited to {len(intended)} assertion(s) about "
        f"`{symbol or 'the changed symbol'}`, executed under the recorded isolation tier."
    )


def _summarise(
    outcome: VerificationOutcome, runs: list[RunRecord], regressions: list[dict[str, Any]], symbol: str
) -> str:
    lines = [f"Verification outcome: {outcome.value}"]
    for r in runs:
        lines.append("  " + r.summary_line)
    if regressions:
        lines.append(f"  regressions: {len(regressions)} -> " + ", ".join(x["test"] for x in regressions[:5]))
    if symbol:
        lines.append(f"  symbol: {symbol}")
    return "\n".join(lines)


__all__ = [
    "RevisionSource",
    "RunRecord",
    "VerificationResult",
    "verify_patch",
]
