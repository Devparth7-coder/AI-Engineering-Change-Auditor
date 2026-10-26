"""Pre-existing regression suite shipped with the example repository.

These tests deliberately DO NOT cover :func:`apply_discount`'s return-value
contract.  That coverage gap is part of the demonstration: the auditor is
expected to report "missing tests" as a finding and to generate the missing
reproduction test itself.

Everything here must keep passing both before and after a proposed repair,
which makes this file the regression signal used during verification.
"""

from __future__ import annotations

import pytest

from discount_service.pricing import (
    apply_tiered_discount,
    format_price,
    price_with_tax,
    validate_discount_rate,
)


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        (0.0, True),
        (0.20, True),
        (1.0, True),
        (-0.1, False),
        (1.5, False),
        ("20", False),
        (True, False),
        (None, False),
    ],
)
def test_validate_discount_rate(rate, expected):
    assert validate_discount_rate(rate) is expected


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("none", 1000.0),
        ("bronze", 950.0),
        ("silver", 900.0),
        ("gold", 850.0),
    ],
)
def test_apply_tiered_discount(tier, expected):
    assert apply_tiered_discount(1000.0, tier) == pytest.approx(expected)


def test_apply_tiered_discount_rejects_unknown_tier():
    with pytest.raises(ValueError):
        apply_tiered_discount(1000.0, "platinum")


def test_format_price():
    assert format_price(1234.5) == "1,234.50"
    assert format_price(0) == "0.00"


def test_price_with_tax():
    assert price_with_tax(100.0) == pytest.approx(118.0)


def test_price_with_tax_rejects_negative():
    with pytest.raises(ValueError):
        price_with_tax(-1.0)
