from src.refunds.core import refund_after_penalty, validate_rate


def test_zero_penalty_refunds_all():
    assert refund_after_penalty(900.0, 0.0) == 900.0


def test_full_penalty_refunds_nothing():
    assert refund_after_penalty(900.0, 1.0) == 0.0
