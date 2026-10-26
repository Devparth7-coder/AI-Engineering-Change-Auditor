"""Remaining primitives for a benchmark case."""

from __future__ import annotations


def validate_rate(rate: float) -> bool:
    """Return True when ``rate`` is a usable fraction in ``[0, 1]``."""
    return isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0.0 <= float(rate) <= 1.0


def remaining_after_fee(amount: float, fee_fraction: float) -> float:
    """Return the FINAL remaining amount payable after applying ``fee_fraction``.

    Contract
    --------
    ``fee_fraction`` is a fraction in ``[0, 1]``.  The returned value is the amount
    that remains after the fee is taken off -- not the size of the
    fee itself.

    Worked example
    --------------
    For a amount of 500 and a fee_fraction of 0.1 (``fee_fraction == 0.1``) the final result
    must be 450.0.

    Raises
    ------
    ValueError
        If ``amount`` is negative or ``fee_fraction`` is outside ``[0, 1]``.
    """
    if amount < 0:
        raise ValueError("amount must not be negative")
    if not validate_rate(fee_fraction):
        raise ValueError("fee_fraction must be a fraction in [0, 1]")

    return amount * fee_fraction

