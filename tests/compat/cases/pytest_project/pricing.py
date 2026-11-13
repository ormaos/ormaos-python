"""A small module with tests, run by pytest."""


def parse_price(text):
    try:
        return float(text)
    except ValueError:
        return None  # a bad price is dropped


def total(prices):
    return sum(price for price in map(parse_price, prices) if price is not None)
