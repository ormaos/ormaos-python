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
            f"... {rest['not_shown']} more calls in {where} ({names}{raised}): "
            f"--offset {rest['offset']}"
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
    return line


def _exceptions(doc: Doc) -> list[str]:
    where = doc["address"]
    if doc.get("scope"):
        where += f" {doc['scope']['fn']} and the calls below it"
    head = (
        f"{where}: {_plural(doc['total'], 'exception')} "
        f"({doc['swallowed']} swallowed, {doc['uncaught']} uncaught)"
    )
    lines = [head]
    lines.extend(_exception_line(entry) for entry in doc["exceptions"])
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
    code = context.get("exit_code")
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
            f"({counts['swallowed']} swallowed, {counts['uncaught']} uncaught)"
        )
    else:
        head += ": no Python process recorded"
    lines = [head]
    command = context.get("command")
    if command:
        lines.append("command: " + " ".join(str(part) for part in command))
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
        f"never recorded: values, and {always - 1} more kinds of things "
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
        return f"file {op} {entry.get('target')}{mode}"
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
    "error": _error,
}


def render(doc: Doc) -> list[str]:
    """The text answer for a document: its lines, then the ``next:`` line."""
    lines = _RENDERERS[doc["kind"]](doc)
    lines.append(_next(doc))
    return lines
