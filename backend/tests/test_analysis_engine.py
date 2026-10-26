"""Deterministic analysis engine: contracts, evidence, dedupe, limits."""

from __future__ import annotations

from app.models import Severity
from app.services.analysis.engine import ChangeBundle, analyze
from app.services.analysis.rules import RULE_IDS
from app.services.analysis.rules.base import EVIDENCE_CHECKS

PATH = "src/shipping/pricing.py"

DOC = '''"""Shipping pricing primitives."""

from __future__ import annotations


def validate_rate(rate: float) -> bool:
    """Return True when rate is a usable fraction in [0, 1]."""
    return isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0.0 <= float(rate) <= 1.0


def apply_discount(price: float, discount_rate: float) -> float:
    """Return the FINAL price payable after applying ``discount_rate``.

    Contract
    --------
    ``discount_rate`` is a fraction in ``[0, 1]``.  The returned value is the
    amount the customer actually pays, i.e. the price *remaining* after the
    discount is taken off -- not the size of the discount itself.

    Worked example
    --------------
    For a price of 1000 and a discount of 20% (``discount_rate == 0.20``) the
    final price must be 800.

    Raises
    ------
    ValueError
        If ``price`` is negative or ``discount_rate`` is outside ``[0, 1]``.
    """
    if price < 0:
        raise ValueError("price must not be negative")
    if not validate_rate(discount_rate):
        raise ValueError("discount_rate must be a fraction in [0, 1]")

    return price * (1 - discount_rate)
'''

DEFECT = DOC.replace(
    "    return price * (1 - discount_rate)", "    return price * discount_rate"
)

CLEAN_CHANGE = DOC + '''

def format_price(amount: float) -> str:
    """Render ``amount`` with two decimal places."""
    return f"{amount:,.2f}"
'''


def bundle(old: str, new: str) -> ChangeBundle:
    return ChangeBundle(
        source_commit_sha="a" * 40,
        target_commit_sha="b" * 40,
        source_branch="feature/x",
        target_branch="main",
        old_sources={PATH: old},
        new_sources={PATH: new},
        test_sources={},
        mode="unit_test",
    )


def test_contract_violation_is_detected() -> None:
    result = analyze(bundle(DOC, DEFECT))
    ids = [d.rule_id for d in result.findings]
    assert "R-ARITH-001" in ids
    draft = next(d for d in result.findings if d.rule_id == "R-ARITH-001")
    assert draft.severity == Severity.CRITICAL
    assert draft.symbol == "apply_discount"
    assert "800" in draft.expected_behavior  # derived from the worked example


def test_facts_and_hypotheses_stay_separate() -> None:
    result = analyze(bundle(DOC, DEFECT))
    draft = next(d for d in result.findings if d.rule_id == "R-ARITH-001")
    assert draft.facts, "observed facts must be recorded"
    assert draft.hypotheses, "interpretations must be labelled as hypotheses"
    assert draft.evidence, "a finding must carry checkable evidence"
    for item in draft.evidence:
        assert item.check in EVIDENCE_CHECKS


def test_no_invented_confidence() -> None:
    result = analyze(bundle(DOC, DEFECT))
    for draft in result.findings:
        blob = repr(draft.confidence).lower()
        assert "accuracy" not in blob
        assert "0.9" not in blob  # no fabricated probability anywhere


def test_clean_change_produces_no_critical_finding() -> None:
    result = analyze(bundle(DOC, CLEAN_CHANGE))
    assert not [d for d in result.findings if d.severity == Severity.CRITICAL]


def test_every_rule_runs_and_reports_errors() -> None:
    result = analyze(bundle(DOC, DEFECT))
    assert set(RULE_IDS) <= set(result.rules_run)
    assert result.rule_errors == {}
    assert result.files_analyzed >= 1


def test_findings_are_deduplicated() -> None:
    result = analyze(bundle(DOC, DEFECT))
    keys = [(d.rule_id, d.file_path, d.symbol) for d in result.findings]
    assert len(keys) == len(set(keys))


def test_max_findings_is_enforced() -> None:
    result = analyze(bundle(DOC, DEFECT), max_findings=1)
    assert len(result.findings) <= 1


def test_result_metadata_is_honest_about_engine() -> None:
    result = analyze(bundle(DOC, DEFECT))
    payload = result.to_dict()
    assert payload["engine"] == "local_deterministic"
    assert payload["finding_count"] == len(result.findings)
