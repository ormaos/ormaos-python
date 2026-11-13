"""The ``ormaos`` command line."""

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any, NoReturn

from ormaos import __version__

_ADDRESS_HELP = (
    "`last` (the default), a recording such as r3, or an address inside one: "
    "r3:c42 (a call), r3:e5 (an exception), r3:i7 (an io event)"
)


class _Parser(argparse.ArgumentParser):
    """With ``--json`` on the command line, usage errors are JSON too."""

    argv: Sequence[str] = ()

    def error(self, message: str) -> NoReturn:
        if "--json" in _Parser.argv:
            from ormaos.verbs import error_document

            doc = error_document(message, ["ormaos --help"])
            print(json.dumps(doc, indent=2))
            raise SystemExit(2)
        super().error(message)


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _not_negative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def _reader(
    commands: Any, name: str, summary: str, description: str, limit: int | None
) -> argparse.ArgumentParser:
    verb: argparse.ArgumentParser = commands.add_parser(
        name, help=summary, description=description
    )
    verb.add_argument("address", nargs="?", default="last", help=_ADDRESS_HELP)
    verb.add_argument("--json", action="store_true", help="print JSON on stdout")
    if limit is not None:
        verb.add_argument(
            "--limit",
            type=_positive,
            default=limit,
            metavar="N",
            help=f"show at most N entries (default {limit})",
        )
        verb.add_argument(
            "--offset",
            type=_not_negative,
            default=None,
            metavar="N",
            help="skip the first N entries",
        )
    return verb


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
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
        usage=(
            "ormaos run [--path SCOPE] [--no-io] [--json-out FILE] "
            "-- <command> [args...]"
        ),
        description=(
            "Runs the command unchanged, records the Python processes it "
            "starts into .ormaos/, prints a summary of at most 5 lines on "
            "stderr, and exits with the command's exit code."
        ),
    )
    run.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="SCOPE",
        help=(
            "also record line by line inside SCOPE: file.py, file.py:function "
            "or function (can be given more than once)"
        ),
    )
    run.add_argument(
        "--no-io",
        action="store_true",
        help="do not record I/O (database, files, subprocesses, HTTP)",
    )
    run.add_argument(
        "--json-out",
        metavar="FILE",
        help="also write the overview of the recording to FILE, as JSON",
    )
    run.add_argument("command", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    _reader(
        commands,
        "show",
        "show the overview of a recording",
        "Shows the overview of a recording from .ormaos/: what ran, the counts, "
        "the swallowed and the uncaught exceptions, and where to look next.",
        None,
    )
    tree = _reader(
        commands,
        "tree",
        "show the calls below an address",
        "Shows the call tree below a recording or a call: the shape of the "
        "arguments and of the result, where each call is, how long it took.",
        30,
    )
    tree.add_argument(
        "--depth",
        type=_not_negative,
        default=2,
        metavar="N",
        help="how many levels below the address (default 2)",
    )
    _reader(
        commands,
        "exceptions",
        "show the exceptions of an address, swallowed first",
        "For a recording or a call: its exceptions, swallowed first. For one "
        "exception: where it was raised, the calls it left, where it was handled.",
        20,
    )
    _reader(
        commands,
        "path",
        "show the lines a call ran, in order",
        "Shows line by line what a call ran, with the calls it made and the "
        "exceptions it handled in between. Lines exist only for calls inside "
        "the scope given to `ormaos run --path`.",
        40,
    )
    io = _reader(
        commands,
        "io",
        "show the I/O of an address: database, files, subprocesses, HTTP",
        "For a recording or a call: what I/O it did, grouped by what was done "
        "and by the line of your code that caused it. For one io event: where "
        "it happened. Statements, paths and URLs are shown without values.",
        20,
    )
    io.add_argument(
        "--kind",
        choices=("sqlite", "file", "subprocess", "http"),
        help="show only this kind of I/O",
    )
    diff = commands.add_parser(
        "diff",
        help="show what two recordings did differently",
        description=(
            "Compares two recordings: the calls, the exceptions and the I/O "
            "that one has and the other has not, or has more of. Events are "
            "matched by where they are in the code (file, function, line) and "
            "by order, never by address. Differences are described, not judged."
        ),
    )
    diff.add_argument("a", help="a recording: r3, 3 or last")
    diff.add_argument("b", help="the other recording")
    diff.add_argument("--json", action="store_true", help="print JSON on stdout")
    diff.add_argument(
        "--limit",
        type=_positive,
        default=20,
        metavar="N",
        help="show at most N differences of each kind (default 20)",
    )
    listing = commands.add_parser(
        "list",
        help="list the recordings, newest first",
        description=(
            "Lists the recordings in .ormaos/, newest first: when, how it ended, "
            "how long, the counts, the command. A recording whose process was "
            "killed or is still running is marked."
        ),
    )
    listing.add_argument("--json", action="store_true", help="print JSON on stdout")
    listing.add_argument(
        "--limit",
        type=_positive,
        default=10,
        metavar="N",
        help="show at most N recordings (default 10)",
    )
    listing.add_argument(
        "--offset",
        type=_not_negative,
        default=0,
        metavar="N",
        help="skip the N newest recordings",
    )
    status = commands.add_parser(
        "status",
        help="what ormaos can do here, and what is in .ormaos/ now",
        description=(
            "Shows the version, whether this Python can record, whether a "
            "recording is running now, how many recordings there are, the last "
            "one, and the ones left incomplete."
        ),
    )
    status.add_argument("--json", action="store_true", help="print JSON on stdout")
    guide = commands.add_parser(
        "guide",
        help="how to use ormaos, for a coding agent (start here)",
        description=(
            "Prints a short guide for a coding agent: when to use Ormaos, where "
            "to start, how to follow addresses, what is not recorded."
        ),
    )
    guide.add_argument("--json", action="store_true", help="print JSON on stdout")
    guide.add_argument(
        "--agents-md",
        action="store_true",
        help="print one line to add to the AGENTS.md of a project, and nothing else",
    )
    guide.add_argument(
        "--write",
        nargs="?",
        const="AGENTS.md",
        metavar="FILE",
        help=(
            "with --agents-md: show the change, then add the line to FILE "
            "(default AGENTS.md). Without this flag no file is touched."
        ),
    )
    return parser


def _guide(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from ormaos.guide import agents_md, guide_document
    from ormaos.summary import PREFIX
    from ormaos.verbs import error_document

    if args.write is not None and not args.agents_md:
        parser.error("--write needs --agents-md")
    if not args.agents_md:
        doc = guide_document()
        print(json.dumps(doc, indent=2) if args.json else doc["text"], end="")
        if args.json:
            print()
        return 0
    try:
        doc = agents_md(args.write)
        content = doc.pop("content", None)
        if not args.json:
            # The change is shown first, then made.
            print(doc["diff"] if args.write is not None else doc["snippet"], end="")
            if args.write is None:
                print()
            sys.stdout.flush()
        if content is not None:
            with open(args.write, "w", encoding="utf-8") as handle:
                handle.write(content)
    except OSError as err:
        message = f"cannot change {args.write} ({type(err).__name__})"
        if args.json:
            print(json.dumps(error_document(message), indent=2))
        else:
            print(PREFIX + message, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(doc, indent=2))
    elif args.write is not None:
        done = f"added the line to {args.write}" if doc["changed"] else doc["note"]
        print(PREFIX + done, file=sys.stderr)
    return 0


def _answer(build: Callable[[], dict[str, Any]], as_json: bool) -> int:
    """Print one answer: JSON or text on stdout; an error in the same form."""
    from ormaos.render import render
    from ormaos.show import NotFound
    from ormaos.summary import PREFIX
    from ormaos.verbs import Problem, error_document

    code = 0
    try:
        doc = build()
    except Problem as err:
        doc, code = error_document(str(err), err.commands), 1
    except NotFound as err:
        doc, code = error_document(str(err), ["ormaos run -- <command>"]), 1
    except OSError as err:
        doc, code = (
            error_document(f"cannot read the recording ({type(err).__name__})"),
            1,
        )
    if as_json:
        print(json.dumps(doc, indent=2))
    elif code:
        for line in render(doc):
            print(PREFIX + line, file=sys.stderr)
    else:
        for line in render(doc):
            print(line)
    return code


def _read(args: argparse.Namespace) -> int:
    from ormaos import verbs
    from ormaos.show import document

    cwd = os.getcwd()

    def build() -> dict[str, Any]:
        target = verbs.resolve(args.address)
        if args.verb == "show":
            if target.kind != "recording":
                raise verbs.Problem(
                    f"`show` takes a recording; {target.address} is inside one",
                    [f"ormaos show {target.recording.name}"],
                )
            return document(target.recording, cwd)
        if args.verb == "tree":
            return verbs.tree(target, cwd, args.limit, args.depth, args.offset or 0)
        if args.verb == "exceptions":
            return verbs.exceptions(target, cwd, args.limit, args.offset or 0)
        if args.verb == "io":
            return verbs.io(target, cwd, args.limit, args.offset or 0, args.kind)
        return verbs.path(target, cwd, args.limit, args.offset)

    return _answer(build, args.json)


def _diff(args: argparse.Namespace) -> int:
    from ormaos import verbs
    from ormaos.diff import diff

    def build() -> dict[str, Any]:
        sides = []
        names = [ref.partition(":")[0] for ref in (args.a, args.b)]
        for ref in (args.a, args.b):
            target = verbs.resolve(ref)
            if target.kind != "recording":
                raise verbs.Problem(
                    f"`diff` takes two recordings; {target.address} is inside one",
                    [f"ormaos diff {names[0]} {names[1]}"],
                )
            sides.append(target.recording)
        return diff(sides[0], sides[1], args.limit)

    return _answer(build, args.json)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    _Parser.argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)
    if args.verb == "run":
        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("run needs a command: ormaos run -- <command> [args...]")
        from ormaos.runner import run

        return run(command, json_out=args.json_out, path=args.path, io=not args.no_io)
    if args.verb in ("show", "tree", "exceptions", "path", "io"):
        return _read(args)
    if args.verb == "diff":
        return _diff(args)
    if args.verb == "guide":
        return _guide(args, parser)
    if args.verb == "list":
        from ormaos.listing import list_document

        return _answer(lambda: list_document(args.limit, args.offset), args.json)
    if args.verb == "status":
        from ormaos.listing import status_document

        return _answer(status_document, args.json)
    parser.print_help()
    return 0
