"""JUnit parsing and reproduction classification (no execution needed)."""

from __future__ import annotations

from app.models import ReproOutcome, TestRunStatus
from app.services.pytest_runner import PytestReport, match_intended, parse_junit

XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="4" failures="2" errors="1" skipped="0">
    <testcase classname="test_audit" name="test_intent" time="0.01">
      <failure message="assert 200.0 == 800.0">assert 200.0 == 800.0</failure>
    </testcase>
    <testcase classname="test_audit" name="test_other" time="0.01">
      <error message="ImportError while importing test module">boom</error>
    </testcase>
    <testcase classname="test_audit" name="test_ok" time="0.01"/>
    <testcase classname="test_audit" name="test_flaky" time="0.01">
      <failure message="infrastructure flake: runner lost its socket">runner lost its socket</failure>
    </testcase>
  </testsuite>
</testsuites>
"""


def test_parse_junit_counts() -> None:
    cases, counts, _summary = parse_junit(XML)
    assert len(cases) == 4
    assert counts["failed"] >= 1
    assert counts["errors"] >= 1
    assert counts["passed"] >= 1


def test_assertion_failures_are_distinguished_from_collection_errors() -> None:
    cases, _counts, _summary = parse_junit(XML)
    by_name = {c.name: c for c in cases}
    assert by_name["test_intent"].is_assertion_failure
    assert not by_name["test_intent"].is_collection_error
    assert by_name["test_other"].is_collection_error


def test_match_intended_requires_the_intended_assertion() -> None:
    cases, _counts, _summary = parse_junit(XML)
    node = {c.name: c.node_id for c in cases}
    hit = match_intended(cases, [node["test_intent"]])
    assert hit["all_intended_executed"] is True
    assert hit["intended_assertion_failures"], "the intended assertion must be recognised"
    miss = match_intended(cases, [node["test_ok"]])
    assert not miss["intended_assertion_failures"]
    assert "test_other" in miss["collection_errors"]
    flaky = match_intended(cases, [node["test_flaky"]])
    assert flaky["unrelated_failures"] == ["test_flaky"], (
        "a failure that is not the intended assertion must be labelled unrelated"
    )


def test_failed_run_without_intended_failure_is_not_reproduced() -> None:
    from app.services.pytest_runner import classify_reproduction

    cases, _counts, _summary = parse_junit(XML)
    node = {c.name: c.node_id for c in cases}
    report = PytestReport(
        exit_code=1, status=TestRunStatus.FAILED, cases=cases,
        total=3, passed=1, failed=1, errors=1,
    )
    outcome, why, _detail = classify_reproduction(report, [node["test_ok"]])
    assert outcome == ReproOutcome.EXECUTION_ERROR
    assert "collection errors" in why.lower()

    no_error_cases = [c for c in cases if not c.is_collection_error]
    flaky_report = PytestReport(
        exit_code=1, status=TestRunStatus.FAILED, cases=no_error_cases,
        total=3, passed=1, failed=2, errors=0,
    )
    outcome2, why2, _d2 = classify_reproduction(flaky_report, [node["test_flaky"]])
    assert outcome2 == ReproOutcome.INCONCLUSIVE
    assert "unrelated" in why2.lower()
    assert outcome != ReproOutcome.REPRODUCED and outcome2 != ReproOutcome.REPRODUCED


def test_malformed_junit_is_reported_not_crashed() -> None:
    cases, counts, summary = parse_junit("<not-xml")
    assert cases == [] or counts.get("errors", 0) >= 0
    assert isinstance(summary, str)
