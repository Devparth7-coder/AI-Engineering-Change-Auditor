"""Shipping primitives for a benchmark case."""

from __future__ import annotations


def validate_rate(rate: float) -> bool:
    """Return True when ``rate`` is a usable fraction in ``[0, 1]``."""
    return isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0.0 <= float(rate) <= 1.0


def shipping_cost(base_cost: float, waiver_fraction: float) -> float:
    """Return the FINAL shipping cost payable after applying ``waiver_fraction``.

    Contract
    --------
    ``waiver_fraction`` is a fraction in ``[0, 1]``.  The returned value is the amount
    that remains after the waiver is taken off -- not the size of the
    waiver itself.

    Worked example
    --------------
    For a base_cost of 1000 and a waiver_fraction of 0.2 (``waiver_fraction == 0.2``) the final result
    must be 800.0.

    Raises
    ------
    ValueError
        If ``base_cost`` is negative or ``waiver_fraction`` is outside ``[0, 1]``.
    """
    if base_cost < 0:
        raise ValueError("base_cost must not be negative")
    if not validate_rate(waiver_fraction):
        raise ValueError("waiver_fraction must be a fraction in [0, 1]")

    return base_cost * waiver_fraction

