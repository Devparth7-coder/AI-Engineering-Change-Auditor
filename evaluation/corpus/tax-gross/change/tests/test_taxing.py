from src.taxing.core import gross_price, validate_rate


def test_validate_rate_bounds():
    assert validate_rate(0.18)
    assert not validate_rate(2.0)


def test_zero_tax_is_net():
    assert gross_price(400.0, 0.0) == 400.0


def test_negative_net_rejected():
    try:
        gross_price(-5.0, 0.1)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
