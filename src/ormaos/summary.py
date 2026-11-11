"""The short summary printed on stderr after ``ormaos run``. At most 5 lines.

Facts only. Every line starts with ``ormaos:`` so it can be told apart from
the program's own stderr. The last line gives the commands, with the addresses,
for the next question.
"""

import os
from typing import Any

from ormaos.recording import Record, Recording

PREFIX = "ormaos: "
MAX_LINES = 5


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _short_type(name: str) -> str:
    return name.removeprefix("builtins.")


def display_path(file: str, cwd: str) -> str:
    """The path relative to ``cwd`` when it is inside it, else unchanged."""
    if os.path.isabs(file) and cwd:
        try:
            relative = os.path.relpath(file, cwd)
        except ValueError:
            return file
        if not relative.startswith(".."):
            return relative
    return file


def _place(where: dict[str, Any], cwd: str) -> str:
    return f"{display_path(str(where.get('file', '?')), cwd)}:{where.get('line', '?')}"


def _first_line(recording: Recording) -> str:
    end = recording.end or {}
    parts = [recording.name]
    code = end.get("exit_code")
    if code is None:
        parts.append("no exit code")
    elif end.get("signal"):
        parts.append(f"exit {code} (signal {end['signal']})")
    else:
        parts.append(f"exit {code}")
    if "dur" in end:
        parts.append(f"in {end['dur'] / 1e9:.2f}s")
    head = " ".join(parts)
    if not recording.processes:
        return f"{head}: no Python process recorded"
    exceptions = recording.exceptions
    swallowed = sum(1 for exc in exceptions if exc.get("swallowed"))
    counts = [
        _plural(len(recording.processes), "Python process").replace(
            "processs", "processes"
        ),
        _plural(len(recording.calls), "call"),
        _plural(len(exceptions), "exception"),
    ]
    tail = f" ({swallowed} swallowed)" if exceptions else ""
    events = sum(int(event.get("n", 1)) for event in recording.io)
    if events:
        tail += f", {events} io"
    return f"{head}: {', '.join(counts)}{tail}"


def swallowed_groups(exceptions: list[Record]) -> list[list[Record]]:
    groups: dict[tuple[Any, Any, Any], list[Record]] = {}
    for exc in exceptions:
        if exc.get("swallowed"):
            handled = exc.get("handled", {})
            key = (exc.get("type"), handled.get("file"), handled.get("line"))
            groups.setdefault(key, []).append(exc)
    return sorted(
        groups.values(),
        key=lambda group: (
            not group[0].get("handled", {}).get("user", False),
            -len(group),
        ),
    )


def _swallowed_line(group: list[Record], cwd: str) -> str:
    first = group[0]
    origins = {_place(exc.get("origin", {}), cwd) for exc in group}
    raised = _place(first.get("origin", {}), cwd)
    if len(origins) > 1:
        raised += f" and {_plural(len(origins) - 1, 'other line')}"
    return (
        f"swallowed {len(group)}x {_short_type(str(first.get('type')))} "
        f"at {_place(first.get('handled', {}), cwd)}, raised at {raised} "
        f"[{first.get('addr')}]"
    )


def _uncaught_line(exc: Record, cwd: str) -> str:
    return (
        f"uncaught {_short_type(str(exc.get('type')))} "
        f"raised at {_place(exc.get('origin', {}), cwd)} [{exc.get('addr')}]"
    )


def summarize(recording: Recording, cwd: str = "") -> list[str]:
    exceptions = recording.exceptions
    groups = swallowed_groups(exceptions)
    uncaught = [exc for exc in exceptions if exc.get("end") == "uncaught"]

    highlights: list[tuple[str, str]] = []  # (line, address)
    if groups:
        highlights.append((_swallowed_line(groups[0], cwd), groups[0][0]["addr"]))
    if uncaught:
        highlights.append((_uncaught_line(uncaught[0], cwd), uncaught[0]["addr"]))
    elif len(groups) > 1:
        highlights.append((_swallowed_line(groups[1], cwd), groups[1][0]["addr"]))

    lines = [_first_line(recording)]
    lines.extend(line for line, _addr in highlights)

    missing = recording.not_collected
    if missing:
        first = missing[0]
        more = f" (+{len(missing) - 1} more)" if len(missing) > 1 else ""
        lines.append(f"not collected: {first.get('what')}: {first.get('reason')}{more}")

    following = [f"ormaos exceptions {addr}" for _line, addr in highlights]
    calls = recording.calls
    if calls:
        following.append(f"ormaos tree {calls[0]['addr']}")
    last = f"saved in {display_path(recording.path, cwd)}"
    if following:
        last += f", next: {' | '.join(following)}"
    lines.append(last)
    return [PREFIX + line for line in lines[:MAX_LINES]]
