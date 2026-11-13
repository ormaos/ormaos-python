"""``ormaos list`` and ``ormaos status``.

``list``: the recordings in ``.ormaos/``, newest first, one line each.
``status``: what Ormaos can do here and what is in ``.ormaos/`` right now.
Facts only; both end with the commands for the next question.
"""

import os
import sys
import time
from typing import Any

from ormaos import __version__
from ormaos.recording import ROOT, Recording, load, recording_numbers
from ormaos.show import VERSION

STATUS_LOOKS_AT = 50


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists and is not ours
    return True


def _entry(recording: Recording) -> dict[str, Any]:
    header = recording.header
    end = recording.end or {}
    started = header.get("started")
    text = None
    if isinstance(started, int | float):
        text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
    exceptions = recording.exceptions
    dur = end.get("dur")
    return {
        "recording": recording.name,
        "started": started,
        "started_text": text,
        "mode": header.get("mode", "run"),
        "command": header.get("command"),
        "exit_code": end.get("exit_code"),
        "signal": end.get("signal"),
        "duration_s": round(dur / 1e9, 6) if isinstance(dur, int) else None,
        "complete": recording.complete,
        "running": recording.end is None and _alive(header.get("pid")),
        "processes": len(recording.processes),
        "calls": recording.n_calls,
        "exceptions": len(exceptions),
        "swallowed": sum(1 for exc in exceptions if exc.get("swallowed")),
        "io": sum(recording.io_counts.values()),
    }


def _try_entry(root: str, number: int) -> dict[str, Any]:
    """One recording of the list; one that cannot be read does not hide the
    others."""
    try:
        return _entry(load(os.path.join(root, f"r{number}"), overview=True))
    except Exception as err:
        return {"recording": f"r{number}", "reason": type(err).__name__}


def list_document(limit: int, offset: int, root: str = ROOT) -> dict[str, Any]:
    numbers = recording_numbers(root)[::-1]
    shown = numbers[offset : offset + limit]
    entries: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []
    for number in shown:
        entry = _try_entry(root, number)
        (unreadable if "reason" in entry else entries).append(entry)
    commands = [f"ormaos show {entry['recording']}" for entry in entries[:3]]
    rest = len(numbers) - offset - len(shown)
    if rest > 0:
        commands.append(f"ormaos list --offset {offset + len(shown)}")
    if len(entries) > 1:
        commands.append(
            f"ormaos diff {entries[1]['recording']} {entries[0]['recording']}"
        )
    if not numbers:
        commands = ["ormaos run -- <command>"]
    return {
        "kind": "list",
        "version": VERSION,
        "root": root,
        "total": len(numbers),
        "limit": limit,
        "offset": offset,
        "not_shown": max(0, rest),
        "recordings": entries,
        "unreadable": unreadable,
        "next": [entry["recording"] for entry in entries[:3]],
        "next_commands": commands,
    }


def _pth_installed() -> bool:
    """Is ``ormaos.pth`` where this Python reads it at start?"""
    return any(os.path.isfile(os.path.join(entry, "ormaos.pth")) for entry in sys.path)


def status_document(root: str = ROOT) -> dict[str, Any]:
    numbers = recording_numbers(root)
    running: list[str] = []
    incomplete: list[str] = []
    last: dict[str, Any] | None = None
    unreadable: list[dict[str, Any]] = []
    for number in numbers[::-1][:STATUS_LOOKS_AT]:
        entry = _try_entry(root, number)
        if "reason" in entry:
            unreadable.append(entry)
            continue
        if last is None:
            last = entry
        if entry["running"]:
            running.append(entry["recording"])
        elif not entry["complete"]:
            incomplete.append(entry["recording"])
    now = os.environ.get("ORMAOS_DIR")
    commands = []
    if last is not None:
        commands = [f"ormaos show {last['recording']}", "ormaos list"]
    else:
        commands = ["ormaos run -- <command>", "ormaos guide"]
    return {
        "kind": "status",
        "version": VERSION,
        "ormaos": __version__,
        "python": ".".join(str(n) for n in sys.version_info[:3]),
        "executable": sys.executable,
        "can_record": hasattr(sys, "monitoring"),
        "starts_in_child_processes": _pth_installed(),
        "recording_now": os.path.basename(now) if now and os.path.isdir(now) else None,
        "root": root,
        "recordings": len(numbers),
        "looked_at": min(len(numbers), STATUS_LOOKS_AT),
        "last": last,
        "running": running,
        "incomplete": incomplete,
        "unreadable": unreadable,
        "next": [last["recording"]] if last is not None else [],
        "next_commands": commands,
    }
