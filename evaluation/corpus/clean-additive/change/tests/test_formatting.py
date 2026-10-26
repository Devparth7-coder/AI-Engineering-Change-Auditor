from src.formatting.core import apply_discount


def test_discount_remainder():
    assert apply_discount(1000.0, 0.2) == 800.0


def test_discount_zero():
    assert apply_discount(120.0, 0.0) == 120.0


def test_compact_format():
    from src.formatting.core import format_price_compact
    assert format_price_compact(1234.5) == "1234.50"
