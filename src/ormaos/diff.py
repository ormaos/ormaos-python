"""``ormaos diff <a> <b>``: what two recordings did differently.

Addresses belong to one recording, so nothing is matched by address. Events
are matched by where they come from in the code (file, function, line) and,
for the same place, in order: the first with the first, the second with the
second. A place whose line moved between the two recordings is matched when
the file and the function leave no doubt. The answer describes what differs
and never says which of the two is right.
"""

from collections.abc import Callable, Hashable
from typing import Any

from ormaos.recording import Record, Recording
from ormaos.render import io_label
from ormaos.summary import display_path
from ormaos.verbs import VERSION, Problem, _finish, _io_entry, _seconds, _status

NOT_COMPARED = [
    {"what": "values", "reason": "never recorded"},
    {
        "what": "shapes of arguments and results",
        "reason": "only how many calls there were and how each ended is compared",
    },
    {"what": "durations, cpu and memory", "reason": "they differ in every run"},
    {"what": "the lines of `path`", "reason": "not compared yet"},
    {
        "what": "which process or thread an event was in",
        "reason": "events are matched over the whole recording",
    },
]

# An event, described: (what stays when lines move, the lines, the record).
type _Described = tuple[Hashable, tuple[Any, ...], Record]
type _Pair = tuple[tuple[Any, ...], list[Record], tuple[Any, ...], list[Record]]


def _capabilities(recording: Recording) -> set[str]:
    return {
        str(c) for p in recording.processes for c in p.header.get("capabilities", [])
    }


def _cwd(recording: Recording) -> str:
    return str(recording.header.get("cwd") or "")


def _match(a: list[_Described], b: list[_Described]) -> list[_Pair]:
    """Pair the places of two recordings, in order of first appearance."""
    sides: list[dict[Hashable, dict[tuple[Any, ...], list[Record]]]] = [{}, {}]
    order: list[Hashable] = []
    for side, events in zip(sides, (a, b), strict=True):
        for ident, lines, record in events:
            if ident not in sides[0] and ident not in sides[1]:
                order.append(ident)
            side.setdefault(ident, {}).setdefault(lines, []).append(record)
    pairs: list[_Pair] = []
    for ident in order:
        in_a, in_b = sides[0].get(ident, {}), sides[1].get(ident, {})
        only_a = [lines for lines in in_a if lines not in in_b]
        only_b = [lines for lines in in_b if lines not in in_a]
        for lines in in_a:
            if lines in in_b:
                pairs.append((lines, in_a[lines], lines, in_b[lines]))
        if len(only_a) == 1 and len(only_b) == 1:
            # The same file, function and kind of event, at another line.
            pairs.append((only_a[0], in_a[only_a[0]], only_b[0], in_b[only_b[0]]))
            continue
        pairs.extend((lines, in_a[lines], (), []) for lines in only_a)
        pairs.extend(((), [], lines, in_b[lines]) for lines in only_b)
    return pairs


def _count(records: list[Record]) -> int:
    return sum(int(record.get("n", 1)) for record in records)


def _side(records: list[Record], count: int, where: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"count": count, **where}
    if records:
        out["first"] = records[0]["addr"]
    return out


def _calls(a: Recording, b: Recording) -> tuple[list[dict[str, Any]], int]:
    def described(recording: Recording) -> list[_Described]:
        cwd = _cwd(recording)
        return [
            ((display_path(str(c.get("file")), cwd), c.get("fn")), (c.get("line"),), c)
            for c in recording.calls
        ]

    def raised(calls: list[Record]) -> int:
        return sum(1 for call in calls if call.get("out") == "raised")

    out = []
    pairs = _match(described(a), described(b))
    for lines_a, in_a, lines_b, in_b in pairs:
        if len(in_a) == len(in_b) and raised(in_a) == raised(in_b):
            continue
        some = (in_a or in_b)[0]
        entry: dict[str, Any] = {
            "fn": some.get("fn"),
            "file": display_path(str(some.get("file")), _cwd(a if in_a else b)),
            "a": _side(in_a, len(in_a), {"line": lines_a[0] if lines_a else None}),
            "b": _side(in_b, len(in_b), {"line": lines_b[0] if lines_b else None}),
        }
        entry["a"]["raised"], entry["b"]["raised"] = raised(in_a), raised(in_b)
        for nth, (one, other) in enumerate(zip(in_a, in_b, strict=False), start=1):
            if one.get("out") != other.get("out"):
                # The first call, in order, that ended differently.
                entry["first_other_end"] = {
                    "nth": nth,
                    "a": {"address": one["addr"], "out": one.get("out")},
                    "b": {"address": other["addr"], "out": other.get("out")},
                }
                break
        out.append(entry)
    return out, len(pairs)


def _place(where: Any, cwd: str) -> tuple[Any, Any, Any]:
    if not isinstance(where, dict) or not where:
        return (None, None, None)
    return (
        display_path(str(where.get("file")), cwd),
        where.get("fn"),
        where.get("line"),
    )


def _exceptions(a: Recording, b: Recording) -> tuple[list[dict[str, Any]], int]:
    def described(recording: Recording) -> list[_Described]:
        cwd = _cwd(recording)
        out: list[_Described] = []
        for exc in recording.exceptions:
            raised = _place(exc.get("origin"), cwd)
            handled = _place(exc.get("handled"), cwd)
            ident = (exc.get("type"), _status(exc), raised[:2], handled[:2])
            out.append((ident, (raised[2], handled[2]), exc))
        return out

    def where(lines: tuple[Any, ...]) -> dict[str, Any]:
        raised, handled = lines or (None, None)
        return {"raised_line": raised, "handled_line": handled}

    out = []
    pairs = _match(described(a), described(b))
    for lines_a, in_a, lines_b, in_b in pairs:
        if len(in_a) == len(in_b):
            continue
        some, cwd = (in_a or in_b)[0], _cwd(a if in_a else b)
        raised, handled = (
            _place(some.get("origin"), cwd),
            _place(some.get("handled"), cwd),
        )
        out.append(
            {
                "type": some.get("type"),
                "status": _status(some),
                "raised_at": {"file": raised[0], "fn": raised[1]},
                "handled_at": {"file": handled[0], "fn": handled[1]}
                if handled[0]
                else None,
                "a": _side(in_a, len(in_a), where(lines_a)),
                "b": _side(in_b, len(in_b), where(lines_b)),
            }
        )
    return out, len(pairs)


def _io(a: Recording, b: Recording) -> tuple[list[dict[str, Any]], int]:
    def entry_of(event: Record, cwd: str) -> dict[str, Any]:
        entry = _io_entry(event, cwd)
        # The number of a connection and a port are given at run time.
        entry.pop("db", None)
        entry.pop("port", None)
        return entry

    def described(recording: Recording) -> list[_Described]:
        cwd = _cwd(recording)
        out: list[_Described] = []
        for event in recording.io:
            entry = entry_of(event, cwd)
            at = entry.pop("at")
            ident = (
                tuple(sorted(entry.items(), key=str)),
                at.get("file"),
                at.get("fn"),
            )
            out.append((ident, (at.get("line"),), event))
        return out

    out = []
    pairs = _match(described(a), described(b))
    for lines_a, in_a, lines_b, in_b in pairs:
        if _count(in_a) == _count(in_b):
            continue
        entry = entry_of((in_a or in_b)[0], _cwd(a if in_a else b))
        at = entry.pop("at")
        out.append(
            {
                **entry,
                "label": io_label(entry),
                "file": at.get("file"),
                "fn": at.get("fn"),
                "a": _side(
                    in_a, _count(in_a), {"line": lines_a[0] if lines_a else None}
                ),
                "b": _side(
                    in_b, _count(in_b), {"line": lines_b[0] if lines_b else None}
                ),
            }
        )
    return out, len(pairs)


def _context(recording: Recording) -> dict[str, Any]:
    end = recording.end or {}
    return {
        "recording": recording.name,
        "command": recording.header.get("command"),
        "exit_code": end.get("exit_code"),
        "signal": end.get("signal"),
        "duration_s": _seconds(end.get("dur")),
        "python": sorted({str(p.header.get("python")) for p in recording.processes}),
        "processes": len(recording.processes),
        "capabilities": sorted(_capabilities(recording)),
        "path_scope": sorted(
            {
                str(s)
                for p in recording.processes
                for s in p.header.get("path_scope", [])
            }
        ),
        "calls": len(recording.calls),
        "exceptions": len(recording.exceptions),
        "io": _count(recording.io),
    }


def _notes(recording: Recording) -> list[dict[str, Any]]:
    seen = []
    for note in recording.not_collected:
        entry = {"what": note.get("what"), "reason": note.get("reason")}
        if entry not in seen:
            seen.append(entry)
    return seen


def diff(a: Recording, b: Recording, limit: int = 20) -> dict[str, Any]:
    """What ``b`` did differently from ``a``, as a document."""
    if a.name == b.name:
        raise Problem(
            f"`diff` takes two recordings; {a.name} was given twice",
            [f"ormaos show {a.name}"],
        )
    ctx_a, ctx_b = _context(a), _context(b)
    doc: dict[str, Any] = {
        "kind": "diff",
        "version": VERSION,
        "a": ctx_a,
        "b": ctx_b,
        "matched_by": "file, function and line of the code, then order; never address",
        "context": [
            {"what": name, "a": ctx_a[name], "b": ctx_b[name]}
            for name in (
                "command",
                "exit_code",
                "signal",
                "python",
                "processes",
                "capabilities",
                "path_scope",
            )
            if ctx_a[name] != ctx_b[name]
        ],
        "limit": limit,
    }
    sections: tuple[tuple[str, Callable[..., Any]], ...] = (
        ("calls", _calls),
        ("exceptions", _exceptions),
        ("io", _io),
    )
    commands: list[str] = []
    for name, compare in sections:
        missing = [r.name for r in (a, b) if name not in _capabilities(r)]
        section: dict[str, Any] = {"compared": not missing}
        if missing:
            section["reason"] = f"{name} not recorded in {' and '.join(missing)}"
            section.update(
                {"places": 0, "differ": 0, "differences": [], "not_shown": 0}
            )
        else:
            differences, places = compare(a, b)
            section.update(
                {
                    "places": places,
                    "differ": len(differences),
                    "differences": differences[:limit],
                    "not_shown": max(0, len(differences) - limit),
                }
            )
        doc[name] = section
    verbs = {"calls": "tree", "exceptions": "exceptions", "io": "io"}
    for name in ("exceptions", "calls", "io"):
        for entry in doc[name]["differences"][:2]:
            end = entry.get("first_other_end")
            firsts = (
                [end["a"]["address"], end["b"]["address"]]
                if end
                else [entry[side].get("first") for side in ("a", "b")]
            )
            commands.extend(f"ormaos {verbs[name]} {addr}" for addr in firsts if addr)
    notes_a, notes_b = _notes(a), _notes(b)
    doc["not_collected"] = {
        "only_a": [note for note in notes_a if note not in notes_b],
        "only_b": [note for note in notes_b if note not in notes_a],
        "in_both": len([note for note in notes_a if note in notes_b]),
    }
    doc["not_compared"] = NOT_COMPARED
    commands += [f"ormaos show {a.name}", f"ormaos show {b.name}"]
    return _finish(doc, _spread(commands))


def _spread(commands: list[str]) -> list[str]:
    """Keep the list short without dropping a whole kind of question."""
    by_verb: dict[str, list[str]] = {}
    for command in dict.fromkeys(commands):
        by_verb.setdefault(command.split()[1], []).append(command)
    out: list[str] = []
    while any(by_verb.values()):
        for verb in list(by_verb):
            if by_verb[verb]:
                out.append(by_verb[verb].pop(0))
    return [c for c in out if " show " not in c] + [c for c in out if " show " in c]
