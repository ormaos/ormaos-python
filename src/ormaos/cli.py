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
    commands = parser.add_subparsers(dest="verb", metavar="<verb>")
    run = commands.add_parser(
        "run",
        help="run a command and record the Python processes it starts",
        usage="ormaos run -- <command> [args...]",
        description=(
            "Runs the command unchanged, records the Python processes it "
            "starts into .ormaos/, prints a summary of at most 5 lines on "
            "stderr, and exits with the command's exit code."
        ),
    )
    run.add_argument("command", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verb == "run":
        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("run needs a command: ormaos run -- <command> [args...]")
        from ormaos.runner import run

        return run(command)
    parser.print_help()
    return 0
