"""The ``ormaos`` command line."""

import argparse
from collections.abc import Sequence

from ormaos import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ormaos",
        description=(
            "Records what happened inside a Python execution and hands it "
            "to the developer and to their coding agent."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ormaos {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
