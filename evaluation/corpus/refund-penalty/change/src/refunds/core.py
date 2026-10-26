"""Refund primitives for a benchmark case."""

from __future__ import annotations


def validate_rate(rate: float) -> bool:
    """Return True when ``rate`` is a usable fraction in ``[0, 1]``."""
    return isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0.0 <= float(rate) <= 1.0


def refund_after_penalty(payment: float, penalty_fraction: float) -> float:
    """Return the FINAL refund payable after applying ``penalty_fraction``.

    Contract
    --------
    ``penalty_fraction`` is a fraction in ``[0, 1]``.  The returned value is the amount
    that remains after the penalty is taken off -- not the size of the
    penalty itself.

    Worked example
    --------------
    For a payment of 900 and a penalty_fraction of 0.25 (``penalty_fraction == 0.25``) the final result
    must be 675.0.

    Raises
    ------
    ValueError
        If ``payment`` is negative or ``penalty_fraction`` is outside ``[0, 1]``.
    """
    if payment < 0:
        raise ValueError("payment must not be negative")
    if not validate_rate(penalty_fraction):
        raise ValueError("penalty_fraction must be a fraction in [0, 1]")

    return payment * penalty_fraction

