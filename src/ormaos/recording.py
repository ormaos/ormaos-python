"""Read a recording from disk.

A recording is a directory ``.ormaos/r<N>/`` with one ``run.jsonl`` (written
by ``ormaos run``) and one ``p<pid>-<time>.jsonl`` per Python process. Every
file starts with a header line carrying ``"format": "provisional-0"``.

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


def _note(what: str, reason: str) -> Record:
    return {"k": "not_collected", "what": what, "reason": reason}


def _load_process(path: str) -> Process:
    records, bad = _read_lines(path)
    header: Record = {}
    if records and records[0].get("kind") == "process":
        header = records.pop(0)
    proc = Process(index=0, path=path, header=header)
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
    if bad:
        proc.not_collected.append(
            _note(f"{bad} lines of {name}", "the lines are not valid JSON")
        )
    if proc.end is None:
        proc.not_collected.append(
            _note(
                f"the last events of process {header.get('pid', '?')}",
                "the process ended without running its exit handlers "
                "(killed, os._exit, or still running)",
            )
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


def load(path: str) -> Recording:
    match = _DIR_NAME.fullmatch(os.path.basename(os.path.normpath(path)))
    number = int(match.group(1)) if match else 0
    recording = Recording(number=number, path=path, header={})
    run_path = os.path.join(path, "run.jsonl")
    if os.path.isfile(run_path):
        records, _bad = _read_lines(run_path)
        for rec in records:
            if rec.get("kind") == "run":
                recording.header = rec
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
