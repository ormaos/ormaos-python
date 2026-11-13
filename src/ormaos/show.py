"""The overview of one recording, as a JSON document.

Used by ``ormaos show --json`` and by ``ormaos run --json-out``. It is
bounded: counts and groups, never every call. Facts only. It ends with the
addresses for the next question.
"""

import os
from typing import Any

from ormaos.recording import FORMAT, ROOT, Process, Record, Recording, recording_numbers
from ormaos.summary import (
    TAKEN,
    display_path,
    swallowed_groups,
    taken_counts,
    taken_groups,
    taken_phrase,
)

VERSION = 1
MAX_GROUPS = 10
MAX_ADDRESSES = 5


class NotFound(Exception):
    """The recording asked for does not exist."""


def find(ref: str, root: str = ROOT) -> str:
    """The directory of a recording: ``last``, ``r3`` or ``3``."""
    numbers = recording_numbers(root)
    if not numbers:
        raise NotFound(f"no recordings in {root}; make one with `ormaos run -- <cmd>`")
    if ref == "last":
        return os.path.join(root, f"r{numbers[-1]}")
    text = ref.removeprefix("r")
    if not text.isdigit() or int(text) not in numbers:
        known = (
            f"r{numbers[0]}..r{numbers[-1]}" if len(numbers) > 1 else f"r{numbers[0]}"
        )
        raise NotFound(f"no recording {ref!r} in {root} (there: {known})")
    return os.path.join(root, f"r{int(text)}")


def _seconds(nanoseconds: Any) -> float | None:
    if isinstance(nanoseconds, int):
        return round(nanoseconds / 1e9, 6)
    return None


def _where(place: dict[str, Any], cwd: str) -> dict[str, Any]:
    out = {
        "file": display_path(str(place.get("file", "?")), cwd),
        "line": place.get("line"),
        "fn": place.get("fn"),
    }
    if "user" in place:
        out["user_code"] = place["user"]
    return out


def _process(proc: Process) -> dict[str, Any]:
    header = proc.header
    end = proc.end or {}
    return {
        "process": proc.index,
        "parent_process": proc.parent,
        "pid": header.get("pid"),
        "ppid": header.get("ppid"),
        "forked": header.get("forked"),
        "started_by": header.get("started_by"),
        "started": header.get("started"),
        "capabilities": header.get("capabilities"),
        "python": header.get("python"),
        "executable": header.get("executable"),
        "argv": header.get("argv"),
        "complete": proc.end is not None,
        "ended": None
        if proc.end is not None
        else {**(proc.ending or {"signal": None, "by": None}), "lost": proc.lost},
        "ended_text": None if proc.end is not None else proc.ending_text,
        "written_until_s": _seconds(proc.written_until),
        "duration_s": _seconds(end.get("dur")),
        "cpu_user_s": end.get("cpu_user"),
        "cpu_system_s": end.get("cpu_system"),
        "max_rss_kb": end.get("max_rss_kb"),
        "threads": end.get("threads") or len(proc.call_threads),
        "tasks": len(proc.call_tasks),
        "calls": proc.n_calls,
        "exceptions": len(proc.exceptions),
    }


def taken_entry(group: list[Record], cwd: str, addresses: int) -> dict[str, Any]:
    """One origin of exceptions that the interpreter took without a handler."""
    first = group[0]
    return {
        "type": first.get("type"),
        "how": first.get("how"),
        "ended": taken_phrase(first.get("how")),
        "count": len(group),
        "raised_at": _where(first.get("origin", {}), cwd),
        "addresses": [exc["addr"] for exc in group[:addresses]],
    }


def _swallowed(group: list[Record], cwd: str) -> dict[str, Any]:
    first = group[0]
    origins: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for exc in group:
        origin = exc.get("origin", {})
        key = (origin.get("file"), origin.get("line"), origin.get("fn"))
        if key not in origins:
            origins[key] = {**_where(origin, cwd), "count": 0}
        origins[key]["count"] += 1
    return {
        "type": first.get("type"),
        "count": len(group),
        "handled_at": _where(first.get("handled", {}), cwd),
        "raised_at": list(origins.values())[:MAX_GROUPS],
        "addresses": [exc["addr"] for exc in group[:MAX_ADDRESSES]],
    }


def document(recording: Recording, cwd: str = "") -> dict[str, Any]:
    header = recording.header
    end = recording.end or {}
    exceptions = recording.exceptions
    first_call = recording.first_call
    groups = swallowed_groups(exceptions)
    uncaught = [exc for exc in exceptions if exc.get("end") == "uncaught"]

    always: list[Record] = []
    for proc in recording.processes:
        for note in proc.header.get("not_collected", []):
            if note not in always:
                always.append(note)
    not_collected = [
        {"what": n.get("what"), "reason": n.get("reason")}
        for n in recording.not_collected
    ] + [
        {"what": n.get("what"), "reason": n.get("reason"), "always": True}
        for n in always
    ]

    following = [group[0]["addr"] for group in groups[:MAX_ADDRESSES]]
    following += [exc["addr"] for exc in uncaught[:MAX_ADDRESSES]]
    commands = [f"ormaos exceptions {addr}" for addr in following]
    if first_call:
        following.append(first_call)
        commands.append(f"ormaos tree {first_call}")
    io_by_kind = recording.io_counts
    if io_by_kind:
        following.append(recording.name)
        commands.append(f"ormaos io {recording.name}")

    return {
        "kind": "show",
        "version": VERSION,
        "format": FORMAT,
        "recording": recording.name,
        "path": display_path(recording.path, cwd),
        "complete": recording.complete,
        "incomplete": recording.incomplete_reasons,
        "context": {
            "mode": header.get("mode", "run"),
            "command": header.get("command"),
            "cwd": header.get("cwd"),
            "env_names": header.get("env_names"),
            "platform": header.get("platform"),
            "started": header.get("started"),
            "exit_code": end.get("exit_code"),
            "signal": end.get("signal"),
            "duration_s": _seconds(end.get("dur")),
            "cpu_user_s": end.get("cpu_user"),
            "cpu_system_s": end.get("cpu_system"),
            "max_rss_kb": end.get("max_rss_kb"),
            "processes": [_process(proc) for proc in recording.processes],
        },
        "counts": {
            "processes": len(recording.processes),
            "calls": recording.n_calls,
            "exceptions": len(exceptions),
            "swallowed": sum(1 for exc in exceptions if exc.get("swallowed")),
            "uncaught": len(uncaught),
            "taken_by_interpreter": sum(
                1 for exc in exceptions if exc.get("end") == TAKEN
            ),
            "io": sum(io_by_kind.values()),
        },
        "taken_by_interpreter": {
            "counts": taken_counts(exceptions),
            "groups": [
                taken_entry(group, cwd, MAX_ADDRESSES)
                for group in taken_groups(exceptions)[:MAX_GROUPS]
            ],
        },
        "io": io_by_kind,
        "swallowed": [_swallowed(group, cwd) for group in groups[:MAX_GROUPS]],
        "swallowed_groups_not_shown": max(0, len(groups) - MAX_GROUPS),
        "uncaught": [
            {
                "type": exc.get("type"),
                "raised_at": _where(exc.get("origin", {}), cwd),
                "address": exc["addr"],
            }
            for exc in uncaught[:MAX_GROUPS]
        ],
        "not_collected": not_collected,
        "next": following,
        "next_commands": commands,
    }
