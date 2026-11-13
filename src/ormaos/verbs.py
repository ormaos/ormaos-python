"""The verbs that go inside a recording: ``tree``, ``exceptions``, ``path``.

Each function takes an address and returns one JSON document. Every document
is bounded (a limit, and how to ask for more), states facts only, carries a
``version``, and ends with ``next``: the addresses for the next question, and
``next_commands``: the same as commands.
"""

import os
import re
import shlex
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ormaos.recording import ROOT, Record, Recording, load
from ormaos.show import NotFound, find, taken_entry
from ormaos.summary import (
    TAKEN,
    display_path,
    taken_counts,
    taken_groups,
    taken_phrase,
)

VERSION = 1
MAX_NEXT = 5
MAX_THROUGH = 20
MAX_TAKEN_ADDRESSES = 1000

_ITEM = re.compile(r"([cei])(\d+)")
_KINDS = {"c": "call", "e": "exception", "i": "io"}
_ADDRESS = re.compile(r"r\d+(?::[cei]\d+)?")


class Problem(Exception):
    """The question cannot be answered. ``commands`` say what to try."""

    def __init__(self, message: str, commands: list[str] | None = None) -> None:
        super().__init__(message)
        self.commands = commands or []


@dataclass
class Target:
    recording: Recording
    kind: str  # "recording", "call", "exception" or "io"
    record: Record
    address: str


class Index:
    """A recording, indexed by address."""

    def __init__(self, recording: Recording) -> None:
        self.recording = recording
        self.calls: dict[str, Record] = {}
        self.children: dict[str, list[Record]] = defaultdict(list)
        self.roots: list[Record] = []
        for call in recording.calls:
            self.calls[call["addr"]] = call
        for call in recording.calls:
            parent = call.get("parent_addr")
            if parent in self.calls:
                self.children[parent].append(call)
            else:
                self.roots.append(call)
        self.exceptions: dict[str, Record] = {
            exc["addr"]: exc for exc in recording.exceptions
        }
        self.handled_in: dict[str, list[Record]] = defaultdict(list)
        for exc in recording.exceptions:
            if exc.get("handled_addr"):
                self.handled_in[exc["handled_addr"]].append(exc)
        self.io: dict[str, Record] = {event["addr"]: event for event in recording.io}
        self.io_in: dict[str, list[Record]] = defaultdict(list)
        for event in recording.io:
            if event.get("call_addr"):
                self.io_in[event["call_addr"]].append(event)

    def subtree(self, address: str) -> list[str]:
        """The call and every call below it, in call order."""
        out = []
        todo = [address]
        while todo:
            addr = todo.pop()
            out.append(addr)
            todo.extend(kid["addr"] for kid in reversed(self.children.get(addr, [])))
        return out

    def io_count(self, addresses: list[str]) -> int:
        """How many I/O events happened directly in these calls."""
        return sum(
            int(event.get("n", 1))
            for address in addresses
            for event in self.io_in.get(address, [])
        )

    def ancestors(self, call: Record) -> list[Record]:
        """From the top of the tree down to the parent of ``call``."""
        out: list[Record] = []
        parent = self.calls.get(call.get("parent_addr") or "")
        while parent is not None and len(out) < 1000:
            out.append(parent)
            parent = self.calls.get(parent.get("parent_addr") or "")
        return out[::-1]


def resolve(ref: str, root: str = ROOT) -> Target:
    """``last``, ``r3``, ``3``, ``r3:c42``, ``r3:e5`` or ``r3:i7``."""
    name, _, item = ref.partition(":")
    recording = load(find(name, root))
    if not item:
        return Target(recording, "recording", {}, recording.name)
    match = _ITEM.fullmatch(item)
    if match is None:
        raise Problem(
            f"{ref!r} is not an address: use a recording (last, {recording.name}) "
            f"or {recording.name}:c<N> (call), {recording.name}:e<N> (exception), "
            f"{recording.name}:i<N> (io)",
            [f"ormaos show {recording.name}"],
        )
    kind = _KINDS[match.group(1)]
    address = f"{recording.name}:{item}"
    records = {
        "call": recording.calls,
        "exception": recording.exceptions,
        "io": recording.io,
    }[kind]
    for record in records:
        if record["addr"] == address:
            return Target(recording, kind, record, address)
    there = f"{records[0]['addr']}..{records[-1]['addr']}" if records else f"no {kind}s"
    raise Problem(
        f"no {kind} {address} in {recording.name} (there: {there})",
        [f"ormaos show {recording.name}"],
    )


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


def _brief(call: Record, cwd: str) -> dict[str, Any]:
    return {"address": call["addr"], **_where(call, cwd)}


def _finish(doc: dict[str, Any], commands: list[str]) -> dict[str, Any]:
    """Add ``next`` (addresses) and ``next_commands``, without repeats."""
    unique = list(dict.fromkeys(commands))[:MAX_NEXT]
    addresses: list[str] = []
    for command in unique:
        for address in _ADDRESS.findall(command):
            if address not in addresses:
                addresses.append(address)
    doc["next"] = addresses
    doc["next_commands"] = unique
    return doc


def _base(kind: str, target: Target) -> dict[str, Any]:
    return {
        "kind": kind,
        "version": VERSION,
        "recording": target.recording.name,
        "address": target.address,
    }


def error_document(message: str, commands: list[str] | None = None) -> dict[str, Any]:
    return _finish(
        {"kind": "error", "version": VERSION, "error": message}, commands or []
    )


def _call_of(target: Target, ix: Index) -> Record | None:
    """The call an address stands for: itself, or the call it happened in."""
    if target.kind == "call":
        return target.record
    if target.kind == "exception":
        addr = target.record.get("handled_addr") or target.record.get("call_addr")
        return ix.calls.get(addr or "")
    if target.kind == "io":
        return ix.calls.get(target.record.get("call_addr") or "")
    return None


# ------------------------------------------------------------------- tree


def _status(exc: Record) -> str:
    if exc.get("swallowed"):
        return "swallowed"
    if exc.get("end") == "uncaught":
        return "uncaught"
    if "replaced_by" in exc:
        return "replaced"
    if exc.get("end") == "handled":
        return "handled"
    if exc.get("end") == TAKEN:
        return taken_phrase(exc.get("how"))
    if exc.get("type") == "SystemExit":
        return "exit"  # `sys.exit()`: it ends the program, nothing handles it
    return "no handler seen"


def _node(ix: Index, call: Record, cwd: str, depth: int) -> dict[str, Any]:
    addr = call["addr"]
    node: dict[str, Any] = {
        **_brief(call, cwd),
        "depth": depth,
        "args": call.get("args", {}),
        "out": call.get("out"),
        "duration_s": _seconds(call.get("dur")),
        "thread": call.get("thread"),
        "process": call.get("process"),
        "calls_below": len(ix.children.get(addr, [])),
    }
    if "task" in call:
        node["task"] = call["task"]
    if "ret" in call:
        node["ret"] = call["ret"]
    groups: dict[tuple[Any, str], list[Record]] = {}
    for exc in ix.handled_in.get(addr, []):
        groups.setdefault((exc.get("type"), _status(exc)), []).append(exc)
    if groups:
        node["handled_here"] = [
            {
                "type": kind,
                "status": status,
                "count": len(group),
                "address": group[0]["addr"],
            }
            for (kind, status), group in groups.items()
        ]
    if "path" in call:
        node["path_steps"] = len(call["path"])
    events = ix.io_count(ix.subtree(addr))
    if events:
        node["io"] = events
    return node


def _rest(ix: Index, parent: str, kids: list[Record], cwd: str) -> dict[str, Any]:
    by_fn: dict[str, int] = {}
    for kid in kids:
        name = str(kid.get("fn"))
        by_fn[name] = by_fn.get(name, 0) + 1
    shown = (
        len(ix.children[parent]) - len(kids) if parent else len(ix.roots) - len(kids)
    )
    return {
        "parent": parent or None,
        "total": shown + len(kids),
        "not_shown": len(kids),
        "by_fn": dict(sorted(by_fn.items(), key=lambda item: -item[1])[:5]),
        "raised": sum(1 for kid in kids if kid.get("out") == "raised"),
        "offset": shown,
    }


def tree(
    target: Target, cwd: str = "", limit: int = 30, depth: int = 2, offset: int = 0
) -> dict[str, Any]:
    """The calls below an address, down to ``depth``, at most ``limit``."""
    ix = Index(target.recording)
    name = target.recording.name
    root = _call_of(target, ix)
    if root is None and target.kind != "recording":
        raise Problem(
            f"{target.address} did not happen inside a recorded call",
            [f"ormaos tree {name}"],
        )
    nodes: list[dict[str, Any]] = []
    cut: list[dict[str, Any]] = []

    def visit(call: Record, level: int) -> None:
        nodes.append(_node(ix, call, cwd, level))
        if level >= depth:
            return
        kids = ix.children.get(call["addr"], [])
        start = offset if call is root else 0
        walk(call["addr"], kids, start, level + 1)

    def walk(parent: str, kids: list[Record], start: int, level: int) -> None:
        for position in range(start, len(kids)):
            if len(nodes) >= limit:
                cut.append(_rest(ix, parent, kids[position:], cwd))
                return
            visit(kids[position], level)

    doc = _base("tree", target)
    commands: list[str] = []
    if root is None:
        doc["root"] = None
        doc["ancestors"] = []
        if offset >= len(ix.roots) > 0:
            raise Problem(
                f"{name} has {len(ix.roots)} top calls; --offset is past them"
            )
        walk("", ix.roots, offset, 0)
        total = len(ix.roots)
    else:
        kids = ix.children.get(root["addr"], [])
        if offset and offset >= len(kids):
            raise Problem(
                f"{root['addr']} has {len(kids)} calls below it; --offset is past them",
                [f"ormaos tree {root['addr']}"],
            )
        doc["root"] = root["addr"]
        doc["ancestors"] = [_brief(call, cwd) for call in ix.ancestors(root)[-10:]]
        visit(root, 0)
        total = len(kids)
    doc.update(
        {
            "depth": depth,
            "limit": limit,
            "offset": offset,
            "calls_below_root": total,
            "nodes": nodes,
            "not_shown": cut[::-1],
        }
    )
    scope = root["addr"] if root is not None else name
    for node in nodes:
        for group in node.get("handled_here", []):
            commands.append(f"ormaos exceptions {group['address']}")
    for rest in cut[::-1]:
        commands.append(
            f"ormaos tree {rest['parent'] or name} --offset {rest['offset']}"
        )
    if root is not None and "path" in root:
        commands.append(f"ormaos path {root['addr']}")
    deeper = [n for n in nodes if n["depth"] == depth and n["calls_below"]]
    if deeper:
        commands.append(f"ormaos tree {deeper[0]['address']}")
    if root is not None and root.get("parent_addr"):
        commands.append(f"ormaos tree {root['parent_addr']}")
    commands.append(f"ormaos exceptions {scope}")
    return _finish(doc, commands)


# ------------------------------------------------------------- exceptions


def _exception_entry(exc: Record, cwd: str) -> dict[str, Any]:
    handled = exc.get("handled")
    entry = _exception_fields(exc, cwd, handled)
    if "task" in exc:
        entry["task"] = exc["task"]
    return entry


def _exception_fields(exc: Record, cwd: str, handled: Any) -> dict[str, Any]:
    return {
        "address": exc["addr"],
        "type": exc.get("type"),
        "status": _status(exc),
        "raised_at": _where(exc.get("origin", {}), cwd),
        "handled_at": _where(handled, cwd) if handled else None,
        "raised_in": exc.get("call_addr"),
        "handled_in": exc.get("handled_addr"),
        "thread": exc.get("thread"),
        "process": exc.get("process"),
    }


def _rank(exc: Record) -> int:
    """Swallowed first: in user code, then elsewhere; then the others."""
    if exc.get("swallowed"):
        return 0 if exc.get("handled", {}).get("user") else 1
    return 2 if exc.get("end") == "uncaught" else 3


def _same_group(exc: Record, exceptions: list[Record]) -> list[Record]:
    handled = exc.get("handled", {})
    key = (exc.get("type"), _status(exc), handled.get("file"), handled.get("line"))
    out = []
    for other in exceptions:
        place = other.get("handled", {})
        if (
            other.get("type"),
            _status(other),
            place.get("file"),
            place.get("line"),
        ) == key:
            out.append(other)
    return out


def _exception_detail(target: Target, ix: Index, cwd: str) -> dict[str, Any]:
    exc = target.record
    doc = _base("exception", target)
    doc.update(_exception_entry(exc, cwd))
    doc["args"] = exc.get("args")
    doc["message"] = "not recorded (values are never recorded)"
    doc["seen_at"] = _where(exc.get("at", {}), cwd)
    doc["t_s"] = _seconds(exc.get("t"))
    through: list[dict[str, Any]] = []
    call = ix.calls.get(exc.get("call_addr") or "")
    stop = exc.get("handled_addr")
    while call is not None and call["addr"] != stop and len(through) < MAX_THROUGH:
        through.append(_brief(call, cwd))
        call = ix.calls.get(call.get("parent_addr") or "")
    doc["left_calls"] = through
    for key in ("during", "replaced_by"):
        if exc.get(f"{key}_addr"):
            doc[key] = exc[f"{key}_addr"]
    group = _same_group(exc, target.recording.exceptions)
    doc["same_type_and_handler"] = {
        "count": len(group),
        "addresses": [other["addr"] for other in group[:MAX_NEXT]],
    }
    commands = []
    handled_in = exc.get("handled_addr")
    if handled_in:
        handler = ix.calls[handled_in]
        if "path" in handler:
            commands.append(f"ormaos path {exc['addr']}")
        commands.append(f"ormaos tree {handled_in}")
        if ix.io_count(ix.subtree(handled_in)):
            commands.append(f"ormaos io {handled_in}")
    if through:
        commands.append(f"ormaos tree {through[0]['address']}")
    for key in ("replaced_by", "during"):
        if key in doc:
            commands.append(f"ormaos exceptions {doc[key]}")
    others = [other["addr"] for other in group if other is not exc]
    if others:
        commands.append(f"ormaos exceptions {others[0]}")
    if handled_in and "path" not in ix.calls[handled_in]:
        commands.append(f"ormaos path {exc['addr']}")
    return _finish(doc, commands)


def exceptions(
    target: Target, cwd: str = "", limit: int = 20, offset: int = 0
) -> dict[str, Any]:
    """One exception in full, or the exceptions of a recording or of a call
    and the calls below it: swallowed first."""
    ix = Index(target.recording)
    if target.kind == "exception":
        return _exception_detail(target, ix, cwd)
    found = target.recording.exceptions
    call = _call_of(target, ix)
    if target.kind != "recording":
        if call is None:
            raise Problem(
                f"{target.address} did not happen inside a recorded call",
                [f"ormaos exceptions {target.recording.name}"],
            )
        inside = set(ix.subtree(call["addr"]))
        found = [
            exc
            for exc in found
            if exc.get("call_addr") in inside or exc.get("handled_addr") in inside
        ]
    # What the interpreter took without a handler (the `StopIteration` that
    # ends an `await`) is counted and grouped by origin, not listed one by one.
    taken = [exc for exc in found if exc.get("end") == TAKEN]
    listed = [exc for exc in found if exc.get("end") != TAKEN]
    ordered = sorted(listed, key=_rank)  # stable: recording order inside a rank
    page = ordered[offset : offset + limit]
    doc = _base("exceptions", target)
    doc.update(
        {
            "scope": _brief(call, cwd) if call is not None else None,
            "total": len(found),
            "swallowed": sum(1 for exc in found if exc.get("swallowed")),
            "uncaught": sum(1 for exc in found if exc.get("end") == "uncaught"),
            "limit": limit,
            "offset": offset,
            "exceptions": [_exception_entry(exc, cwd) for exc in page],
            "not_shown": max(0, len(ordered) - offset - len(page)),
            "taken_by_interpreter": {
                "total": len(taken),
                "counts": taken_counts(taken),
                "groups": [
                    taken_entry(group, cwd, MAX_TAKEN_ADDRESSES)
                    for group in taken_groups(taken)
                ],
            },
        }
    )
    commands = [f"ormaos exceptions {entry['address']}" for entry in doc["exceptions"]]
    commands = commands[:2]
    if doc["not_shown"]:
        commands.append(
            f"ormaos exceptions {target.address} --offset {offset + len(page)}"
        )
    if page and page[0].get("handled_addr"):
        commands.append(f"ormaos tree {page[0]['handled_addr']}")
    if not found:
        commands.append(f"ormaos tree {target.address}")
    return _finish(doc, commands)


# ------------------------------------------------------------------- path


def _source(file: str) -> list[str]:
    try:
        with open(file, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    except OSError:
        return []


def _record_command(recording: Recording, scope: str) -> str:
    command = recording.header.get("command")
    tail = shlex.join(str(part) for part in command) if command else "<command>"
    return f"ormaos run --path {scope} -- {tail}"


def _scope_of(call: Record, cwd: str) -> str:
    file = display_path(str(call.get("file", "")), cwd)
    fn = str(call.get("fn", ""))
    return file if fn == "<module>" else f"{file}:{fn}"


def _paths_overview(
    target: Target, cwd: str, limit: int, offset: int
) -> dict[str, Any]:
    recording = target.recording
    groups: dict[tuple[Any, Any, Any], list[Record]] = {}
    for call in recording.calls:
        if "path" in call:
            key = (call.get("file"), call.get("fn"), call.get("line"))
            groups.setdefault(key, []).append(call)
    doc = _base("paths", target)
    scopes = sorted(
        {
            str(s)
            for proc in recording.processes
            for s in proc.header.get("path_scope", [])
        }
    )
    doc["scope"] = scopes
    if not groups:
        suggestion = "<file.py:function>"
        handlers = [e for e in recording.exceptions if e.get("handled_addr")]
        handlers.sort(key=_rank)
        calls = {call["addr"]: call for call in recording.calls}
        if handlers:
            suggestion = _scope_of(calls[handlers[0]["handled_addr"]], cwd)
        elif recording.calls:
            suggestion = _scope_of(recording.calls[0], cwd)
        raise Problem(
            f"no path in {recording.name}: lines are recorded only inside a scope "
            "given when recording",
            [_record_command(recording, suggestion)],
        )
    listed = list(groups.values())
    page = listed[offset : offset + limit]
    doc.update(
        {
            "total": len(listed),
            "limit": limit,
            "offset": offset,
            "functions": [
                {
                    **_where(group[0], cwd),
                    "calls": len(group),
                    "steps": sum(len(call["path"]) for call in group),
                    "addresses": [call["addr"] for call in group[:3]],
                }
                for group in page
            ],
            "not_shown": max(0, len(listed) - offset - len(page)),
        }
    )
    commands = [f"ormaos path {entry['addresses'][0]}" for entry in doc["functions"]]
    commands = commands[:3]
    if doc["not_shown"]:
        commands.append(f"ormaos path {recording.name} --offset {offset + len(page)}")
    return _finish(doc, commands)


def path(
    target: Target, cwd: str = "", limit: int = 40, offset: int | None = None
) -> dict[str, Any]:
    """The lines a call ran, in order, with the calls it made and the
    exceptions it handled in between."""
    if target.kind == "recording":
        return _paths_overview(target, cwd, limit, offset or 0)
    ix = Index(target.recording)
    call = _call_of(target, ix)
    if call is None:
        raise Problem(
            f"{target.address} did not happen inside a recorded call",
            [f"ormaos path {target.recording.name}"],
        )
    if "path" not in call:
        via = ""
        if target.kind != "call":
            via = f", the call of {target.address}"
        raise Problem(
            f"no path for {call['addr']} ({call.get('fn')}{via}): its lines were "
            "not recorded; lines are recorded only inside a scope given when "
            "recording, so the program has to run again",
            [
                _record_command(target.recording, _scope_of(call, cwd)),
                f"ormaos tree {call['addr']}",
            ],
        )
    steps = call["path"]
    if offset is None:
        offset = 0
        if target.kind == "exception" and target.address in steps:
            offset = max(0, steps.index(target.address) - limit // 2)
    file = str(call.get("file", ""))
    lines = _source(file)
    out: list[dict[str, Any]] = []
    for number, step in enumerate(steps[offset : offset + limit], start=offset + 1):
        if isinstance(step, int):
            entry: dict[str, Any] = {"step": number, "line": step}
            if 0 < step <= len(lines):
                entry["text"] = lines[step - 1].rstrip()
        elif step in ix.calls:
            made = ix.calls[step]
            entry = {
                "step": number,
                "call": step,
                "fn": made.get("fn"),
                "out": made.get("out"),
            }
        else:
            exc = ix.exceptions.get(step, {})
            entry = {
                "step": number,
                "exception": step,
                "type": exc.get("type"),
                "status": _status(exc),
            }
        out.append(entry)
    doc = _base("path", target)
    doc.update(
        {
            "call": _brief(call, cwd),
            "total": len(steps),
            "limit": limit,
            "offset": offset,
            "cut_by_limit_when_recording": bool(call.get("path_cut")),
            "steps": out,
            "not_shown_before": offset,
            "not_shown_after": max(0, len(steps) - offset - len(out)),
        }
    )
    if lines:
        started = target.recording.header.get("started")
        try:
            changed = isinstance(started, float) and os.path.getmtime(file) > started
        except OSError:
            changed = False
        doc["source"] = {
            "file": display_path(file, cwd),
            "read": "now, from the file on disk",
            "changed_after_recording": changed,
        }
    else:
        doc["source"] = None
    commands = []
    for entry in out:
        if "exception" in entry:
            commands.append(f"ormaos exceptions {entry['exception']}")
    commands = commands[:2]
    if doc["not_shown_after"]:
        commands.append(f"ormaos path {call['addr']} --offset {offset + len(out)}")
    raised = [e["call"] for e in out if e.get("out") == "raised"]
    if raised:
        commands.append(f"ormaos tree {raised[0]}")
    commands.append(f"ormaos tree {call['addr']}")
    return _finish(doc, commands)


# --------------------------------------------------------------------- io

IO_KINDS = ("sqlite", "file", "subprocess", "http")
_IO_FIELDS = (
    "target",
    "mode",
    "db",
    "table",
    "sql",
    "argc",
    "scheme",
    "host",
    "port",
    "status",
    "out",
)


def _io_entry(event: Record, cwd: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"io_kind": event.get("kind"), "op": event.get("op")}
    for name in _IO_FIELDS:
        if name in event:
            entry[name] = event[name]
    if entry["io_kind"] in ("file", "sqlite") and isinstance(entry.get("target"), str):
        entry["target"] = display_path(entry["target"], cwd)
    if event.get("query"):
        entry["query_string"] = "not recorded"
    entry["at"] = _where(event, cwd)
    return entry


def _by_kind(events: list[Record]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        kind = str(event.get("kind"))
        counts[kind] = counts.get(kind, 0) + int(event.get("n", 1))
    return counts


def _io_detail(target: Target, ix: Index, cwd: str) -> dict[str, Any]:
    event = target.record
    doc = _base("io_event", target)
    doc.update(_io_entry(event, cwd))
    doc["count"] = int(event.get("n", 1))
    doc["duration_s"] = _seconds(event.get("dur"))
    doc["t_s"] = _seconds(event.get("t"))
    doc["thread"] = event.get("thread")
    doc["process"] = event.get("process")
    if "task" in event:
        doc["task"] = event["task"]
    call = ix.calls.get(event.get("call_addr") or "")
    doc["call"] = _brief(call, cwd) if call is not None else None
    doc["inside"] = (
        [_brief(c, cwd) for c in ix.ancestors(call)[-10:]] if call is not None else []
    )
    commands = []
    if call is not None:
        commands = [f"ormaos io {call['addr']}", f"ormaos tree {call['addr']}"]
    commands.append(f"ormaos io {target.recording.name}")
    return _finish(doc, commands)


def io(
    target: Target,
    cwd: str = "",
    limit: int = 20,
    offset: int = 0,
    kind: str | None = None,
) -> dict[str, Any]:
    """The I/O of a recording, or of a call and the calls below it, grouped by
    what was done and by the line of user code that caused it. Or one event."""
    ix = Index(target.recording)
    if target.kind == "io":
        return _io_detail(target, ix, cwd)
    name = target.recording.name
    events = target.recording.io
    call = _call_of(target, ix)
    if target.kind != "recording":
        if call is None:
            raise Problem(
                f"{target.address} did not happen inside a recorded call",
                [f"ormaos io {name}"],
            )
        inside = set(ix.subtree(call["addr"]))
        events = [event for event in events if event.get("call_addr") in inside]
    by_kind = _by_kind(events)
    if kind is not None:
        events = [event for event in events if event.get("kind") == kind]
    groups: dict[Any, list[Record]] = {}
    for event in events:
        key = (
            *(event.get(field) for field in ("kind", "op", *_IO_FIELDS)),
            event.get("file"),
            event.get("line"),
        )
        groups.setdefault(key, []).append(event)
    listed = list(groups.values())
    page = listed[offset : offset + limit]
    doc = _base("io", target)
    doc.update(
        {
            "scope": _brief(call, cwd) if call is not None else None,
            "kind_filter": kind,
            "total": sum(by_kind.values()),
            "by_kind": by_kind,
            "limit": limit,
            "offset": offset,
            "groups": [
                {
                    **_io_entry(group[0], cwd),
                    "count": sum(int(event.get("n", 1)) for event in group),
                    "calls": len({event.get("call_addr") for event in group}),
                    "first": group[0]["addr"],
                }
                for group in page
            ],
            "not_shown": max(0, len(listed) - offset - len(page)),
        }
    )
    if call is not None:
        below: dict[str, dict[str, Any]] = {}
        for kid in ix.children.get(call["addr"], []):
            entry = below.setdefault(
                str(kid.get("fn")),
                {"fn": kid.get("fn"), "calls": 0, "raised": 0, "io": 0},
            )
            entry["calls"] += 1
            entry["raised"] += kid.get("out") == "raised"
            entry["io"] += ix.io_count(ix.subtree(kid["addr"]))
        doc["calls_directly_below"] = list(below.values())[:8]
        doc["io_in_the_call_itself"] = ix.io_count([call["addr"]])
    if not target.recording.processes or not any(
        "io" in proc.header.get("capabilities", [])
        for proc in target.recording.processes
    ):
        doc["note"] = "I/O was not recorded in this recording"
    commands = [f"ormaos io {group['first']}" for group in doc["groups"][:2]]
    if doc["not_shown"]:
        more = f"ormaos io {target.address} --offset {offset + len(page)}"
        commands.append(more + (f" --kind {kind}" if kind else ""))
    if call is not None:
        commands.append(f"ormaos tree {call['addr']}")
        if call.get("parent_addr"):
            commands.append(f"ormaos io {call['parent_addr']}")
    else:
        callers = list(
            dict.fromkeys(e.get("call_addr") for e in events if e.get("call_addr"))
        )
        commands.extend(f"ormaos io {address}" for address in callers[:1])
        commands.append(f"ormaos exceptions {name}")
    return _finish(doc, commands)


__all__ = [
    "IO_KINDS",
    "VERSION",
    "Index",
    "NotFound",
    "Problem",
    "Target",
    "error_document",
    "exceptions",
    "io",
    "path",
    "resolve",
    "tree",
]
