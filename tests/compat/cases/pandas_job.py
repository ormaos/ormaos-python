"""A pandas job: read a CSV, apply a function to a column, write a CSV."""

import sys

import pandas as pd


def to_number(text):
    try:
        return float(text)
    except ValueError:
        return 0.0  # a bad amount counts as zero


def main(source, target):
    frame = pd.read_csv(source, dtype=str)
    frame["amount"] = frame["amount"].apply(to_number)
    totals = frame.groupby("sku", as_index=False)["amount"].sum()
    totals.to_csv(target, index=False)
    print(totals.to_string(index=False))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
