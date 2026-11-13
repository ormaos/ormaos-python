import pytest
from pricing import parse_price, total


@pytest.fixture
def prices():
    return ["10.5", "abc", "2"]


def test_total_skips_bad_prices(prices):
    assert total(prices) == 12.5


@pytest.mark.parametrize("text", ["1", "x"])
def test_parse_price(text):
    assert parse_price(text) in (1.0, None)


def test_raises():
    with pytest.raises(ZeroDivisionError):
        assert 1 / 0
