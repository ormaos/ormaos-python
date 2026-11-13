"""A script that records one block of itself with `with ormaos.record():`."""

import ormaos


def parse(text):
    try:
        return int(text)
    except ValueError:
        return 0  # not a number counts as zero


def setup():
    return ["1", "x", "3"]


items = setup()
with ormaos.record():
    print("total", sum(parse(item) for item in items))
print("done")
