"""Refactor primitives for a benchmark case."""

from __future__ import annotations


def validate_rate(rate: float) -> bool:
    """Return True when ``rate`` is a usable fraction in ``[0, 1]``."""
    return isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0.0 <= float(rate) <= 1.0


def _deduct(price: float, fraction: float) -> float:
    """Return ``price`` reduced by ``fraction`` of itself."""
    return price - price * fraction


def apply_discount(price: float, discount_rate: float) -> float:
    """Return the FINAL price payable after applying ``discount_rate``.

    Contract
    --------
    ``discount_rate`` is a fraction in ``[0, 1]``; the result is the remainder.

    Worked example
    --------------
    For a price of 1000 and a discount of 0.2 the final price must be 800.0.
    """
    if discount_rate == 0:
        return price
    return _deduct(price, discount_rate)

