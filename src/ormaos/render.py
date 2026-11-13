"""Turn the JSON document of a verb into a few lines of text.

Facts only, short, and the last line is always ``next:`` with the commands
for the next question.
"""

from typing import Any

MAX_ARGS = 90

type Doc = dict[str, Any]


def shape_text(shape: Any) -> str:
    """``str[14]``, ``list[40 parse.Order]``, ``dict[3]``, ``int``."""
    if not isinstance(shape, dict):
        return "?"
    name = str(shape.get("type", "?"))
    if "len" not in shape:
        return name
    inner = str(shape["len"])
    kinds = shape.get("items")
    if isinstance(kinds, dict) and len(kinds) == 1:
        inner += f" {next(iter(kinds))}"
    return f"{name}[{inner}]"


def duration_text(seconds: Any) -> str:
    if not isinstance(seconds, int | float):
        return ""
    if seconds >= 1:
        return f"{seconds:.2f}s"
    if seconds >= 0.001:
        return f"{seconds * 1e3:.1f}ms"
    return f"{seconds * 1e6:.0f}us"


def _place(where: Any) -> str:
    if not isinstance(where, dict):
        return "?"
    return f"{where.get('file', '?')}:{where.get('line', '?')}"


def _next(doc: Doc) -> str:
    commands = doc.get("next_commands") or []
    return "next: " + (" | ".join(commands) if commands else "(nothing)")


def _signature(node: Doc) -> str:
    args = ", ".join(f"{k}: {shape_text(v)}" for k, v in node.get("args", {}).items())
    if len(args) > MAX_ARGS:
        args = args[: MAX_ARGS - 3] + "..."
    out = node.get("out")
    if out == "returned":
        tail = f" -> {shape_text(node.get('ret'))}"
    elif out:
        tail = f" -> {out}"
    else:
        tail = ""
    return f"{node.get('fn')}({args}){tail}"


def _who(entry: Doc) -> str:
    """`process 2, thread 3, task 1`: only what is not the first of its kind."""
    parts = []
    if isinstance(entry.get("process"), int) and entry["process"] > 1:
        parts.append(f"process {entry['process']}")
    if isinstance(entry.get("thread"), int) and entry["thread"] > 1:
        parts.append(f"thread {entry['thread']}")
    if entry.get("task") is not None:
        parts.append(f"task {entry['task']}")
    return ", ".join(parts)


def _node_line(node: Doc) -> str:
    parts = [
        f"{'  ' * node['depth']}{node['address']} {_signature(node)}",
        _place(node),
    ]
    took = duration_text(node.get("duration_s"))
    if took:
        parts.append(took)
    if node.get("io"):
        parts.append(f"io {node['io']}")
    where = _who(node)
    if where:
        parts.append(where)
    for group in node.get("handled_here", []):
        parts.append(
            f"[{group['status']} {group['count']}x {group['type']}: {group['address']}]"
        )
    if node["calls_below"] and node["depth"] >= 0:
        parts.append(f"calls below: {node['calls_below']}")
    return "  ".join(parts)


def _tree(doc: Doc) -> list[str]:
    lines = []
    if doc.get("ancestors"):
        chain = " > ".join(f"{a['address']} {a['fn']}" for a in doc["ancestors"])
        lines.append(f"inside: {chain}")
    if not doc["nodes"]:
        lines.append(f"{doc['recording']}: no calls recorded")
    lines.extend(_node_line(node) for node in doc["nodes"])
    for rest in doc.get("not_shown", []):
        names = ", ".join(f"{n} {fn}" for fn, n in rest["by_fn"].items())
        raised = f"; {rest['raised']} raised" if rest["raised"] else ""
        where = rest["parent"] or doc["recording"]
        lines.append(
            f"... {rest['not_shown']} of the {rest['total']} calls directly below "
            f"{where} are not shown ({names}{raised}): --offset {rest['offset']}"
        )
    return lines


def _exception_line(entry: Doc) -> str:
    line = (
        f"{entry['address']} {entry['status']} {entry['type']}"
        f"  raised at {_place(entry['raised_at'])}"
    )
    handled = entry.get("handled_at")
    if handled:
        line += f"  handled at {_place(handled)} in {handled.get('fn')}"
    where = _who(entry)
    if where:
        line += f"  {where}"
    return line


def _exceptions(doc: Doc) -> list[str]:
    where = doc["address"]
    if doc.get("scope"):
        where += f" {doc['scope']['fn']} and the calls below it"
    taken = doc.get("taken_by_interpreter", {})
    head = (
        f"{where}: {_plural(doc['total'], 'exception')} "
        f"({doc['swallowed']} swallowed, {doc['uncaught']} uncaught"
        + "".join(f", {text}" for text in taken.get("counts", []))
        + ")"
    )
    lines = [head]
    lines.extend(_exception_line(entry) for entry in doc["exceptions"])
    # One line per origin for what the interpreter took without a handler.
    for group in taken.get("groups", []):
        raised = group["raised_at"]
        lines.append(
            f"{group['count']}x {group['type']} {group['ended']}, at "
            f"{_place(raised)} in {raised.get('fn')}  [{group['addresses'][0]}]"
        )
    if doc["not_shown"]:
        lines.append(
            f"... {doc['not_shown']} more: --offset {doc['offset'] + doc['limit']}"
        )
    return lines


def _exception(doc: Doc) -> list[str]:
    lines = [f"{doc['address']} {doc['type']}: {doc['status']}"]
    raised = doc["raised_at"]
    line = f"  raised at   {_place(raised)} in {raised.get('fn')}"
    if doc.get("raised_in"):
        line += f"  [{doc['raised_in']}]"
    if raised.get("user_code") is False:
        seen = doc["seen_at"]
        line += f"  (not your code; first seen at {_place(seen)} in {seen.get('fn')})"
    lines.append(line)
    if doc["left_calls"]:
        chain = ", ".join(f"{c['fn']} [{c['address']}]" for c in doc["left_calls"])
        lines.append(f"  left calls   {chain}")
    handled = doc.get("handled_at")
    if handled:
        line = f"  handled at  {_place(handled)} in {handled.get('fn')}"
        if doc.get("handled_in"):
            line += f"  [{doc['handled_in']}]"
        if doc["status"] == "swallowed":
            line += "  and not raised again"
        lines.append(line)
    else:
        lines.append("  handled at  nowhere seen")
    if "replaced_by" in doc:
        lines.append(f"  replaced by {doc['replaced_by']}")
    if "during" in doc:
        lines.append(f"  raised while handling {doc['during']}")
    lines.append(f"  message     not recorded; args: {shape_text(doc.get('args'))}")
    same = doc["same_type_and_handler"]
    if same["count"] > 1:
        lines.append(
            f"  same type, same handler: {same['count']} in all: "
            + " ".join(same["addresses"])
            + (" ..." if same["count"] > len(same["addresses"]) else "")
        )
    return lines


def _paths(doc: Doc) -> list[str]:
    lines = [
        f"{doc['recording']}: lines recorded in {_plural(doc['total'], 'function')}"
    ]
    for entry in doc["functions"]:
        lines.append(
            f"  {entry['fn']} {_place(entry)}: {_plural(entry['calls'], 'call')}, "
            f"{entry['steps']} steps  [{' '.join(entry['addresses'])}]"
        )
    if doc["not_shown"]:
        lines.append(
            f"... {doc['not_shown']} more: --offset {doc['offset'] + doc['limit']}"
        )
    return lines


def _path(doc: Doc) -> list[str]:
    call = doc["call"]
    first = doc["offset"] + 1
    last = doc["offset"] + len(doc["steps"])
    head = (
        f"{call['address']} {call['fn']} {_place(call)}: {doc['total']} steps, "
        f"showing {first}-{last}"
    )
    lines = [head]
    source = doc.get("source")
    if source and source["changed_after_recording"]:
        lines.append(
            f"  ({source['file']} changed after the recording: the text is of now)"
        )
    width = len(str(last))
    for step in doc["steps"]:
        number = str(step["step"]).rjust(width)
        if "line" in step:
            text = step.get("text", "")
            lines.append(f"  {number}  {step['line']:>4}  {text}".rstrip())
        elif "call" in step:
            lines.append(
                f"  {number}        call {step['fn']} [{step['call']}] {step['out']}"
            )
        else:
            lines.append(
                f"  {number}        exception {step['type']} [{step['exception']}] "
                f"enters a handler here ({step['status']})"
            )
    if doc["not_shown_after"]:
        lines.append(f"... {doc['not_shown_after']} more steps: --offset {last}")
    if doc["cut_by_limit_when_recording"]:
        lines.append("  (the recording stopped keeping lines: limit reached)")
    return lines


def _plural(count: int, word: str, many: str = "") -> str:
    return f"{count} {word}" if count == 1 else f"{count} {many or word + 's'}"


def _show(doc: Doc) -> list[str]:
    context = doc["context"]
    counts = doc["counts"]
    head = doc["recording"]
    if not doc.get("complete", True):
        head += " (incomplete)"
    code = context.get("exit_code")
    if context.get("mode") == "record" and code is None:
        if context.get("duration_s") is not None:
            head += " record() block"
        else:
            head += " record() block, not ended"
    else:
        head += " no exit code" if code is None else f" exit {code}"
    if context.get("signal"):
        head += f" (signal {context['signal']})"
    if context.get("duration_s") is not None:
        head += f" in {context['duration_s']:.2f}s"
    if counts["processes"]:
        head += (
            f": {_plural(counts['processes'], 'Python process', 'Python processes')}, "
            f"{_plural(counts['calls'], 'call')}, "
            f"{_plural(counts['exceptions'], 'exception')} "
            f"({counts['swallowed']} swallowed, {counts['uncaught']} uncaught"
            + "".join(
                f", {text}"
                for text in doc.get("taken_by_interpreter", {}).get("counts", [])
            )
            + ")"
        )
    else:
        head += ": no Python process recorded"
    lines = [head]
    if doc.get("incomplete"):
        lines.append(
            "incomplete: "
            + "; ".join(doc["incomplete"])
            + ": shown as far as it was written"
        )
    command = context.get("command")
    if command:
        lines.append("command: " + " ".join(str(part) for part in command))
    processes = context.get("processes") or []
    if len(processes) > 1:
        for proc in processes:
            parent = proc.get("parent_process")
            how = "forked from" if proc.get("forked") else "started by"
            if parent:
                origin = f"{how} process {parent}"
            elif proc["process"] == 1:
                origin = "the first"
            else:
                origin = "started by a process that is not in the recording"
            if proc.get("complete"):
                end = ""
            else:
                written = (
                    "events not written"
                    if (proc.get("ended") or {}).get("lost")
                    else "nothing left to write"
                )
                end = f", {proc.get('ended_text')}, {written}"
            lines.append(
                f"process {proc['process']}: {origin}, "
                f"{_plural(proc['calls'], 'call')}, "
                f"{_plural(proc['exceptions'], 'exception')}{end}"
            )
    for group in doc["swallowed"]:
        raised = group["raised_at"]
        where = _place(raised[0]) if raised else "?"
        if len(raised) > 1:
            where += f" and {_plural(len(raised) - 1, 'other line')}"
        lines.append(
            f"swallowed {group['count']}x {group['type']} at "
            f"{_place(group['handled_at'])} in {group['handled_at'].get('fn')}, "
            f"raised at {where}  [{group['addresses'][0]}]"
        )
    if doc["swallowed_groups_not_shown"]:
        lines.append(
            f"... {doc['swallowed_groups_not_shown']} more groups of swallowed"
        )
    for exc in doc["uncaught"]:
        lines.append(
            f"uncaught {exc['type']} raised at {_place(exc['raised_at'])}  "
            f"[{exc['address']}]"
        )
    if doc.get("io"):
        lines.append(f"io: {_plural(counts['io'], 'event')} ({_kinds(doc['io'])})")
    specific = [note for note in doc["not_collected"] if not note.get("always")]
    for note in specific[:5]:
        lines.append(f"not collected: {note['what']}: {note['reason']}")
    always = len(doc["not_collected"]) - len(specific)
    lines.append(
        f"never recorded: values, and {max(0, always - 1)} more kinds of things "
        "(`ormaos show --json` lists them with the reason)"
    )
    return lines


def _cut(text: str, size: int) -> str:
    return text if len(text) <= size else text[: size - 3] + "..."


def io_label(entry: Doc) -> str:
    """What one I/O event did, in a few words."""
    kind, op = entry.get("io_kind"), entry.get("op")
    if kind == "sqlite":
        if op == "connect":
            return f"sqlite connect {entry.get('target')}"
        return f"sqlite {_cut(str(entry.get('sql')), 72)}"
    if kind == "file":
        mode = f" ({entry['mode']})" if entry.get("mode") else ""
        tail = f" -> {entry['out']}" if entry.get("out") else ""
        return f"file {op} {entry.get('target')}{mode}{tail}"
    if kind == "subprocess":
        return f"subprocess {op} {entry.get('target')}"
    if kind == "http":
        port = f":{entry['port']}" if entry.get("port") else ""
        url = f"{entry.get('scheme')}://{entry.get('host')}{port}{entry.get('target')}"
        if entry.get("query_string"):
            url += "?(not recorded)"
        if entry.get("status") is not None:
            tail = f" -> {entry['status']}"
        else:
            tail = f" -> {entry['out']}" if entry.get("out") else ""
        return f"http {op} {url}{tail}"
    return f"{kind} {op}"


def _kinds(by_kind: Doc) -> str:
    return ", ".join(f"{kind} {count}" for kind, count in by_kind.items())


def _io(doc: Doc) -> list[str]:
    where = doc["address"]
    if doc.get("scope"):
        where += f" {doc['scope']['fn']} and the calls below it"
    head = f"{where}: {_plural(doc['total'], 'io event')}"
    if doc["by_kind"]:
        head += f" ({_kinds(doc['by_kind'])})"
    if doc.get("kind_filter"):
        head += f", showing {doc['kind_filter']}"
    lines = [head]
    if doc.get("note"):
        lines.append(f"  ({doc['note']})")
    for group in doc["groups"]:
        at = group["at"]
        lines.append(
            f"  {group['count']}x {io_label(group)}  at {_place(at)} in {at.get('fn')}"
            f"  [{group['first']}]"
        )
    if doc["not_shown"]:
        lines.append(
            f"... {doc['not_shown']} more groups: "
            f"--offset {doc['offset'] + doc['limit']}"
        )
    below = doc.get("calls_directly_below")
    if below:
        parts = []
        for entry in below:
            notes = []
            if entry["raised"]:
                notes.append(f"{entry['raised']} raised")
            notes.append(f"{entry['io']} io" if entry["io"] else "no io")
            parts.append(f"{entry['calls']} {entry['fn']} ({', '.join(notes)})")
        lines.append("calls directly below: " + ", ".join(parts))
    return lines


def _io_event(doc: Doc) -> list[str]:
    lines = [f"{doc['address']} {io_label(doc)}"]
    if doc["count"] > 1:
        lines.append(f"  {doc['count']} times in a row, at the same line")
    at = doc["at"]
    line = f"  caused at   {_place(at)} in {at.get('fn')}"
    if doc.get("call"):
        line += f"  [{doc['call']['address']}]"
    lines.append(line)
    if doc.get("inside"):
        chain = " > ".join(f"{c['address']} {c['fn']}" for c in doc["inside"])
        lines.append(f"  inside      {chain}")
    if doc.get("io_kind") == "sqlite":
        lines.append(f"  database    {doc.get('target')}")
        if doc.get("op") != "connect":
            lines.append("  parameters  not recorded; literals are shown as ?")
    took = duration_text(doc.get("duration_s"))
    if took:
        lines.append(f"  took        {took}")
    return lines


def _in_each(count_a: int, count_b: int, a: str, b: str, word: str = "") -> str:
    """``30 in r1, 36 in r2``; a count of zero reads ``none``."""
    if count_a == count_b:
        return f"{count_a}{word} in both"
    one = f"{count_a}{word}" if count_a else "none"
    other = str(count_b) if count_b else "none"
    return f"{one} in {a}, {other} in {b}"


def _diff_line(entry: Doc, a: str, b: str) -> str:
    """``inventory.py:76`` and, when the line moved, the line in the other."""
    line_a, line_b = entry["a"].get("line"), entry["b"].get("line")
    place = f"{entry.get('file')}:{line_a if line_a is not None else line_b}"
    if line_a is not None and line_b is not None and line_a != line_b:
        place += f" (line {line_b} in {b})"
    return place


def _addresses(entry: Doc) -> str:
    firsts = [entry[side]["first"] for side in ("a", "b") if entry[side].get("first")]
    return f"  [{' '.join(firsts)}]"


def _diff_section(doc: Doc, name: str, word: str) -> str:
    section = doc[name]
    if not section["compared"]:
        return f"{name}: not compared ({section['reason']})"
    places = _plural(section["places"], word)
    if not section["differ"]:
        return f"{name}: the same in both ({places})"
    return f"{name}: {section['differ']} of {places} differ"


def _exception_place(entry: Doc, side: str, other: str) -> str:
    raised = f"{entry['raised_at'].get('file')}:{entry[side].get('raised_line')}"
    text = f"raised at {raised}"
    if entry.get("handled_at"):
        handled = entry["handled_at"].get("file")
        text += f", handled at {handled}:{entry[side].get('handled_line')}"
    there = entry[other]
    if there["count"] and (
        there.get("raised_line") != entry[side].get("raised_line")
        or there.get("handled_line") != entry[side].get("handled_line")
    ):
        lines = [str(there.get("raised_line"))]
        if entry.get("handled_at"):
            lines.append(str(there.get("handled_line")))
        text += f" (lines {' and '.join(lines)} in the other)"
    return text


def _diff(doc: Doc) -> list[str]:
    a, b = doc["a"]["recording"], doc["b"]["recording"]
    lines = [f"{a} -> {b}, matched by place in the code and order, not by address"]
    for side in (doc["a"], doc["b"]):
        code = side.get("exit_code")
        lines.append(
            f"  {side['recording']}: "
            + ("no exit code" if code is None else f"exit {code}")
            + f", {_plural(side['calls'], 'call')}, "
            f"{_plural(side['exceptions'], 'exception')}, "
            f"{_plural(side['io'], 'io event')}"
        )
    for item in doc["context"]:
        what = str(item["what"]).replace("_", " ")
        if isinstance(item["a"], list):
            one = " ".join(str(part) for part in item["a"]) or "none"
            other = " ".join(str(part) for part in item["b"]) or "none"
        else:
            one, other = str(item["a"]), str(item["b"])
        lines.append(f"{what}: {_cut(one, 60)} in {a}, {_cut(other, 60)} in {b}")
    lines.append(_diff_section(doc, "calls", "place"))
    for entry in doc["calls"]["differences"]:
        text = f"  {entry['fn']} at {_diff_line(entry, a, b)}: "
        text += _in_each(entry["a"]["count"], entry["b"]["count"], a, b, " calls")
        if entry["a"]["raised"] != entry["b"]["raised"]:
            text += "; " + _in_each(
                entry["a"]["raised"], entry["b"]["raised"], a, b, " raised"
            )
        end = entry.get("first_other_end")
        if end:
            text += (
                f"; call {end['nth']} {end['a']['out']} in {a} and "
                f"{end['b']['out']} in {b}  "
                f"[{end['a']['address']} {end['b']['address']}]"
            )
        else:
            text += _addresses(entry)
        lines.append(text)
    lines.append(_diff_section(doc, "exceptions", "group"))
    for entry in doc["exceptions"]["differences"]:
        side, other = ("a", "b") if entry["a"]["count"] else ("b", "a")
        lines.append(
            f"  {entry['status']} {entry['type']}, "
            f"{_exception_place(entry, side, other)}: "
            + _in_each(entry["a"]["count"], entry["b"]["count"], a, b)
            + _addresses(entry)
        )
    lines.append(_diff_section(doc, "io", "group"))
    for entry in doc["io"]["differences"]:
        lines.append(
            f"  {entry['label']}  at {_diff_line(entry, a, b)} in {entry['fn']}: "
            + _in_each(entry["a"]["count"], entry["b"]["count"], a, b)
            + _addresses(entry)
        )
    more = sum(doc[name]["not_shown"] for name in ("calls", "exceptions", "io"))
    if more:
        lines.append(f"... {more} more differences: --limit {doc['limit'] + more}")
    notes = doc["not_collected"]
    for key, name in (("only_a", a), ("only_b", b)):
        for note in notes[key][:3]:
            lines.append(f"not collected only in {name}: {note['what']}")
        if len(notes[key]) > 3:
            lines.append(f"... and {len(notes[key]) - 3} more only in {name}")
    lines.append(
        "not compared: " + ", ".join(str(n["what"]) for n in doc["not_compared"])
    )
    return lines


def _state(entry: Doc) -> str:
    if entry.get("mode") == "record" and entry.get("exit_code") is None:
        state = "record() block"
    elif entry.get("exit_code") is None:
        state = "no exit code"
    else:
        state = f"exit {entry['exit_code']}"
        if entry.get("signal"):
            state += f" (signal {entry['signal']})"
    if entry.get("running"):
        state += ", running"
    elif not entry.get("complete"):
        state += ", incomplete"
    return state


def _list_line(entry: Doc) -> str:
    counts = (
        f"{_plural(entry['processes'], 'process', 'processes')}, "
        f"{_plural(entry['calls'], 'call')}, "
        f"{_plural(entry['exceptions'], 'exception')} ({entry['swallowed']} swallowed)"
    )
    if entry.get("io"):
        counts += f", {entry['io']} io"
    parts = [entry["recording"], entry.get("started_text") or "?", _state(entry)]
    took = duration_text(entry.get("duration_s"))
    if took:
        parts.append(took)
    parts.append(counts)
    command = " ".join(str(part) for part in entry.get("command") or [])
    if len(command) > 60:
        command = "..." + command[-57:]  # the end says more: the script
    if command:
        parts.append(command)
    return "  ".join(parts)


def _unreadable(doc: Doc) -> list[str]:
    return [
        f"{entry['recording']}  cannot be read ({entry['reason']})"
        for entry in doc.get("unreadable", [])
    ]


def _list(doc: Doc) -> list[str]:
    if not doc["total"]:
        return [f"no recordings in {doc['root']}"]
    shown = doc["recordings"]
    lines = [
        f"{_plural(doc['total'], 'recording')} in {doc['root']}, newest first"
        + (f" (showing {len(shown)})" if len(shown) < doc["total"] else "")
    ]
    lines.extend(_list_line(entry) for entry in shown)
    return lines + _unreadable(doc)


def _status(doc: Doc) -> list[str]:
    lines = [f"ormaos {doc['ormaos']} on Python {doc['python']} ({doc['executable']})"]
    can = "yes" if doc["can_record"] else "no (needs Python 3.12 or newer)"
    auto = "yes" if doc["starts_in_child_processes"] else "no (ormaos.pth is not there)"
    lines.append(f"this Python can record: {can}; starts by itself under `run`: {auto}")
    now = doc.get("recording_now")
    lines.append(
        f"this command is running inside a recording: {now}"
        if now
        else "this command is not running inside a recording"
    )
    if not doc["recordings"]:
        lines.append(f"no recordings in {doc['root']}")
        return lines
    last = doc["last"]
    lines.append(
        f"{_plural(doc['recordings'], 'recording')} in {doc['root']}; "
        f"last: {last['recording']}, {last.get('started_text') or '?'}, {_state(last)}"
    )
    if doc["running"]:
        lines.append("running now: " + ", ".join(doc["running"]))
    if doc["incomplete"]:
        lines.append(
            "incomplete (no end record, not running): " + ", ".join(doc["incomplete"])
        )
    return lines + _unreadable(doc)


def _error(doc: Doc) -> list[str]:
    return [str(doc["error"])]


_RENDERERS = {
    "show": _show,
    "tree": _tree,
    "exceptions": _exceptions,
    "exception": _exception,
    "paths": _paths,
    "path": _path,
    "io": _io,
    "io_event": _io_event,
    "diff": _diff,
    "list": _list,
    "status": _status,
    "error": _error,
}


def render(doc: Doc) -> list[str]:
    """The text answer for a document: its lines, then the ``next:`` line."""
    lines = _RENDERERS[doc["kind"]](doc)
    lines.append(_next(doc))
    return lines
