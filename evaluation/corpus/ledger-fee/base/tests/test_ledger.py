from src.ledger.core import remaining_after_fee, validate_rate


def test_zero_fee_keeps_amount():
    assert remaining_after_fee(500.0, 0.0) == 500.0


def test_full_fee_leaves_nothing():
    assert remaining_after_fee(500.0, 1.0) == 0.0


def test_validate_rate_bounds():
    assert not validate_rate(1.5)
