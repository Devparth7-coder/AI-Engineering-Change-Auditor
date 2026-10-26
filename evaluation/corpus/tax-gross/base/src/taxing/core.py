"""Tax primitives for a benchmark case."""

from __future__ import annotations


def validate_rate(rate: float) -> bool:
    """Return True when ``rate`` is a usable fraction in ``[0, 1]``."""
    return isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0.0 <= float(rate) <= 1.0


def gross_price(net_price: float, tax_rate: float) -> float:
    """Return the GROSS price a customer pays, tax included.

    Contract
    --------
    ``tax_rate`` is a fraction in ``[0, 1]`` added on top of the net price.
    The returned value is the net price plus the tax portion.

    Worked example
    --------------
    For a net price of 1000 and a tax rate of 0.18 (``tax_rate == 0.18``) the
    gross price must be 1180.0.

    Raises
    ------
    ValueError
        If ``net_price`` is negative or ``tax_rate`` is outside ``[0, 1]``.
    """
    if net_price < 0:
        raise ValueError("net_price must not be negative")
    if not validate_rate(tax_rate):
        raise ValueError("tax_rate must be a fraction in [0, 1]")

    return net_price * (1 + tax_rate)

