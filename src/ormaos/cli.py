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
        usage="ormaos run [--json-out FILE] -- <command> [args...]",
        description=(
            "Runs the command unchanged, records the Python processes it "
            "starts into .ormaos/, prints a summary of at most 5 lines on "
            "stderr, and exits with the command's exit code."
        ),
    )
    run.add_argument(
        "--json-out",
        metavar="FILE",
        help="also write the overview of the recording to FILE, as JSON",
    )
    run.add_argument("command", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    show = commands.add_parser(
        "show",
        help="show the overview of a recording",
        description="Shows the overview of a recording from .ormaos/.",
    )
    show.add_argument(
        "recording",
        nargs="?",
        default="last",
        help="`last` (the default), or a recording such as r3",
    )
    show.add_argument("--json", action="store_true", help="print JSON on stdout")
    return parser


def _show(ref: str, as_json: bool) -> int:
    import json
    import os
    import sys

    from ormaos.recording import load
    from ormaos.show import NotFound, document, find
    from ormaos.summary import PREFIX, summarize

    try:
        recording = load(find(ref))
    except (NotFound, OSError) as err:
        message = str(err) if isinstance(err, NotFound) else type(err).__name__
        if as_json:
            print(json.dumps({"error": message}))
        else:
            print(f"{PREFIX}{message}", file=sys.stderr)
        return 1
    cwd = os.getcwd()
    if as_json:
        print(json.dumps(document(recording, cwd), indent=2))
    else:
        for line in summarize(recording, cwd):
            print(line.removeprefix(PREFIX))
    return 0


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

        return run(command, json_out=args.json_out)
    if args.verb == "show":
        return _show(args.recording, args.json)
    parser.print_help()
    return 0
