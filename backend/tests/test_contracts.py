"""Contract extraction from docstrings: examples, semantics, completeness."""

from __future__ import annotations

from app.services.analysis.astutil import functions_of, parse_source
from app.services.analysis.contracts import extract_contract

SOURCE = '''
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
        If ``price`` is negative.
    """
    return price * (1 - discount_rate)


def undocumented(x: float) -> float:
    return x * 2
'''


def _contract(name: str):
    tree = parse_source(SOURCE)
    fn = next(f for f in functions_of(tree) if f.name == name)
    return extract_contract(fn, "src/x.py", SOURCE.splitlines())


def test_worked_example_is_parsed() -> None:
    contract = _contract("apply_discount")
    assert contract.has_expectation
    example = contract.examples[0]
    blob = repr(example)
    assert "1000" in blob and "800" in blob


def test_return_semantics_classified_as_final() -> None:
    contract = _contract("apply_discount")
    assert "final" in str(contract.return_semantics).lower() or "net" in str(
        contract.return_semantics
    ).lower()
    assert contract.semantics_evidence, "semantics must cite their evidence"


def test_raises_section_captured() -> None:
    assert _contract("apply_discount").raises


def test_missing_contract_is_reported_not_guessed() -> None:
    contract = _contract("undocumented")
    assert not contract.has_expectation
    assert contract.completeness, "missing contract parts must be listed"
