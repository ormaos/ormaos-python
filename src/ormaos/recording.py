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
from array import array
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Any

from ormaos._command import is_python

FORMAT = "provisional-0"
ROOT = ".ormaos"

_DIR_NAME = re.compile(r"r([1-9]\d*)")

type Record = dict[str, Any]


@dataclass
class Process:
    index: int
    path: str
    header: Record
    raw: Record = field(default_factory=dict)
    parent: int | None = None  # the index of the process that started it
    # Signals this process sent to children it started, and what it learned
    # about how its children ended (`multiprocessing`, `subprocess`).
    children_ended: list[Record] = field(default_factory=list)
    children_exited: list[Record] = field(default_factory=list)
    # For a process with no end record. `lost`: its file does not end with an
    # idle checkpoint, so events it had not written yet may be missing (the
    # recorder says at every write whether anything is still unwritten).
    # `ending`: how it ended, as far as another record says:
    # {"signal": 15 or None, "by": "pool", "program", "outside" or None,
    # "process": the index of the process that sent the signal, or None}.
    lost: bool = False
    ending: Record | None = None
    # Counts that do not need the records themselves. They are filled in both
    # ways of reading; when a recording is read for an overview only
    # (`load(..., overview=True)`), `calls` and `io` stay empty and these are
    # all there is: the file is read line by line and no call is kept.
    n_calls: int = 0
    call_threads: set[int] = field(default_factory=set)
    call_tasks: set[int] = field(default_factory=set)
    has_path: bool = False
    io_counts: dict[str, int] = field(default_factory=dict)
    first_call: str | None = None  # the address of its first call
    call_ids: Any = None  # overview only: the ids of its calls, sorted

    @property
    def ending_text(self) -> str:
        """`was ended by its pool with signal 15`, for a process with no end
        record."""
        ending = self.ending or {}
        signum = ending.get("signal")
        by = ending.get("by")
        if by == "pool":
            return f"was ended by its pool with signal {signum}"
        if by == "program":
            return (
                f"was ended by the program (process {ending.get('process')}) "
                f"with signal {signum}"
            )
        if by == "outside":
            return (
                f"was ended by signal {signum}, which the program was not seen "
                "sending (from outside, or by a way that is not watched)"
            )
        return "has no end record and no signal was seen (it may still be running)"

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
        """The run wrote its end record, and no process ended with events it
        had not written yet. Who ended a process does not matter: a process
        ended by a signal after it had written everything lost nothing."""
        return self.end is not None and not any(proc.lost for proc in self.processes)

    @property
    def incomplete_reasons(self) -> list[str]:
        reasons = []
        if self.end is None:
            reasons.append("the run has no end record")
        for proc in self.processes:
            if proc.lost:
                reasons.append(
                    f"process {proc.index} {proc.ending_text}, with events not written"
                )
        return reasons

    @property
    def calls(self) -> list[Record]:
        return [call for proc in self.processes for call in proc.calls]

    @property
    def n_calls(self) -> int:
        return sum(proc.n_calls for proc in self.processes)

    @property
    def first_call(self) -> str | None:
        return next((p.first_call for p in self.processes if p.first_call), None)

    @property
    def io_counts(self) -> dict[str, int]:
        """I/O events by kind, counting the repeats of an event."""
        out: dict[str, int] = {}
        for proc in self.processes:
            for kind, count in proc.io_counts.items():
                out[kind] = out.get(kind, 0) + count
        return out

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
            except (ValueError, RecursionError):
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


def _from_checkpoints(
    proc: Process, checkpoints: list[Record], counted: set[int] | None = None
) -> None:
    """Finish a file that has no end record, the way the recorder would have:
    a call that had not returned is `open`, an exception that was in a handler
    is `handled`, one still in flight has no handler seen."""
    done_calls: set[Any] = {call.get("id") for call in proc.calls}
    if counted:
        done_calls |= counted  # calls that an overview counted without keeping
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


def _well_formed(rec: Record) -> bool:
    """Has a call, exception or io record the parts the readers rely on? A
    file can be damaged or written by something else."""
    if type(rec.get("id")) is not int:
        return False
    for key in ("t", "dur", "line", "n", "thread"):
        if key in rec and type(rec[key]) is not int:
            return False
    kind = rec["k"]
    if kind == "call":
        parent = rec.get("parent")
        if parent is not None and (type(parent) is not int or parent >= rec["id"]):
            return False  # a parent starts before its calls
        if not isinstance(rec.get("args", {}), dict):
            return False
        return "path" not in rec or isinstance(rec["path"], list)
    if kind == "exc":
        for key in ("origin", "at", "handled"):
            if key in rec and not isinstance(rec[key], dict):
                return False
        return isinstance(rec.get("origin"), dict)
    return True


def _note(what: str, reason: str) -> Record:
    return {"k": "not_collected", "what": what, "reason": reason}


# A call record as the recorder writes it, read without building it: the
# keys come in this order. Anything else on the line is not needed for an
# overview.
_CALL_LINE = re.compile(r'\{"k":"call","id":(\d+),"parent":(?:null|\d+),')
_CALL_THREAD = re.compile(r',"thread":(\d+),"t":')
_CALL_TASK = re.compile(r',"task":(\d+)[,}]')


def _load_process(path: str, overview: bool = False) -> Process:
    proc = Process(index=0, path=path, header={})
    checkpoints: list[Record] = []
    ids = array("q")
    bad = 0
    first = True
    # Does the file end with everything written? True after the header, then
    # as the last checkpoint says; any record after it is something more.
    clean = False
    name = os.path.basename(path)
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if overview:
                found = _CALL_LINE.match(line)
                if found is not None and line.endswith("}\n"):
                    # One call: counted, not kept.
                    ids.append(int(found.group(1)))
                    thread = _CALL_THREAD.search(line)
                    if thread is not None:
                        proc.call_threads.add(int(thread.group(1)))
                    task = _CALL_TASK.search(line)
                    if task is not None:
                        proc.call_tasks.add(int(task.group(1)))
                    if not proc.has_path and ',"path":[' in line:
                        proc.has_path = True
                    clean = False
                    first = False
                    continue
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except (ValueError, RecursionError):
                bad += 1
                continue
            if not isinstance(rec, dict):
                bad += 1
                continue
            if first:
                first = False
                if rec.get("kind") == "process":
                    proc.raw = rec
                    proc.header = flat_header(rec)
                    clean = True
                    continue
            kind = rec.get("k")
            clean = kind == "checkpoint" and rec.get("idle") is True
            if kind in ("call", "exc", "io") and not _well_formed(rec):
                bad += 1  # valid JSON, but not a record this version writes
                continue
            if kind == "call":
                proc.calls.append(rec)
            elif kind == "exc":
                proc.exceptions.append(rec)
            elif kind == "io":
                io_kind = str(rec.get("kind"))
                proc.io_counts[io_kind] = proc.io_counts.get(io_kind, 0) + int(
                    rec.get("n", 1)
                )
                if not overview or io_kind == "subprocess":
                    proc.io.append(rec)
            elif kind == "not_collected":
                proc.not_collected.append(rec)
            elif kind == "end":
                proc.end = rec
            elif kind == "checkpoint":
                checkpoints.append(rec)
            elif kind == "child_ended":
                proc.children_ended.append(rec)
            elif kind == "child_exit":
                proc.children_exited.append(rec)
    if not proc.header:
        proc.not_collected.append(_note(f"header of {name}", "the file has no header"))
    proc.lost = proc.end is None and not (clean and not bad)
    if checkpoints and isinstance(checkpoints[-1].get("t"), int):
        proc.written_until = checkpoints[-1]["t"]
    if proc.end is None:
        _from_checkpoints(proc, checkpoints, set(ids) if overview else None)
    if bad:
        proc.not_collected.append(
            _note(
                f"{bad} lines of {name}",
                "the lines are not valid JSON, or not records in the form this "
                "version writes",
            )
        )
    proc.calls.sort(key=lambda rec: rec.get("id", 0))
    proc.exceptions.sort(key=lambda rec: rec.get("id", 0))
    proc.io.sort(key=lambda rec: rec.get("id", 0))
    for call in proc.calls:  # every call, or in an overview the few read in full
        if type(call.get("thread")) is int:
            proc.call_threads.add(call["thread"])
        if type(call.get("task")) is int:
            proc.call_tasks.add(call["task"])
        proc.has_path = proc.has_path or "path" in call
    if overview:
        ids.extend(call["id"] for call in proc.calls)
        proc.call_ids = array("q", sorted(ids))
        proc.calls = []
        proc.n_calls = len(proc.call_ids)
    else:
        proc.n_calls = len(proc.calls)
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
    # How a process with no end record ended: the signal its parent saw when
    # it waited for it, and whether the program sent that signal itself.
    for proc in processes:
        if proc.end is not None:
            continue
        pid = proc.header.get("pid")
        started = proc.header.get("started") or 0.0
        sent: Record | None = None
        sender = 0
        seen: Record | None = None
        for other in processes:
            base = other.header.get("started") or 0.0
            for rec in other.children_ended:
                if (
                    rec.get("pid") == pid
                    and started <= base + (rec.get("t") or 0) / 1e9
                ):
                    sent, sender = rec, other.index
            for rec in other.children_exited:
                if (
                    rec.get("pid") == pid
                    and started <= base + (rec.get("t") or 0) / 1e9
                ):
                    seen = rec
        if seen is not None and (
            sent is None or sent.get("signal") != seen.get("signal")
        ):
            proc.ending = {
                "signal": seen.get("signal"),
                "by": "outside",
                "process": None,
            }
        elif sent is not None:
            proc.ending = {
                "signal": sent.get("signal"),
                "by": sent.get("by"),
                "process": sender,
            }


def _missing_children(processes: list[Process]) -> None:
    """A process started Python programs that are not in the recording: said,
    with how many. The I/O events of the process say how many it started;
    the files of the recording say how many reported."""
    for proc in processes:
        started = 0
        first: Record | None = None
        for event in proc.io:
            if (
                event.get("kind") == "subprocess"
                and event.get("op") == "spawn"
                and is_python(str(event.get("target")))
            ):
                started += int(event.get("n", 1))
                first = first or event
        if not started or first is None:
            continue
        here = sum(
            1
            for other in processes
            if other.parent == proc.index and not other.header.get("forked")
        )
        if started > here:
            place = f"{os.path.basename(str(first.get('file')))}:{first.get('line')}"
            proc.not_collected.append(
                _note(
                    f"{started - here} of the {started} Python programs that "
                    f"process {proc.index} started (the first at {place})",
                    "they are not in the recording: they ran a Python that has "
                    "no ormaos installed, or that was started without its site "
                    "packages (`-S`), or without ORMAOS_DIR in its environment",
                )
            )


def _ending_notes(processes: list[Process]) -> None:
    for proc in processes:
        if proc.end is not None:
            continue
        pid = proc.header.get("pid", "?")
        if proc.lost:
            proc.not_collected.append(
                _note(
                    f"the last events of process {pid}",
                    f"it {proc.ending_text} before it had written everything: "
                    "what it wrote is here, the calls that had not returned are "
                    "marked open, and up to 256 records or what happened in its "
                    "last 0.2 seconds can be missing",
                )
            )
        else:
            proc.not_collected.append(
                _note(
                    f"the end record of process {pid}",
                    f"it {proc.ending_text}; it had no event left to write. What "
                    "a process writes when it ends is missing: its duration, CPU "
                    "and memory, and the counts of what it saw and did not record",
                )
            )


def _overview_addresses(
    proc: Process, prefix: str, calls: int, exceptions: int
) -> None:
    """The addresses an overview shows, without the call records: a call's
    address is its place among the calls of the recording, in the order of
    their ids."""
    ids = proc.call_ids

    def of(call_id: Any) -> str | None:
        if type(call_id) is not int:
            return None
        at = bisect_left(ids, call_id)
        if at < len(ids) and ids[at] == call_id:
            return f"{prefix}:c{calls + at + 1}"
        return None

    if len(ids):
        proc.first_call = f"{prefix}:c{calls + 1}"
    exc_addr: dict[Any, str] = {}
    for number, exc in enumerate(proc.exceptions, start=exceptions + 1):
        exc["addr"] = exc_addr[exc.get("id")] = f"{prefix}:e{number}"
        exc["process"] = proc.index
    for exc in proc.exceptions:
        exc["call_addr"] = of(exc.get("at", {}).get("call"))
        exc["handled_addr"] = of(exc.get("handled", {}).get("call"))
        for key in ("during", "replaced_by"):
            if key in exc:
                exc[f"{key}_addr"] = exc_addr.get(exc[key])


def load(path: str, overview: bool = False) -> Recording:
    """Read a recording. With ``overview``, for the answers that need counts
    and exceptions only (the summary, `show`, `list`, `status`): the files are
    read line by line and no call or I/O record is kept, so a large recording
    takes little memory. `calls` and `io` are then empty; use the counts."""
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
        _load_process(os.path.join(path, name), overview)
        for name in sorted(os.listdir(path))
        if name.startswith("p") and name.endswith(".jsonl")
    ]
    processes.sort(
        key=lambda p: (p.header.get("started", 0.0), p.header.get("pid", 0), p.path)
    )
    _link(processes)
    # The command itself: `ormaos run` saw the signal that ended it.
    signum = (recording.end or {}).get("signal")
    command_pid = (recording.end or {}).get("command_pid")
    for proc in processes:
        if (
            signum
            and proc.end is None
            and proc.ending is None
            and proc.header.get("pid") == command_pid
        ):
            proc.ending = {"signal": signum, "by": "outside", "process": None}
    _ending_notes(processes)
    _missing_children(processes)
    prefix = recording.name
    call_n = 0
    exc_n = 0
    io_n = 0
    for index, proc in enumerate(processes, start=1):
        proc.index = index
        if overview:
            _overview_addresses(proc, prefix, call_n, exc_n)
            call_n += proc.n_calls
            exc_n += len(proc.exceptions)
            recording.not_collected.extend(proc.not_collected)
            continue
        call_addr: dict[Any, str] = {}
        for call in proc.calls:
            call_n += 1
            call["addr"] = call_addr[call.get("id")] = f"{prefix}:c{call_n}"
            call["process"] = index
        if proc.calls:
            proc.first_call = proc.calls[0]["addr"]
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
        {
            str(s)
            for proc in processes
            if isinstance(proc.header.get("path_scope"), list)
            for s in proc.header["path_scope"]
        }
    )
    if scopes and not any(proc.has_path for proc in processes):
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
