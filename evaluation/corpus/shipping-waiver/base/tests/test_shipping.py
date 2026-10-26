from src.shipping.core import shipping_cost, validate_rate


def test_validate_rate_bounds():
    assert validate_rate(0.0) and validate_rate(1.0)
    assert not validate_rate(-0.1) and not validate_rate(1.1)


def test_zero_waiver_pays_full_base():
    assert shipping_cost(250.0, 0.0) == 250.0


def test_full_waiver_pays_nothing():
    assert shipping_cost(250.0, 1.0) == 0.0


def test_negative_base_rejected():
    try:
        shipping_cost(-1.0, 0.2)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
