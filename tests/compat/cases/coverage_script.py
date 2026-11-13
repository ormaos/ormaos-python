"""A script measured by coverage.py while Ormaos records it."""


def parse(text):
    try:
        return int(text)
    except ValueError:
        return 0  # not a number counts as zero


def never_called():
    return "not covered"


print("total", sum(parse(item) for item in ["1", "x", "3"]))
