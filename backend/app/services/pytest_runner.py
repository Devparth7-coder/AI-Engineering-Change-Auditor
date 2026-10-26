"""Deterministic test-execution and result-classification service.

Two jobs:

1. **Execute** pytest inside the isolation tier chosen by ``services.sandbox``
   and capture the real exit code, JUnit XML and sanitised output.
2. **Classify** what actually happened -- and refuse to let an unrelated error
   masquerade as a confirmed defect.

The classification rule the product depends on:

============================  ==================================================
Situation                     Verdict
============================  ==================================================
intended test ran and its     REPRODUCED (the defect is real)
own assertion failed
intended test ran and passed  NOT_REPRODUCED
collection error / ImportError EXECUTION_ERROR -- proves nothing about the defect
test errored for an unrelated INCONCLUSIVE (assertion was never reached)
reason (TypeError, fixture)
test skipped or deselected    INCONCLUSIVE -- never treated as success
timed out                     EXECUTION_ERROR
============================  ==================================================
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sandbox import ExecutionRequest, ExecutionResult, execute
from app.config import Settings, get_settings
from app.models import ReproOutcome, TestRunStatus

ASSERTION_HINTS = ("AssertionError", "assert ", "contract requires")
COLLECTION_HINTS = (
    "ModuleNotFoundError",
    "ImportError",
    "IndentationError",
    "SyntaxError",
    "errors during collection",
    "ERROR collecting",
    "no tests ran",
    "file or directory not found",
)
UNRELATED_HINTS = (
    "TypeError",
    "AttributeError",
    "NameError",
    "fixture",
    "ZeroDivisionError",
    "KeyError",
    "IndexError",
)


@dataclass
class TestCaseResult:
    node_id: str
    classname: str
    name: str
    outcome: str  # passed|failed|error|skipped
    message: str = ""
    text: str = ""
    duration: float = 0.0

    @property
    def is_assertion_failure(self) -> bool:
        blob = f"{self.message}\n{self.text}"
        return self.outcome == "failed" and any(h in blob for h in ASSERTION_HINTS)

    @property
    def is_collection_error(self) -> bool:
        blob = f"{self.message}\n{self.text}"
        return self.outcome == "error" and any(h in blob for h in COLLECTION_HINTS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "classname": self.classname,
            "name": self.name,
            "outcome": self.outcome,
            "message": self.message[:600],
            "text": self.text[:1200],
            "duration": self.duration,
            "is_assertion_failure": self.is_assertion_failure,
            "is_collection_error": self.is_collection_error,
        }


@dataclass
class PytestReport:
    exit_code: int | None
    status: TestRunStatus
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    cases: list[TestCaseResult] = field(default_factory=list)
    junit_parsed: bool = False
    parse_error: str = ""
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0
    isolation_level: str = "none"
    network_disabled: bool = True
    workspace: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "status": self.status.value,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
            "cases": [c.to_dict() for c in self.cases],
            "junit_parsed": self.junit_parsed,
            "parse_error": self.parse_error,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "isolation_level": self.isolation_level,
            "network_disabled": self.network_disabled,
            "workspace": self.workspace,
            "notes": self.notes,
        }


def parse_junit(xml_text: str) -> tuple[list[TestCaseResult], dict[str, int], str]:
    """Parse pytest's JUnit XML into per-case results and aggregate counts."""
    cases: list[TestCaseResult] = []
    counts = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    if not xml_text.strip():
        return cases, counts, "empty junit xml"
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return cases, counts, f"junit parse error: {exc}"

    for suite in root.iter("testsuite"):
        for tc in suite.findall("testcase"):
            classname = tc.get("classname", "") or ""
            name = tc.get("name", "") or ""
            node_id = f"{classname}::{name}" if classname else name
            duration = float(tc.get("time", "0") or 0)
            outcome = "passed"
            message = ""
            text = ""
            failure = tc.find("failure")
            error = tc.find("error")
            skipped = tc.find("skipped")
            if failure is not None:
                outcome = "failed"
                message = failure.get("message", "") or ""
                text = failure.text or ""
            elif error is not None:
                outcome = "error"
                message = error.get("message", "") or ""
                text = error.text or ""
            elif skipped is not None:
                outcome = "skipped"
                message = skipped.get("message", "") or ""
                text = skipped.text or ""
            cases.append(
                TestCaseResult(
                    node_id=node_id,
                    classname=classname,
                    name=name,
                    outcome=outcome,
                    message=message,
                    text=text,
                    duration=duration,
                )
            )
    counts["total"] = len(cases)
    counts["passed"] = sum(1 for c in cases if c.outcome == "passed")
    counts["failed"] = sum(1 for c in cases if c.outcome == "failed")
    counts["skipped"] = sum(1 for c in cases if c.outcome == "skipped")
    counts["errors"] = sum(1 for c in cases if c.outcome == "error")
    return cases, counts, ""


def _pytest_argv(
    test_targets: list[str], junit_rel: str, extra: list[str] | None = None
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "--color=no",
        "--strict-markers",
        f"--junit-xml={junit_rel}",
        *(extra or []),
        *test_targets,
    ]


def run_pytest(
    workspace: Path,
    test_targets: list[str],
    *,
    settings: Settings | None = None,
    label: str = "",
    extra_args: list[str] | None = None,
    pythonpath: list[str] | None = None,
    timeout_seconds: int | None = None,
) -> PytestReport:
    """Execute pytest in the sandbox and classify the real outcome."""
    settings = settings or get_settings()
    junit_rel = "artifacts/junit.xml"
    junit_path = workspace / junit_rel
    if junit_path.exists():
        junit_path.unlink()

    env_extra: dict[str, str] = {}
    if pythonpath:
        env_extra["PYTHONPATH"] = ":".join(pythonpath)
    env_extra["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    req = ExecutionRequest(
        argv=_pytest_argv(test_targets, junit_rel, extra_args),
        workspace=workspace,
        timeout_seconds=timeout_seconds or settings.sandbox_timeout_seconds,
        env_extra=env_extra,
        allow_network=settings.sandbox_allow_network,
        memory_mb=settings.sandbox_memory_mb,
        cpu_seconds=settings.sandbox_cpu_seconds,
        max_file_mb=settings.sandbox_max_file_mb,
        max_processes=settings.sandbox_max_processes,
        docker_image=settings.sandbox_docker_image,
        label=label,
    )
    result: ExecutionResult = execute(req, settings)

    xml_text = ""
    if junit_path.exists():
        try:
            xml_text = junit_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            xml_text = ""
            result.notes.append(f"could not read junit xml: {exc}")

    cases, counts, parse_error = parse_junit(xml_text)
    status, notes = _classify(result, cases, counts, parse_error, test_targets)
    try:
        (workspace / "artifacts" / "stdout.log").write_text(result.stdout, encoding="utf-8")
        (workspace / "artifacts" / "stderr.log").write_text(result.stderr, encoding="utf-8")
    except OSError:
        pass

    return PytestReport(
        exit_code=result.exit_code,
        status=status,
        total=counts["total"],
        passed=counts["passed"],
        failed=counts["failed"],
        skipped=counts["skipped"],
        errors=counts["errors"],
        cases=cases,
        junit_parsed=bool(xml_text) and not parse_error,
        parse_error=parse_error,
        stdout=result.stdout,
        stderr=result.stderr,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
        isolation_level=result.isolation_level.value,
        network_disabled=result.network_disabled,
        workspace=str(workspace),
        notes=notes + result.notes,
    )


def _classify(
    result: ExecutionResult,
    cases: list[TestCaseResult],
    counts: dict[str, int],
    parse_error: str,
    test_targets: list[str],
) -> tuple[TestRunStatus, list[str]]:
    """Map raw execution facts onto a TestRunStatus, conservatively."""
    notes: list[str] = []
    if result.timed_out:
        notes.append(
            "execution exceeded the timeout; a timeout is NEVER treated as a passing run"
        )
        return TestRunStatus.TIMEOUT, notes

    combined = f"{result.stdout}\n{result.stderr}"
    if counts["total"] == 0:
        if any(h in combined for h in COLLECTION_HINTS) or parse_error:
            notes.append(
                "no test cases were collected; the intended tests never executed, "
                "so this run proves nothing about the defect"
            )
            return TestRunStatus.NOT_COLLECTED, notes
        if result.exit_code not in (0, None) and not cases:
            notes.append("pytest produced no results and exited non-zero")
            return TestRunStatus.EXECUTION_ERROR, notes
        notes.append("no test cases were reported")
        return TestRunStatus.NOT_COLLECTED, notes

    if counts["errors"] and counts["errors"] == counts["total"]:
        notes.append("every reported case errored (setup/collection), not asserted")
        return TestRunStatus.ERROR, notes
    if counts["errors"]:
        notes.append(f"{counts['errors']} case(s) errored during setup/collection")
    if counts["skipped"] and counts["skipped"] == counts["total"]:
        notes.append(
            "all cases were skipped; a skipped test is NEVER treated as a successful verification"
        )
        return TestRunStatus.SKIPPED, notes
    if counts["failed"]:
        return TestRunStatus.FAILED, notes
    if counts["errors"]:
        return TestRunStatus.ERROR, notes
    if counts["passed"] == counts["total"]:
        return TestRunStatus.PASSED, notes
    return TestRunStatus.ERROR, notes or ["ambiguous result"]


def match_intended(
    cases: list[TestCaseResult], intended_node_ids: list[str]
) -> dict[str, Any]:
    """Check that the tests we meant to run actually ran, and how they fared.

    Matching is by test function name: generated names embed the finding id and
    the provenance, so they are unique within the audit workspace.
    """
    intended_names = [nid.split("::")[-1] for nid in intended_node_ids if "::" in nid]
    executed: list[str] = []
    missing: list[str] = []
    per: dict[str, str] = {}
    for want in intended_names:
        hit = next((c for c in cases if c.name == want), None)
        if hit is None:
            missing.append(want)
        else:
            executed.append(want)
            per[want] = hit.outcome
    assertion_failures = [
        c.name for c in cases if c.name in intended_names and c.is_assertion_failure
    ]
    collection_errors = [c.name for c in cases if c.is_collection_error]
    unrelated = [
        c.name
        for c in cases
        if c.name in intended_names
        and c.outcome in ("failed", "error")
        and not c.is_assertion_failure
        and not c.is_collection_error
    ]
    return {
        "intended": intended_names,
        "executed": executed,
        "missing": missing,
        "all_intended_executed": bool(intended_names) and not missing,
        "outcomes": per,
        "intended_assertion_failures": assertion_failures,
        "collection_errors": collection_errors,
        "unrelated_failures": unrelated,
    }


def classify_reproduction(
    report: PytestReport, intended_node_ids: list[str]
) -> tuple[ReproOutcome, str, dict[str, Any]]:
    """Decide REPRODUCED / NOT_REPRODUCED / INCONCLUSIVE / EXECUTION_ERROR.

    A failing test does not automatically prove the hypothesis: the failure must
    be the *intended assertion*, not an import error, fixture problem or timeout.
    """
    match = match_intended(report.cases, intended_node_ids)
    if report.timed_out:
        return (
            ReproOutcome.EXECUTION_ERROR,
            "Execution timed out. A timeout does not demonstrate the defect.",
            match,
        )
    if report.status in (TestRunStatus.EXECUTION_ERROR, TestRunStatus.NOT_COLLECTED):
        return (
            ReproOutcome.EXECUTION_ERROR,
            "The intended tests never executed "
            f"(status={report.status.value}). No conclusion about the defect is possible.",
            match,
        )
    if not match["all_intended_executed"]:
        return (
            ReproOutcome.INCONCLUSIVE,
            "Only some of the intended tests executed: "
            f"missing={match['missing']}. Result is inconclusive.",
            match,
        )
    if match["collection_errors"]:
        return (
            ReproOutcome.EXECUTION_ERROR,
            f"Collection errors: {match['collection_errors']}.",
            match,
        )
    if match["intended_assertion_failures"] and not match["unrelated_failures"]:
        return (
            ReproOutcome.REPRODUCED,
            "The intended contract assertion failed against the original revision "
            f"({', '.join(match['intended_assertion_failures'])}). "
            "The declared contract and the implementation disagree -- the defect is reproduced.",
            match,
        )
    if match["unrelated_failures"]:
        return (
            ReproOutcome.INCONCLUSIVE,
            "The test failed for a reason unrelated to the intended assertion "
            f"({', '.join(match['unrelated_failures'])}). The failure does not "
            "establish the hypothesis.",
            match,
        )
    if report.status == TestRunStatus.SKIPPED:
        return (
            ReproOutcome.INCONCLUSIVE,
            "All intended tests were skipped; a skip is not a pass.",
            match,
        )
    if report.status == TestRunStatus.PASSED:
        return (
            ReproOutcome.NOT_REPRODUCED,
            "Every intended assertion passed against the original revision. "
            "The hypothesis is not supported by execution; it remains a hypothesis "
            "and must not be reported as a confirmed defect.",
            match,
        )
    return (
        ReproOutcome.INCONCLUSIVE,
        f"Unclassified outcome (status={report.status.value}); treated as inconclusive.",
        match,
    )


def summarise_output(report: PytestReport, limit: int = 4000) -> str:
    """Build the sanitised output summary stored on a TestRun."""
    tail = report.stdout[-limit:] if report.stdout else ""
    err = report.stderr[-limit // 2 :] if report.stderr else ""
    parts = [f"exit_code={report.exit_code}", f"status={report.status.value}"]
    parts.append(
        f"tests: {report.total} total, {report.passed} passed, {report.failed} failed, "
        f"{report.skipped} skipped, {report.errors} errored"
    )
    parts.append(f"isolation={report.isolation_level} network_disabled={report.network_disabled}")
    if report.parse_error:
        parts.append(f"junit: {report.parse_error}")
    for n in report.notes:
        parts.append(f"note: {n}")
    if tail:
        parts.append("--- stdout ---\n" + tail)
    if err.strip():
        parts.append("--- stderr ---\n" + err)
    return "\n".join(parts)


__all__ = [
    "PytestReport",
    "TestCaseResult",
    "classify_reproduction",
    "match_intended",
    "parse_junit",
    "run_pytest",
    "summarise_output",
]
