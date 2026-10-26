"""Pricing primitives for the discount service.

This module is the bundled demonstration fixture used by the mandatory
end-to-end acceptance test.  The ``feature/refactor-discount`` branch of this
repository contains a *known, deliberately introduced* defect in
:func:`apply_discount` so that the auditor has something real to find,
reproduce, repair and verify.
"""

from __future__ import annotations

TAX_RATE = 0.18


def validate_discount_rate(discount_rate: float) -> bool:
    """Return ``True`` when ``discount_rate`` is a usable fraction in [0, 1].

    The contract is intentionally narrow: a discount rate is expressed as a
    fraction (``0.20`` means 20%), not as a percentage number (``20``).
    """
    if not isinstance(discount_rate, (int, float)):
        return False
    if isinstance(discount_rate, bool):
        return False
    return 0.0 <= float(discount_rate) <= 1.0


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
    if not validate_discount_rate(discount_rate):
        raise ValueError("discount_rate must be a fraction in [0, 1]")

    return price * (1 - discount_rate)


def format_price(amount: float) -> str:
    """Render ``amount`` as a currency string with two decimal places."""
    return f"{amount:,.2f}"


def apply_tiered_discount(price: float, tier: str) -> float:
    """Return the final price for a named volume tier.

    Tiers are independent of :func:`apply_discount` on purpose so that the
    bundled regression suite keeps passing while ``apply_discount`` is broken.
    """
    tiers = {"none": 0.0, "bronze": 0.05, "silver": 0.10, "gold": 0.15}
    if tier not in tiers:
        raise ValueError(f"unknown tier: {tier!r}")
    return price - price * tiers[tier]


def price_with_tax(price: float) -> float:
    """Return ``price`` plus the standard tax rate."""
    if price < 0:
        raise ValueError("price must not be negative")
    return price + price * TAX_RATE
