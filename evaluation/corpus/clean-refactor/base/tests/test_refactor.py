from src.refactor.core import apply_discount


def test_discount_remainder():
    assert apply_discount(1000.0, 0.2) == 800.0


def test_zero_rate():
    assert apply_discount(77.0, 0.0) == 77.0


def test_full_rate():
    assert apply_discount(77.0, 1.0) == 0.0
