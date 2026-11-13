"""Read a recording from disk.

A recording is a directory ``.ormaos/r<N>/`` with one ``run.jsonl`` (written
by ``ormaos run`` or by ``with ormaos.record():``) and one
``p<pid>-<time>.jsonl`` per Python process. Every file starts with a header
line carrying ``"format": "provisional-0"``.

The header has a part named ``core`` (what any recorder writes: the process
or the command, the capabilities recorded, the wall-clock anchor of the event
times) and, in a process file, a part named ``python``. Here both are read
into one flat mapping, ``header``; the file's own line stays in ``raw``.

A process that was killed has no end record. Its file is still read: the
records are written in chunks, and each chunk ends with a checkpoint that
lists what was open then. The calls and exceptions of the checkpoints that
never got their final record are added as they were last written.

Addresses (``r3:c42``, ``r3:e5``, ``r3:i7``) are given here, at read time:
processes in start order, then records in the order they were created in each
process.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

FORMAT = "provisional-0"
ROOT = ".ormaos"

_DIR_NAME = re.compile(r"r(\d+)")

type Record = dict[str, Any]


@dataclass
class Process:
    index: int
    path: str
    header: Record
    raw: Record = field(default_factory=dict)
    parent: int | None = None  # the index of the process that started it
    # Signals this process sent to multiprocessing children it started.
    children_ended: list[Record] = field(default_factory=list)
    # Set when a recorded process ended this one itself (multiprocessing
    # `terminate`, the end of a `Pool`): {"process", "by", "signal"}.
    ended_by: Record | None = None
    written_until: int | None = None  # `t` of the last checkpoint
    calls: list[Record] = field(default_factory=list)
    exceptions: list[Record] = field(default_factory=list)
    io: list[Record] = field(default_factory=list)
    not_collected: list[Record] = field(default_factory=list)
    end: Record | None = None


@dataclass
class Recording:
    number: int
    path: str
    header: Record
    end: Record | None = None
    processes: list[Process] = field(default_factory=list)
    not_collected: list[Record] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"r{self.number}"

    @property
    def complete(self) -> bool:
        """The run wrote its end record, and so did every process that the
        recorded program did not end itself (a pool ends its workers with a
        signal: that is the program's own doing, not a kill from outside)."""
        return self.end is not None and all(
            proc.end is not None or proc.ended_by is not None for proc in self.processes
        )

    @property
    def incomplete_reasons(self) -> list[str]:
        reasons = []
        if self.end is None:
            reasons.append("the run has no end record")
        open_ = [
            proc.index
            for proc in self.processes
            if proc.end is None and proc.ended_by is None
        ]
        if open_:
            numbers = ", ".join(str(index) for index in open_)
            word = "process" if len(open_) == 1 else "processes"
            reasons.append(f"{word} {numbers} wrote no end record")
        return reasons

    @property
    def calls(self) -> list[Record]:
        return [call for proc in self.processes for call in proc.calls]

    @property
    def exceptions(self) -> list[Record]:
        return [exc for proc in self.processes for exc in proc.exceptions]

    @property
    def io(self) -> list[Record]:
        return [event for proc in self.processes for event in proc.io]


def recording_numbers(root: str) -> list[int]:
    try:
        names = os.listdir(root)
    except OSError:
        return []
    numbers = []
    for name in names:
        match = _DIR_NAME.fullmatch(name)
        if match and os.path.isdir(os.path.join(root, name)):
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def _read_lines(path: str) -> tuple[list[Record], int]:
    """The JSON objects of a file, and how many lines could not be read."""
    records: list[Record] = []
    bad = 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(value, dict):
                records.append(value)
            else:
                bad += 1
    return records, bad


def flat_header(raw: Record) -> Record:
    """One mapping from a header with `core` and `python` parts. A header
    written before the split is already flat and comes back unchanged."""
    core = raw.get("core")
    if not isinstance(core, dict):
        return raw
    flat = {key: value for key, value in raw.items() if key not in ("core", "python")}
    flat.update(core)
    anchor = core.get("anchor")
    if isinstance(anchor, dict):
        flat["started"] = anchor.get("unix")
    profile = raw.get("python")
    if isinstance(profile, dict):
        flat.update({key: value for key, value in profile.items() if key != "version"})
        flat["python"] = profile.get("version")
    return flat


def _from_checkpoints(proc: Process, checkpoints: list[Record]) -> None:
    """Finish a file that has no end record, the way the recorder would have:
    a call that had not returned is `open`, an exception that was in a handler
    is `handled`, one still in flight has no handler seen."""
    done_calls = {call.get("id") for call in proc.calls}
    done_exceptions = {exc.get("id") for exc in proc.exceptions}
    open_exceptions: dict[Any, Record] = {}
    for point in checkpoints:
        for call in point.get("calls", []):
            if isinstance(call, dict) and call.get("id") not in done_calls:
                done_calls.add(call.get("id"))
                proc.calls.append({**call, "out": "open"})
        for exc in point.get("exceptions", []):
            if isinstance(exc, dict) and exc.get("id") not in done_exceptions:
                open_exceptions[exc.get("id")] = exc
    for exc in open_exceptions.values():
        if "handled" in exc:
            exc.setdefault("swallowed", True)
            exc["end"] = "handled"
        else:
            exc["end"] = "no_handler_seen"
        proc.exceptions.append(exc)


def _note(what: str, reason: str) -> Record:
    return {"k": "not_collected", "what": what, "reason": reason}


def _load_process(path: str) -> Process:
    records, bad = _read_lines(path)
    header: Record = {}
    raw: Record = {}
    if records and records[0].get("kind") == "process":
        raw = records.pop(0)
        header = flat_header(raw)
    proc = Process(index=0, path=path, header=header, raw=raw)
    checkpoints: list[Record] = []
    name = os.path.basename(path)
    if not header:
        proc.not_collected.append(_note(f"header of {name}", "the file has no header"))
    for rec in records:
        kind = rec.get("k")
        if kind == "call":
            proc.calls.append(rec)
        elif kind == "exc":
            proc.exceptions.append(rec)
        elif kind == "io":
            proc.io.append(rec)
        elif kind == "not_collected":
            proc.not_collected.append(rec)
        elif kind == "end":
            proc.end = rec
        elif kind == "checkpoint":
            checkpoints.append(rec)
        elif kind == "child_ended":
            proc.children_ended.append(rec)
    if checkpoints and isinstance(checkpoints[-1].get("t"), int):
        proc.written_until = checkpoints[-1]["t"]
    if proc.end is None:
        _from_checkpoints(proc, checkpoints)
    if bad:
        proc.not_collected.append(
            _note(f"{bad} lines of {name}", "the lines are not valid JSON")
        )
    proc.calls.sort(key=lambda rec: rec.get("id", 0))
    proc.exceptions.sort(key=lambda rec: rec.get("id", 0))
    proc.io.sort(key=lambda rec: rec.get("id", 0))
    return proc


def _path(steps: Any, call_addr: dict[Any, str], exc_addr: dict[Any, str]) -> list[Any]:
    """Line numbers stay; the marks ``c12`` and ``e3`` become addresses."""
    out: list[Any] = []
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, int):
            out.append(step)
        elif isinstance(step, str) and step[1:].isdigit():
            table = call_addr if step[0] == "c" else exc_addr
            addr = table.get(int(step[1:]))
            if addr is not None:
                out.append(addr)
    return out


def _link(processes: list[Process]) -> None:
    """Give every process the index of the recorded process that started it:
    the one whose pid is its parent pid, the latest that started before it."""
    for index, proc in enumerate(processes, start=1):
        proc.index = index
    for proc in processes:
        ppid = proc.header.get("ppid")
        started = proc.header.get("started") or 0.0
        for other in processes:
            if other is proc or other.header.get("pid") != ppid:
                continue
            if (other.header.get("started") or 0.0) <= started:
                proc.parent = other.index
    # Who ended whom: a signal sent through multiprocessing, after the child
    # started, to a child that then wrote no end record.
    for sender in processes:
        base = sender.header.get("started") or 0.0
        for sent in sender.children_ended:
            at = base + (sent.get("t") or 0) / 1e9
            for proc in processes:
                if (
                    proc.end is None
                    and proc.ended_by is None
                    and proc.header.get("pid") == sent.get("pid")
                    and (proc.header.get("started") or 0.0) <= at
                ):
                    proc.ended_by = {
                        "process": sender.index,
                        "by": sent.get("by"),
                        "signal": sent.get("signal"),
                    }
    for proc in processes:
        if proc.end is not None:
            continue
        pid = proc.header.get("pid", "?")
        if proc.ended_by is None:
            proc.not_collected.append(
                _note(
                    f"the last events of process {pid}",
                    "the process ended without running its exit handlers "
                    "(killed, os._exit, or still running); what it had written "
                    "is here, and the calls that had not returned are marked open",
                )
            )
            continue
        who = (
            "its pool ended it"
            if proc.ended_by["by"] == "pool"
            else f"process {proc.ended_by['process']} ended it with multiprocessing"
        )
        # The top of a spawned child is the `<module>` of its start command.
        running = sum(
            1
            for call in proc.calls
            if call.get("out") == "open" and call.get("fn") != "<module>"
        )
        tail = (
            f"; {running} of its calls had not returned and are marked open"
            if running
            else "; no call of your code was running in it"
        )
        proc.not_collected.append(
            _note(
                f"the end record of process {pid}",
                f"{who} with signal {proc.ended_by['signal']}, so it wrote no end "
                f"record: its duration, CPU and memory are missing{tail}",
            )
        )


def load(path: str) -> Recording:
    match = _DIR_NAME.fullmatch(os.path.basename(os.path.normpath(path)))
    number = int(match.group(1)) if match else 0
    recording = Recording(number=number, path=path, header={})
    run_path = os.path.join(path, "run.jsonl")
    if os.path.isfile(run_path):
        records, _bad = _read_lines(run_path)
        for rec in records:
            if rec.get("kind") == "run":
                recording.header = flat_header(rec)
            elif rec.get("k") == "end":
                recording.end = rec
            elif rec.get("k") == "not_collected":
                recording.not_collected.append(rec)
    processes = [
        _load_process(os.path.join(path, name))
        for name in sorted(os.listdir(path))
        if name.startswith("p") and name.endswith(".jsonl")
    ]
    processes.sort(
        key=lambda p: (p.header.get("started", 0.0), p.header.get("pid", 0), p.path)
    )
    _link(processes)
    prefix = recording.name
    call_n = 0
    exc_n = 0
    io_n = 0
    for index, proc in enumerate(processes, start=1):
        proc.index = index
        call_addr: dict[Any, str] = {}
        for call in proc.calls:
            call_n += 1
            call["addr"] = call_addr[call.get("id")] = f"{prefix}:c{call_n}"
            call["process"] = index
        exc_addr: dict[Any, str] = {}
        for exc in proc.exceptions:
            exc_n += 1
            exc["addr"] = exc_addr[exc.get("id")] = f"{prefix}:e{exc_n}"
            exc["process"] = index
        for call in proc.calls:
            call["parent_addr"] = call_addr.get(call.get("parent"))
            if "path" in call:
                call["path"] = _path(call["path"], call_addr, exc_addr)
        for event in proc.io:
            io_n += 1
            event["addr"] = f"{prefix}:i{io_n}"
            event["process"] = index
            event["call_addr"] = call_addr.get(event.get("call"))
        for exc in proc.exceptions:
            exc["call_addr"] = call_addr.get(exc.get("at", {}).get("call"))
            exc["handled_addr"] = call_addr.get(exc.get("handled", {}).get("call"))
            for key in ("during", "replaced_by"):
                if key in exc:
                    exc[f"{key}_addr"] = exc_addr.get(exc[key])
        recording.not_collected.extend(proc.not_collected)
    recording.processes = processes
    scopes = sorted(
        {str(s) for proc in processes for s in proc.header.get("path_scope", [])}
    )
    if scopes and not any("path" in call for call in recording.calls):
        recording.not_collected.append(
            _note("path", f"no call matched the scope {', '.join(scopes)}")
        )
    if not processes:
        recording.not_collected.append(
            _note(
                "everything inside Python",
                "no Python process reported: the command started no Python, "
                "or a Python without ormaos installed in its environment",
            )
        )
    return recording
