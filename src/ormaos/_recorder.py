"""The recorder. It runs inside the observed Python process.

Rules this module keeps (see AGENTS.md):

- Fail open. Every callback is guarded. On an internal error, recording
  stops, the reason is written as a ``not_collected`` record, and the
  program goes on unchanged.
- Never change the state of the observed program. We keep no reference to
  the program's objects (frames, exceptions, arguments): only ``id()``
  numbers and our own records. We never print.
- Shape always, value never. See ``_shape.py``.

Imports are kept small on purpose: this module is loaded at interpreter
start in every recorded process.
"""

import _thread
import atexit
import itertools
import json
import os
import sys
import time
from _weakref import ref as _weakref
from types import CodeType, FrameType, ModuleType
from typing import Any

from ormaos._shape import SAMPLE, shape, type_name

FORMAT = "provisional-0"

_mon = sys.monitoring
_EV = _mon.events
_DISABLE: Any = _mon.DISABLE

# Read exception attributes through the base class, so a subclass that
# overrides them cannot run its own code inside our callbacks.
_get_traceback = BaseException.__dict__["__traceback__"].__get__
_get_args = BaseException.__dict__["args"].__get__

# Before 3.13, `frame.f_locals` of a function is a dict copied from the frame.
_LOCALS_ARE_A_COPY = sys.version_info < (3, 13)
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep
_STDLIB_DIR = os.path.dirname(os.path.abspath(os.__file__)) + os.sep
_SITE_MARKS = (f"site-packages{os.sep}", f"dist-packages{os.sep}")
_USER_PSEUDO_FILES = ("<string>", "<stdin>")

# Library functions whose start and return the I/O recorder looks at.
_IO_NAMES = frozenset({"getresponse", "_send_single_request"})

# A chunk is written when this many records wait, when the oldest has waited
# this long (checked when the next record arrives), and when a call that has
# no call of the user's code above it ends. A process that is killed loses at
# most what was waiting.
_FLUSH_EVERY = 256
_FLUSH_AFTER_NS = 200_000_000
# The function a multiprocessing child runs in; it ends with os._exit, which
# skips the exit handlers, so the recording is closed when it returns.
_MP_BOOTSTRAP = ("_bootstrap", os.sep + os.path.join("multiprocessing", "process.py"))
# The function through which multiprocessing sends a signal to a child it
# started (`Process.terminate`, `Process.kill`, the end of a `Pool`). The
# parent writes down that it ended that child itself.
_MP_SIGNAL = ("_send_signal", os.sep + os.path.join("multiprocessing", "popen_fork.py"))
# Library functions that are watched, by name and file: what a watched
# function is for. `sent`: the program sends a signal to a child it started.
# `exit`: the program learns how a child ended. `bootstrap`: see above.
_SUBPROCESS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(os.__file__)), "subprocess.py"
)
_WATCHED: dict[str, tuple[str, str]] = {
    _MP_BOOTSTRAP[0]: (_MP_BOOTSTRAP[1], "bootstrap"),
    _MP_SIGNAL[0]: (_MP_SIGNAL[1], "sent"),
    "poll": (_MP_SIGNAL[1], "exit"),
    "send_signal": (_SUBPROCESS_FILE, "sent"),
    "_handle_exitstatus": (_SUBPROCESS_FILE, "exit"),
}
_MP_POOL = os.sep + os.path.join("multiprocessing", "pool.py")
_KERNEL_CELL = os.sep + "ipykernel_"
# The recorder that is recording in this process, if any.
ACTIVE: "Recorder | None" = None
_MAX_PENDING_HANDLED = 1000
_MAX_CLAUSES = 64
_MAX_PLACE_NOTES = 20

_ALWAYS_NOT_COLLECTED = [
    {
        "what": "values",
        "reason": "by design: only type, length and structure are recorded",
    },
    {
        "what": "calls outside user code",
        "reason": "the standard library and installed packages are not recorded",
    },
    {
        "what": "code compiled from a string under another name",
        "reason": "code made with `compile` or `exec` is the program's when its "
        "file name is `<string>`, `<stdin>` or a notebook cell; under any other "
        "made-up name it is not recorded",
    },
    {
        "what": "code of the program that runs in the middle of the recorder",
        "reason": "a signal handler, a `__del__` or a weak reference callback "
        "that Python runs while Ormaos is noting an event is not recorded "
        "(Python reports no events then)",
    },
    {
        "what": "exceptions that never pass through user code",
        "reason": "only exceptions seen in a user-code frame are recorded",
    },
    {
        "what": "which `except*` clause matched",
        "reason": "for `except*` the handler line is the first clause of the "
        "`try`; several clauses can each take a part of the group, and which "
        "ones did is not recorded (the place is listed when it happens)",
    },
]
_PATH_OFF = {
    "what": "path",
    "reason": "line-by-line recording is off; ask for a scope with "
    "`ormaos run --path <file.py:function>`",
}

type _Call = dict[str, Any] | None
# One `except` clause: first offset, last offset, line.
type _Clause = tuple[int, int, int]
type _Entry = tuple[CodeType, _Call]
_OPEN_CALL_KEYS = ("k", "id", "parent", "fn", "file", "line", "thread", "t", "args")


class _Flight:
    """One exception, from the first time we see it until it is handled."""

    __slots__ = (
        "back",
        "during",
        "frame",
        "key",
        "rec",
        "seen",
        "unwound",
        "written",
    )

    def __init__(self, key: int, rec: dict[str, Any]) -> None:
        self.key = key  # id() of the exception object
        self.rec = rec
        self.unwound = 0  # id() of the last frame it left, 0 if none yet
        self.frame = 0  # id() of the frame whose handler caught it
        self.seen = 0  # id() of the frame it was last raised in
        self.back = 0  # id() of the caller of the last frame it left
        self.during: _Flight | None = None  # the exception being handled
        self.written = 0  # in the last checkpoint: 1 as in flight, 2 as handled


class _ThreadState:
    __slots__ = ("index", "stack")

    def __init__(self, index: int) -> None:
        self.index = index
        self.stack: list[_Entry] = []


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ[name]))
    except (KeyError, ValueError):
        return default


def _parse_scope(text: str) -> tuple[str | None, str | None]:
    """A path scope: ``file.py``, ``file.py:function`` or ``function``."""
    if ":" in text:
        left, right = text.rsplit(":", 1)
        if left and right and os.sep not in right and not right.endswith(".py"):
            return os.path.normpath(left), right
    if text.endswith(".py") or os.sep in text or "/" in text:
        return os.path.normpath(text), None
    return None, text


def _injected_fault(*_args: object) -> Any:
    raise RuntimeError("fault injected by ORMAOS_FAULT")


def _argv_shape() -> dict[str, Any]:
    """How Python was started, without the content of ``-c`` or the arguments."""
    orig = sys.orig_argv
    mode = "other"
    target: str | None = None
    i = 1
    while i < len(orig):
        arg = orig[i]
        short = arg.startswith("-") and not arg.startswith("--")
        if short and arg.endswith("c"):
            mode = "command"
            break
        if short and arg.endswith("m"):
            mode = "module"
            target = orig[i + 1] if i + 1 < len(orig) else None
            break
        if arg in ("-W", "-X", "--check-hash-based-pycs"):
            i += 2
            continue
        if arg.startswith("-") and arg != "-":
            i += 1
            continue
        mode = "script"
        target = arg
        break
    return {"mode": mode, "target": target, "argc": len(orig)}


_OPCODES: tuple[int, ...] | None = None
_OPNAMES = (
    "CHECK_EXC_MATCH",
    "POP_TOP",
    "RERAISE",
    "EXTENDED_ARG",
    "CHECK_EG_MATCH",
    "PUSH_EXC_INFO",
    "POP_EXCEPT",
    "WITH_EXCEPT_START",
)


def _opcodes() -> tuple[int, ...]:
    """The numbers of the opcodes in ``_OPNAMES``. Imported when needed."""
    global _OPCODES
    if _OPCODES is None:
        import opcode

        _OPCODES = tuple(opcode.opmap[name] for name in _OPNAMES)
    return _OPCODES


def _line_at(code: CodeType, offset: int) -> int | None:
    for start, end, line in code.co_lines():
        if start <= offset < end:
            return line
    return None


def _clauses(code: CodeType, offset: int) -> tuple[list[_Clause], bool]:
    """The `except` clauses of the handler that starts at ``offset``, and
    whether it is an `except*` handler.

    A clause is ``<load the types> CHECK_EXC_MATCH POP_JUMP_IF_FALSE next``.
    For each one: the first and the last offset of that part (while a frame
    is there, the clause is being tried) and the line of its `except`. A bare
    `except:` at the end starts with POP_TOP. The list is empty for `finally`,
    `with` and a `try` with only a bare `except:`.
    """
    check, pop_top, reraise, extended, star, push, pop_except, with_start = _opcodes()
    ends = (check, star, reraise, push, pop_except, with_start)
    raw = code.co_code
    end = len(raw)
    lines = list(code.co_lines())
    out: list[_Clause] = []
    start = offset
    while start < end and len(out) < _MAX_CLAUSES:
        pos = start + 2 if not out and raw[start] == push else start
        if out and raw[pos] == pop_top:
            bare = _line_at(code, pos)
            if bare is not None:
                out.append((pos, pos, bare))
            break
        scan = pos
        while scan < end and raw[scan] not in ends:
            scan += 2
        if scan >= end:
            break
        if raw[scan] == star:
            return [], True
        line = _line_at(code, scan)
        if raw[scan] != check or line is None:
            break
        # The types of a clause are written on the line of its `except` or
        # below it. Anything above is other code (the body of a `finally`).
        if any(
            at is not None and at < line
            for first, last, at in lines
            if first < scan and last > pos
        ):
            break
        jump = scan + 2
        arg = 0
        while jump < end and raw[jump] == extended:
            arg = (arg | raw[jump + 1]) << 8
            jump += 2
        if jump >= end:
            break
        arg |= raw[jump + 1]
        after = jump + 2
        while after < end and raw[after] == 0:  # inline cache entries
            after += 2
        out.append((start, jump, line))
        start = after + 2 * arg
        if start >= end or raw[start] == reraise:
            break
    return out, False


def _handler_line(code: CodeType, offset: int, fallback: int) -> int:
    """The source line of the handler that starts at ``offset``."""
    for _start, end, line in code.co_lines():
        if end > offset and line is not None:
            return line
    return fallback


def _unhook(tb: Any) -> None:
    """An exception arrived in the program while one of our callbacks was
    running (a KeyboardInterrupt, or what a signal handler of the program
    raised). Its traceback then has our frames in the middle; they are taken
    out, so that the program sees and prints the traceback it would have
    without us: its own line, then the handler's frames if there are any."""
    entry = tb.tb_next
    while entry is not None and entry.tb_frame.f_code.co_filename.startswith(
        (_PACKAGE_DIR, _STDLIB_DIR, "<frozen")
    ):
        entry = entry.tb_next
    tb.tb_next = entry


def _plain_attribute(obj: object, name: str) -> object:
    """An attribute kept in the object's own dict: no property, no
    `__getattr__` of the program is run to read it."""
    try:
        return object.__getattribute__(obj, "__dict__").get(name)
    except Exception:
        return None


class Recorder:
    def __init__(
        self,
        directory: str,
        scopes: list[str] | None = None,
        io: bool | None = None,
        started_by: str = "run",
    ) -> None:
        self._dir = directory
        self._started_by = started_by
        # Re-entrant: a signal handler of the program can run in the middle of
        # a write and make calls of its own, on the same thread.
        self._lock = _thread.RLock()
        self._buf: list[dict[str, Any]] = []
        self._fd = -1
        self._tool = -1
        self._dead = False
        self._finished = False
        self._pid = os.getpid()
        self._t0 = time.perf_counter_ns()
        # The thread that is writing the recording right now, 0 if none. The
        # I/O events that this writing causes itself are not recorded.
        self._owner = 0
        self._io: Any = None
        self._io_on = os.environ.get("ORMAOS_IO", "1") != "0" if io is None else io
        self._last_write = self._t0
        self._ck_call = 0  # the highest call id already in a checkpoint
        self._base: int | None = None  # see _MP_BOOTSTRAP
        self._cells_noted = False
        self._sampling_noted = False
        # The threads that exist when recording starts late (a block): what
        # they are in the middle of was not seen.
        self._old_threads: frozenset[int] = frozenset()
        self._old_threads_noted = False
        self._exits_seen: set[tuple[int, int]] = set()
        self._finishing = False
        self._failure: dict[str, Any] | None = None
        self._at_limit = 0
        self._mask = 0
        # asyncio tasks: id(task) -> the number we gave it. A weak reference
        # forgets the task when it is gone, so a reused id() gets a new number.
        self._aio: Any = None
        self._tasks: dict[int, int] = {}
        self._task_refs: dict[int, Any] = {}
        self._ntasks = 0
        self._user_files: dict[str, bool] = {}
        # One state per thread, kept in a thread-local so that a new thread
        # never gets the state (and the number) of one that has ended, even
        # when it gets the same identifier.
        self._tls = _thread._local()
        self._threads: dict[int, _ThreadState] = {}
        self._suspended: dict[int, _Entry] = {}
        self._active: dict[int, _Flight] = {}
        self._handled: dict[int, _Flight] = {}
        # Frames where we still have to see which `except` clause matches:
        # id(frame) -> (the exception, the code, the clause lines of the code).
        self._watching: dict[int, tuple[_Flight, CodeType, list[_Clause]]] = {}
        self._clauses: dict[tuple[CodeType, int], tuple[list[_Clause], bool]] = {}
        self._star_noted: set[tuple[str, int]] = set()
        self._call_ids = itertools.count(1)
        self._exc_ids = itertools.count(1)
        self._ncalls = 0
        self._nexc = 0
        self._max_calls = _int_env("ORMAOS_MAX_CALLS", 200_000)
        self._max_exc = _int_env("ORMAOS_MAX_EXCEPTIONS", 50_000)
        self._calls_capped = False
        self._exc_capped = False
        # Line-by-line recording, only inside the scopes of ORMAOS_PATH.
        if scopes is None:
            scopes = os.environ.get("ORMAOS_PATH", "").split(",")
        self._scope_text = [part.strip() for part in scopes if part.strip()]
        self._scopes = [_parse_scope(part) for part in self._scope_text]
        self._scope_known: dict[tuple[str, str], bool] = {}
        self._path_codes: set[CodeType] = set()
        self._nlines = 0
        self._max_lines = _int_env("ORMAOS_MAX_LINES", 200_000)
        self._lines_capped = False
        only = os.environ.get("ORMAOS_ONLY", "")
        self._only = tuple(
            os.path.abspath(part).rstrip(os.sep)
            for part in only.split(os.pathsep)
            if part
        )

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        global ACTIVE
        try:
            if self._started_by == "record":
                here = _thread.get_ident()
                self._old_threads = frozenset(
                    ident for ident in sys._current_frames() if ident != here
                )
            self._open(forked=False)
            ACTIVE = self
            atexit.register(self.finish)
            if hasattr(os, "register_at_fork"):
                os.register_at_fork(after_in_child=self._after_fork)
            tool = next((t for t in (4, 3) if _mon.get_tool(t) is None), None)
            if tool is None:
                self._note(
                    "everything",
                    "no free sys.monitoring tool id (3 and 4 are in use)",
                )
                return
            _mon.use_tool_id(tool, "ormaos")
            self._tool = tool
            mask = 0
            for event, callback in self._callbacks():
                _mon.register_callback(tool, event, callback)
                mask |= event
            # LINE is never on everywhere: only for the code objects inside a
            # path scope, and for one code object for the few lines after an
            # exception enters a `try` with several clauses.
            _mon.register_callback(tool, _EV.LINE, self._line)
            if self._io_on:
                from ormaos._iorec import IoRecorder

                self._io = IoRecorder(self)
                mask |= self._io.start(tool)
            self._install_fault()
            _mon.set_events(tool, mask)
            self._mask = mask
        except Exception as err:
            self._fail(err)

    def _install_fault(self) -> None:
        """Test hook: make one internal step fail, to prove that we fail open."""
        fault = os.environ.get("ORMAOS_FAULT", "")
        if fault == "calls":
            setattr(self, "_args", _injected_fault)  # noqa: B010
        elif fault == "exceptions":
            setattr(self, "_origin", _injected_fault)  # noqa: B010
        elif fault == "io" and self._io is not None:
            setattr(self._io, "_site", _injected_fault)  # noqa: B010
        elif fault == "path":
            setattr(self, "_in_scope", _injected_fault)  # noqa: B010
        elif fault == "write":
            setattr(self, "_write_out", _injected_fault)  # noqa: B010

    def _callbacks(self) -> list[tuple[int, Any]]:
        return [
            (_EV.PY_START, self._py_start),
            (_EV.PY_RETURN, self._py_return),
            (_EV.PY_UNWIND, self._py_unwind),
            (_EV.PY_YIELD, self._py_yield),
            (_EV.PY_RESUME, self._py_resume),
            (_EV.PY_THROW, self._py_throw),
            (_EV.RAISE, self._raise),
            (_EV.RERAISE, self._reraise),
            (_EV.EXCEPTION_HANDLED, self._exception_handled),
        ]

    def _stop(self) -> None:
        tool = self._tool
        if tool < 0:
            return
        self._tool = -1
        _mon.set_events(tool, 0)
        if self._io is not None:
            self._io.stop(tool)
        for _flight, code, _lines in list(self._watching.values()):
            _mon.set_local_events(tool, code, 0)
        self._watching = {}
        for code in self._path_codes:
            _mon.set_local_events(tool, code, 0)
        self._path_codes = set()
        for event, _callback in self._callbacks():
            _mon.register_callback(tool, event, None)
        _mon.register_callback(tool, _EV.LINE, None)
        _mon.free_tool_id(tool)

    def _open(self, forked: bool) -> None:
        self._pid = os.getpid()
        path = os.path.join(self._dir, f"p{self._pid}-{time.time_ns()}.jsonl")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
        self._fd = os.open(path, flags, 0o600)
        # The header has two parts. `core` is what any recorder of any
        # language would write: the process, the capabilities recorded, what
        # is never recorded, and the anchor that ties the event times (`t`,
        # nanoseconds on a monotonic clock) to the wall clock. `python` is
        # what only this recorder knows.
        since = time.perf_counter_ns() - self._t0
        self._write(
            [
                {
                    "format": FORMAT,
                    "kind": "process",
                    "core": {
                        "pid": self._pid,
                        "ppid": os.getppid(),
                        "cwd": os.getcwd(),
                        "anchor": {
                            "t": 0,
                            "unix": time.time() - since / 1e9,
                            "clock": "monotonic, nanoseconds",
                        },
                        "capabilities": self._capabilities(),
                        "not_collected": self._always_not_collected(),
                    },
                    "python": {
                        "version": ".".join(str(n) for n in sys.version_info[:3]),
                        "implementation": sys.implementation.name,
                        "executable": sys.executable,
                        "argv": _argv_shape(),
                        "forked": forked,
                        "started_by": self._started_by,
                        "path_scope": self._scope_text,
                    },
                }
            ]
        )

    def _capabilities(self) -> list[str]:
        out = ["context", "calls", "exceptions"]
        if self._scopes:
            out.append("path")
        if self._io_on:
            out.append("io")
        return out

    def _always_not_collected(self) -> list[dict[str, str]]:
        from ormaos._iorec import NOT_COLLECTED, NOT_COLLECTED_OFF

        notes = list(_ALWAYS_NOT_COLLECTED)
        notes.extend(NOT_COLLECTED if self._io_on else NOT_COLLECTED_OFF)
        if self._scopes:
            notes.append(
                {
                    "what": "path outside the scope",
                    "reason": "lines are recorded only inside: "
                    + ", ".join(self._scope_text),
                }
            )
        else:
            notes.append(_PATH_OFF)
        return notes

    def _after_fork(self) -> None:
        """In a forked child: start a file of its own, forget the parent's state."""
        if self._dead or self._finished:
            return
        try:
            self._lock = _thread.RLock()
            self._tls = _thread._local()
            self._buf = []
            self._threads = {}
            self._suspended = {}
            self._active = {}
            self._handled = {}
            for _flight, code, _lines in list(self._watching.values()):
                if code not in self._path_codes:
                    _mon.set_local_events(self._tool, code, 0)
            self._watching = {}
            self._ncalls = 0
            self._nlines = 0
            self._owner = 0
            if self._io is not None:
                self._io.reset()
            self._nexc = 0
            self._tasks = {}
            self._task_refs = {}
            self._ck_call = 0
            self._exits_seen = set()
            self._old_threads = frozenset()
            self._t0 = time.perf_counter_ns()
            self._last_write = self._t0
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            self._open(forked=True)
        except Exception as err:
            self._fail(err)

    def finish(self) -> None:
        """Write what is still open, and the end record. Runs at exit."""
        global ACTIVE
        if self._finished or os.getpid() != self._pid:
            return
        self._finished = True
        if ACTIVE is self:
            ACTIVE = None
        try:
            self._tool_check()
            self._stop()
        except Exception:
            pass
        # What the end adds (the records still open, the notes, the end
        # record) is written in one piece: a process that is ended by a signal
        # while it finishes leaves either all of it or none of it.
        self._finishing = True
        try:
            if not self._dead:
                self._finish_records()
        except Exception as err:
            self._fail(err)
        if self._failure is not None:
            self._buf.append(self._failure)  # it could not be written then
            self._failure = None
        if self._at_limit:
            self._buf.append(
                {
                    "k": "not_collected",
                    "what": "events at the recursion limit",
                    "reason": f"{self._at_limit} times the program was at its "
                    "recursion limit when an event arrived, and the recorder had "
                    "no room to note it: calls and exceptions around that point "
                    "can be missing, or be shown without their end",
                }
            )
        try:
            lines = [json.dumps(rec, separators=(",", ":")) for rec in self._buf]
            self._buf = []
        except Exception as err:
            lines = []
            self._fail(err)
        try:
            # The end record is written even after a failure.
            lines.append(json.dumps(self._end_record(), separators=(",", ":")))
            Recorder._write_out(self, ("\n".join(lines) + "\n").encode("utf-8"))
        except Exception:
            pass
        try:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
        except Exception:
            pass

    def _tool_check(self) -> None:
        """Did the program change the events of our monitoring tool?"""
        tool = self._tool
        if tool < 0 or self._dead:
            return
        try:
            now = _mon.get_events(tool)
        except Exception:
            now = -1
        if now != self._mask:
            self._buf.append(
                {
                    "k": "not_collected",
                    "what": "what ran after the program changed the monitoring tool",
                    "reason": f"Ormaos records through sys.monitoring tool {tool}; "
                    "when the process ended, the events of that tool were not the "
                    "ones Ormaos had set (the program set, cleared or freed them), "
                    "so calls and exceptions after that change are missing",
                }
            )

    def block_left(self, exc: BaseException) -> None:
        """An exception is leaving a `with ormaos.record():` block. The `with`
        statement reported it as handled; it goes on, so it is in flight."""
        try:
            flight = self._handled.pop(id(exc), None)
            if flight is not None:
                flight.rec.pop("handled", None)
                flight.unwound = 0
                self._active[id(exc)] = flight
        except Exception as err:
            self._fail(err)

    def _finish_records(self) -> None:
        if self._io is not None:
            self._io.finish()
        for flight in list(self._handled.values()):
            self._finish_handled(flight)
        self._handled = {}
        last = getattr(sys, "last_exc", None)
        last_key = id(last) if last is not None else 0
        for flight in list(self._active.values()):
            end = "uncaught" if flight.key == last_key else "no_handler_seen"
            flight.rec["end"] = end
            self._buf.append(flight.rec)
        self._active = {}
        now = time.perf_counter_ns() - self._t0
        for state in list(self._threads.values()):
            for _code, rec in state.stack:
                if rec is not None:
                    rec["dur"] = now - rec["t"]
                    rec["out"] = "open"
                    self._buf.append(rec)
        for _code, rec in list(self._suspended.values()):
            if rec is not None:
                rec["dur"] = now - rec["t"]
                rec["out"] = "suspended"
                self._buf.append(rec)

    def _end_record(self) -> dict[str, Any]:
        times = os.times()
        end: dict[str, Any] = {
            "k": "end",
            "dur": time.perf_counter_ns() - self._t0,
            "cpu_user": times.user,
            "cpu_system": times.system,
            "threads": len(self._threads),
            "calls": self._ncalls,
            "exceptions": self._nexc,
            "lines": self._nlines,
            "io": self._io.count() if self._io is not None else 0,
            "stopped_early": self._dead,
        }
        try:
            import resource

            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            end["max_rss_kb"] = rss // 1024 if sys.platform == "darwin" else rss
        except Exception:
            pass
        return end

    # -------------------------------------------------------------- writing

    def _write_out(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self._fd, view) :]

    def _write(self, records: list[dict[str, Any]]) -> None:
        lines = [json.dumps(rec, separators=(",", ":")) for rec in records]
        self._write_out(("\n".join(lines) + "\n").encode("utf-8"))

    def _emit(self, rec: dict[str, Any], now_too: bool = False) -> None:
        with self._lock:
            before = self._owner
            self._owner = _thread.get_ident()
            try:
                if self._dead and not self._finishing:
                    return  # recording has stopped
                self._buf.append(rec)
                if self._finishing:
                    return  # `finish` writes everything at once
                now = time.perf_counter_ns()
                if (
                    now_too
                    or len(self._buf) >= _FLUSH_EVERY
                    or now - self._last_write >= _FLUSH_AFTER_NS
                ):
                    buf = self._buf
                    self._buf = []
                    self._last_write = now
                    buf.append(self._checkpoint(now))
                    self._write(buf)
            finally:
                self._owner = before

    def _flush(self) -> None:
        with self._lock:
            before = self._owner
            self._owner = _thread.get_ident()
            try:
                if self._buf:
                    buf = self._buf
                    self._buf = []
                    self._write(buf)
            finally:
                self._owner = before

    def _flush_now(self) -> None:
        """Write what waits, and a checkpoint, without a new record."""
        if self._finishing:
            return
        with self._lock:
            before = self._owner
            self._owner = _thread.get_ident()
            try:
                now = time.perf_counter_ns()
                buf = self._buf
                self._buf = []
                self._last_write = now
                buf.append(self._checkpoint(now))
                self._write(buf)
            finally:
                self._owner = before

    def _checkpoint(self, now: int) -> dict[str, Any]:
        """What is still open when a chunk is written: the calls that have
        not returned and the exceptions whose end is not known yet. If the
        process is killed, the reader finishes the recording from the last
        checkpoints. Each open call is written once, each exception once per
        state. Only copies are written: other threads own the records."""
        calls: list[dict[str, Any]] = []
        seen = self._ck_call
        highest = seen
        entries: list[_Entry] = []
        for state in list(self._threads.values()):
            entries.extend(list(state.stack))
        entries.extend(list(self._suspended.values()))
        for _code, rec in entries:
            if rec is None or rec["id"] <= seen:
                continue
            highest = max(highest, rec["id"])
            copy = {key: rec[key] for key in _OPEN_CALL_KEYS}
            if "task" in rec:
                copy["task"] = rec["task"]
            calls.append(copy)
        self._ck_call = highest
        exceptions: list[dict[str, Any]] = []
        for state_id, flights in ((1, self._active), (2, self._handled)):
            for flight in list(flights.values()):
                if flight.written != state_id:
                    flight.written = state_id
                    copy = dict(flight.rec)
                    for key in ("origin", "at", "handled"):
                        if key in copy:
                            copy[key] = dict(copy[key])
                    exceptions.append(copy)
        # `idle`: with this chunk, everything that happened is written. No
        # call of the user's code is running or suspended, no exception is in
        # flight, no I/O event is being counted. The next thing that happens
        # starts with a call that is written at once, so a file that ends
        # with an idle checkpoint lost nothing, however the process ended.
        idle = not self._active and (self._io is None or not self._io.busy())
        if idle:
            for _code, rec in entries:
                if rec is None or rec["id"] != self._base:
                    idle = False
                    break
        out: dict[str, Any] = {"k": "checkpoint", "t": now - self._t0, "idle": idle}
        if calls:
            out["calls"] = calls
        if exceptions:
            out["exceptions"] = exceptions
        return out

    def _note(self, what: str, reason: str) -> None:
        self._emit({"k": "not_collected", "what": what, "reason": reason}, True)

    def _fail(self, err: BaseException) -> None:
        """An exception reached one of our callbacks. If it is the program's
        own (raised by a signal handler that ran in the middle of a callback),
        it goes on to the program. Otherwise it is an internal error: stop
        recording, keep the program running."""
        if self._is_the_programs(err):
            raise err
        if isinstance(err, RecursionError):
            # The program is at its recursion limit and our callback had no
            # room left. That one event is not noted; recording goes on, and
            # the recording says how often this happened.
            self._at_limit += 1
            return
        if self._dead:
            return
        self._dead = True
        where = "?"
        try:
            tb = err.__traceback__
            if tb is not None:
                while tb.tb_next is not None:
                    tb = tb.tb_next
                name = os.path.basename(tb.tb_frame.f_code.co_filename)
                where = f"{name}:{tb.tb_lineno}"
        except BaseException:
            pass
        kind = type(err).__name__
        extra = ""
        # Kept, and written when the process ends, if it cannot be written
        # now (at the recursion limit there is no room to do anything).
        self._failure = {
            "k": "not_collected",
            "what": "everything after this point",
            "reason": (
                f"internal error in ormaos ({kind} at {where}){extra}; "
                "recording stopped, the program was not affected"
            ),
        }
        try:
            self._stop()
        except BaseException:
            pass
        try:
            # Bypass the test hook on purpose: say why, if the file still works.
            buf = [*self._buf, self._failure]
            lines = [json.dumps(rec, separators=(",", ":")) for rec in buf]
            Recorder._write_out(self, ("\n".join(lines) + "\n").encode("utf-8"))
            self._buf = []
            self._failure = None
        except BaseException:
            pass

    @staticmethod
    def _is_the_programs(err: BaseException) -> bool:
        """Did code of the program raise this while one of our callbacks was
        running? We never call the program's code, so a frame that is neither
        ours nor the standard library's, below one of ours, is a signal
        handler of the program (or another function Python ran between two of
        our instructions)."""
        try:
            tb = err.__traceback__
            ours = False
            while tb is not None:
                name = tb.tb_frame.f_code.co_filename
                if name.startswith(_PACKAGE_DIR):
                    ours = True
                elif ours and not name.startswith((_STDLIB_DIR, "<frozen")):
                    return True
                tb = tb.tb_next
        except BaseException:
            pass
        return False

    # ------------------------------------------------------------- helpers

    def _user(self, code: CodeType) -> bool:
        """Is this code the user's? Call it only directly from a callback."""
        filename = code.co_filename
        known = self._user_files.get(filename)
        if known is None:
            if filename.startswith("<"):
                # "<string>" is `python -c` but also every exec() of a string:
                # decide by the module the code runs in.
                return self._user_pseudo(filename, sys._getframe(2))
            known = self._user_files[filename] = self._is_user_path(filename)
        return known

    def _user_at(self, filename: str, frame: FrameType) -> bool:
        if filename.startswith("<"):
            return self._user_pseudo(filename, frame)
        known = self._user_files.get(filename)
        if known is None:
            known = self._user_files[filename] = self._is_user_path(filename)
        return known

    def _user_pseudo(self, filename: str, frame: FrameType) -> bool:
        # "<ipython-input-...>" is a cell of an IPython session or a notebook.
        if filename not in _USER_PSEUDO_FILES and not filename.startswith(
            "<ipython-input-"
        ):
            return False
        module = dict.get(frame.f_globals, "__name__")
        if type(module) is not str:
            return False
        key = f"<module {module}>"
        known = self._user_files.get(key)
        if known is None:
            if module == "__main__":
                known = not self._only
            else:
                found = sys.modules.get(module)
                file = (
                    found.__dict__.get("__file__")
                    if type(found) is ModuleType
                    else None
                )
                known = type(file) is str and self._is_user_path(file)
            self._user_files[key] = known
        return known

    def _is_user_path(self, filename: str) -> bool:
        path = os.path.abspath(filename)
        if self._only:
            return any(path == p or path.startswith(p + os.sep) for p in self._only)
        if path.startswith((_PACKAGE_DIR, _STDLIB_DIR)):
            return False
        if any(mark in path for mark in _SITE_MARKS):
            return False
        if not self._cells_noted and _KERNEL_CELL in path and path.endswith(".py"):
            # A Jupyter kernel compiles each cell under a temporary file name.
            self._cells_noted = True
            self._note(
                "which notebook cell a call is in",
                "the kernel gives every cell a temporary file name "
                f"(...{_KERNEL_CELL}<pid>{os.sep}<number>.py); calls and exceptions "
                "are recorded under that name, not under the cell's number, and "
                "the file is gone when the kernel stops",
            )
        return True

    def _task(self) -> int | None:
        """The number of the asyncio task that is running here, if any."""
        events = self._aio
        if events is None:
            events = sys.modules.get("asyncio.events")
            if events is None:
                return None
            self._aio = events
        try:
            loop = events._get_running_loop()
            if loop is None:
                return None
            task = sys.modules["asyncio.tasks"].current_task(loop)
            if task is None:
                return None
            key = id(task)
            number = self._tasks.get(key)
            if number is None:
                self._ntasks += 1
                number = self._tasks[key] = self._ntasks
                self._task_refs[key] = _weakref(task, self._task_gone(key))
            return number
        except Exception:
            return None

    def _task_gone(self, key: int) -> Any:
        def forget(_ref: object) -> None:
            self._tasks.pop(key, None)
            self._task_refs.pop(key, None)

        return forget

    def _thread_state(self) -> _ThreadState:
        try:
            state: _ThreadState = self._tls.state
        except AttributeError:
            index = len(self._threads) + 1
            state = self._tls.state = self._threads[index] = _ThreadState(index)
            if (
                self._old_threads
                and not self._old_threads_noted
                and _thread.get_ident() in self._old_threads
            ):
                self._note_old_thread()
        return state

    @staticmethod
    def _top(stack: list[_Entry]) -> int | None:
        for _code, rec in reversed(stack):
            if rec is not None:
                return int(rec["id"])
        return None

    def _in_scope(self, code: CodeType) -> bool:
        """Is this code inside a path scope? Then its lines are recorded."""
        key = (code.co_filename, code.co_qualname)
        known = self._scope_known.get(key)
        if known is None:
            known = self._scope_known[key] = self._scope_match(*key)
        if known and code not in self._path_codes:
            self._path_codes.add(code)
            _mon.set_local_events(self._tool, code, _EV.LINE)
        return known

    def _scope_match(self, filename: str, qualname: str) -> bool:
        path = os.path.abspath(filename)
        for file, fn in self._scopes:
            if file is not None and not (
                path == os.path.abspath(file) or path.endswith(os.sep + file)
            ):
                continue
            if fn is not None and not (qualname == fn or qualname.endswith("." + fn)):
                continue
            return True
        return False

    def _mark(self, stack: list[_Entry], mark: str) -> None:
        """Note a call or an exception inside the path of the running call."""
        for _code, rec in reversed(stack):
            if rec is not None:
                path = rec.get("path")
                if path is not None and self._nlines < self._max_lines:
                    path.append(mark)
                return

    def _args(self, code: CodeType, frame: FrameType) -> dict[str, Any]:
        flags = code.co_flags
        count = (
            code.co_argcount
            + code.co_kwonlyargcount
            + (1 if flags & 0x04 else 0)
            + (1 if flags & 0x08 else 0)
        )
        if not count:
            return {}
        local = frame.f_locals
        out = {}
        for name in code.co_varnames[:count]:
            if name in local:
                found = out[name] = shape(local[name])
                if "sampled" in found and not self._sampling_noted:
                    self._note_sampling()
        if _LOCALS_ARE_A_COPY:
            # Python 3.12 keeps this dict on the frame, and with it every
            # argument, until the frame ends: emptied, so that an argument is
            # released when the program drops it. (A function frame's dict is
            # filled again whenever something asks for the locals.)
            local.clear()
        return out

    def _note_sampling(self) -> None:
        self._sampling_noted = True
        self._note(
            f"the types of the items after the first {SAMPLE} of a container",
            f"a list, tuple, set or dict with more than {SAMPLE} items was an "
            f"argument or a result: its length is exact, and `items`, `keys` "
            f"and `values` count the types of its first {SAMPLE} items only "
            "(the shape says `sampled`)",
        )

    def _note_old_thread(self) -> None:
        self._old_threads_noted = True
        self._note(
            "what threads that were already running did before recording started",
            f"{len(self._old_threads)} other threads were running when recording "
            "started in this process (a `with ormaos.record():` block). Of "
            "those threads, only the calls that start after that are recorded, "
            "as calls with no call above them; the calls they were already "
            "inside are not",
        )

    def _pop(self, code: CodeType) -> _Entry | None:
        stack = self._thread_state().stack
        if stack and stack[-1][0] is code:
            return stack.pop()
        if self._at_limit and stack:
            # The end of some calls was missed at the recursion limit: they
            # are still on top of this one. They are written as they are.
            for depth in range(len(stack) - 1, -1, -1):
                if stack[depth][0] is code:
                    for _code, rec in stack[depth + 1 :]:
                        if rec is not None:
                            rec["out"] = "end not seen"
                            self._emit(rec)
                    del stack[depth + 1 :]
                    return stack.pop()
        return None

    def _close(self, code: CodeType, out: str, ret: object) -> None:
        entry = self._pop(code)
        if entry is None or entry[1] is None:
            return
        rec = entry[1]
        rec["dur"] = time.perf_counter_ns() - self._t0 - rec["t"]
        rec["out"] = out
        if out == "returned":
            found = rec["ret"] = shape(ret)
            if "sampled" in found and not self._sampling_noted:
                self._note_sampling()
        # A call with no call of the user's code above it has ended (a thread
        # function, a request handler), or a task of a multiprocessing worker:
        # written right away, because such a process is often ended with a
        # signal.
        parent = rec["parent"]
        root = parent is None or parent == self._base
        if root and self._io is not None:
            self._io.release()  # the I/O event this thread was still counting
        self._emit(rec, root)

    # ---------------------------------------------------------------- calls

    def _py_start(self, code: CodeType, offset: int) -> object:
        try:
            io = self._io
            if io is not None and io.pending:
                io.install()
            if not self._user(code):
                if io is not None and code.co_name in _IO_NAMES and io.wants(code):
                    io.started(code, sys._getframe(1))
                    return None
                watched = _WATCHED.get(code.co_name)
                if watched is not None and code.co_filename.endswith(watched[0]):
                    if watched[1] == "bootstrap":
                        # A multiprocessing child starts its work here. What
                        # it runs from now on has this call above it, or none.
                        self._base = self._top(self._thread_state().stack)
                        self._flush_now()  # what led here is written
                    elif watched[1] == "sent":
                        self._child_signalled(sys._getframe(1))
                    return None  # keep every call and return of it visible
                return _DISABLE
            state = self._thread_state()
            stack = state.stack
            if self._ncalls >= self._max_calls:
                stack.append((code, None))
                if not self._calls_capped:
                    self._calls_capped = True
                    self._note(
                        "calls after this point",
                        f"limit of {self._max_calls} calls per process reached "
                        "(set ORMAOS_MAX_CALLS to change it)",
                    )
                return None
            self._ncalls += 1
            rec: dict[str, Any] = {
                "k": "call",
                "id": next(self._call_ids),
                "parent": self._top(stack),
                "fn": code.co_qualname,
                "file": code.co_filename,
                "line": code.co_firstlineno,
                "thread": state.index,
                "t": time.perf_counter_ns() - self._t0,
                "args": self._args(code, sys._getframe(1)),
            }
            task = self._task()
            if task is not None:
                rec["task"] = task
            if self._scopes:
                self._mark(stack, f"c{rec['id']}")
                if self._in_scope(code):
                    rec["path"] = []
            stack.append((code, rec))
            if rec["parent"] is None or rec["parent"] == self._base:
                # A call with no call of the user's code above it starts: a
                # checkpoint now, so that it is known even if the process is
                # ended before anything else is written.
                self._flush_now()
        except Exception as err:
            self._fail(err)
        return None

    def _py_return(self, code: CodeType, offset: int, retval: object) -> object:
        try:
            io = self._io
            if io is not None and io.pending:
                io.install()
            if not self._user(code):
                if io is not None and code in io.watched:
                    io.returned(code, sys._getframe(1), retval)
                    return None
                watched = _WATCHED.get(code.co_name)
                if watched is not None and code.co_filename.endswith(watched[0]):
                    if watched[1] == "bootstrap":
                        self.finish()  # a multiprocessing child is about to exit
                    elif watched[1] == "exit":
                        self._child_exited(sys._getframe(1))
                    return None
                return _DISABLE
            if self._watching:
                self._unwatch(id(sys._getframe(1)))
            if self._active:
                self._absorbed(id(sys._getframe(1)), code)
            self._close(code, "returned", retval)
        except Exception as err:
            self._fail(err)
        return None

    def _py_unwind(self, code: CodeType, offset: int, exc: BaseException) -> None:
        try:
            flight = self._active.get(id(exc))
            if flight is not None:
                frame = sys._getframe(1)
                frame_id = id(frame)
                flight.unwound = frame_id
                flight.back = id(frame.f_back)
                handled = flight.during
                if handled is not None and handled.frame == frame_id:
                    # It left the frame whose handler was running: the
                    # exception that handler caught was replaced by this one.
                    flight.during = None
                    if self._handled.get(handled.key) is handled:
                        del self._handled[handled.key]
                        handled.rec["swallowed"] = False
                        handled.rec["replaced_by"] = flight.rec["id"]
                        self._finish_handled(handled)
            if self._user(code):
                if self._watching:
                    self._unwatch(id(sys._getframe(1)))
                self._close(code, "raised", None)
            elif self._io is not None and code in self._io.watched:
                self._io.unwound(code, sys._getframe(1))
        except Exception as err:
            self._fail(err)

    def _py_yield(self, code: CodeType, offset: int, value: object) -> object:
        try:
            if not self._user(code):
                return _DISABLE
            if self._watching:
                self._unwatch(id(sys._getframe(1)))
            if self._active:
                self._absorbed(id(sys._getframe(1)), code)
            entry = self._pop(code)
            if entry is not None:
                self._suspended[id(sys._getframe(1))] = entry
        except Exception as err:
            self._fail(err)
        return None

    def _resume(self, code: CodeType) -> None:
        entry = self._suspended.pop(id(sys._getframe(2)), None)
        if entry is not None and entry[0] is code:
            self._thread_state().stack.append(entry)

    def _py_resume(self, code: CodeType, offset: int) -> object:
        try:
            if not self._user(code):
                return _DISABLE
            self._resume(code)
        except Exception as err:
            self._fail(err)
        return None

    def _py_throw(self, code: CodeType, offset: int, exc: BaseException) -> None:
        try:
            if self._user(code):
                self._resume(code)
        except Exception as err:
            self._fail(err)

    # ----------------------------------------------------------- exceptions
    #
    # What CPython reports (checked on 3.12, 3.13, 3.14):
    #   RAISE              in every frame the exception reaches
    #   PY_UNWIND          when it leaves a frame
    #   EXCEPTION_HANDLED  when it enters any handler: `except`, `finally`,
    #                      `with`, and the hidden cleanup blocks
    #   RERAISE            when a handler lets it go on (no matching clause,
    #                      end of `finally`, bare `raise`)
    # So an exception is "swallowed" when its last EXCEPTION_HANDLED is not
    # followed by a RERAISE of it, and no other exception raised inside that
    # handler leaves the handler's frame.

    def _raise(self, code: CodeType, offset: int, exc: BaseException) -> None:
        try:
            tb = _get_traceback(exc)
            if tb is not None:
                below = tb.tb_next
                if below is not None and below.tb_frame.f_code.co_filename.startswith(
                    _PACKAGE_DIR
                ):
                    _unhook(tb)
            key = id(exc)
            flight = self._active.get(key)
            if flight is not None:
                if not flight.unwound:
                    frame_id = id(sys._getframe(1))
                    if flight.seen != frame_id and flight.rec["type"] == type_name(
                        type(exc)
                    ):
                        return
                    # Raised again in the frame it was last raised in, with no
                    # handler and no unwinding in between, or another type:
                    # this is a new exception with the id() of one that the
                    # interpreter took without a handler (see `_absorbed`).
                    del self._active[key]
                    self._taken(flight, code)
                    if self._user(code):
                        self._begin(code, sys._getframe(1), exc, key)
                    return
                tb = _get_traceback(exc)
                below = tb.tb_next if tb is not None else None
                here = id(sys._getframe(1))
                # The same exception, one frame up: its traceback says so, or
                # (a bare `raise` in a helper and a `StopIteration` out of
                # `__next__` add no traceback entry) it is raised in the
                # caller of the frame it just left, with the same type.
                if (below is not None and id(below.tb_frame) == flight.unwound) or (
                    flight.back == here and flight.rec["type"] == type_name(type(exc))
                ):
                    flight.unwound = 0
                    flight.seen = here
                    return
                # A new exception object that got the id() of an old one. The
                # old one left Python frames and no handler was reported.
                del self._active[key]
                flight.rec["end"] = "no_handler_seen"
                self._emit(flight.rec)
            old = self._handled.get(key)
            if old is not None:
                # Checked for every exception, also outside user code: a new
                # object may get the id() of one whose handler has finished.
                del self._handled[key]
                if sys.exception() is exc:
                    # `raise e` inside the handler that caught `e`: it goes on.
                    old.rec.pop("handled", None)
                    old.unwound = 0
                    old.seen = id(sys._getframe(1))
                    self._active[key] = old
                    return
                self._finish_handled(old)
            if self._user(code):
                self._begin(code, sys._getframe(1), exc, key)
        except Exception as err:
            self._fail(err)

    def _child_exited(self, frame: FrameType) -> None:
        """This process has just learned how a child it started ended
        (`multiprocessing` or `subprocess` waited for it). Written when a
        signal ended the child: the child itself could not say so."""
        pid = _plain_attribute(frame.f_locals.get("self"), "pid")
        code = _plain_attribute(frame.f_locals.get("self"), "returncode")
        if type(pid) is not int or type(code) is not int or code >= 0:
            return
        if (pid, code) in self._exits_seen:
            return
        self._exits_seen.add((pid, code))
        self._emit(
            {
                "k": "child_exit",
                "pid": pid,
                "signal": -code,
                "t": time.perf_counter_ns() - self._t0,
            },
            True,
        )

    def _child_signalled(self, frame: FrameType) -> None:
        """This process is about to send a signal to a child it started
        (`multiprocessing` or `subprocess`: terminate, kill, the end of a
        pool). Written at once: the child may write no end record."""
        local = frame.f_locals
        pid = _plain_attribute(local.get("self"), "pid")
        signum = local.get("sig")
        if type(pid) is not int or not isinstance(signum, int):
            return
        by = "program"
        above = frame.f_back
        for _ in range(8):
            if above is None:
                break
            if above.f_code.co_filename.endswith(_MP_POOL):
                by = "pool"
                break
            above = above.f_back
        self._emit(
            {
                "k": "child_ended",
                "pid": pid,
                "signal": int(signum),
                "by": by,
                "t": time.perf_counter_ns() - self._t0,
            },
            True,
        )

    def _absorbed(self, frame_id: int, code: CodeType) -> None:
        """A frame returns or yields while an exception that was raised in it
        is still in flight there: the interpreter took it without a handler
        (the `StopIteration` that carries the result of an `await` or of a
        `yield from`). It is over; its id() can be given to a new exception."""
        for flight in list(self._active.values()):
            if flight.seen == frame_id and not flight.unwound:
                del self._active[flight.key]
                self._taken(flight, code)

    def _taken(self, flight: _Flight, code: CodeType) -> None:
        flags = code.co_flags
        if flags & 0x380:  # coroutine, iterable coroutine, async generator
            how = "await"
        elif flags & 0x20:  # generator
            how = "yield from"
        else:
            how = "other"
        flight.rec["end"] = "taken_by_interpreter"
        flight.rec["how"] = how
        self._emit(flight.rec)

    def _begin(
        self, code: CodeType, frame: FrameType, exc: BaseException, key: int
    ) -> None:
        current = sys.exception()  # what this thread is handling right now
        if self._nexc >= self._max_exc:
            if not self._exc_capped:
                self._exc_capped = True
                self._note(
                    "exceptions after this point",
                    f"limit of {self._max_exc} exceptions per process reached "
                    "(set ORMAOS_MAX_EXCEPTIONS to change it)",
                )
            return
        self._nexc += 1
        state = self._thread_state()
        rec: dict[str, Any] = {
            "k": "exc",
            "id": next(self._exc_ids),
            "type": type_name(type(exc)),
            "args": shape(_get_args(exc)),
            "thread": state.index,
            "t": time.perf_counter_ns() - self._t0,
            "origin": self._origin(exc, code, frame),
            "at": {
                "file": code.co_filename,
                "line": frame.f_lineno,
                "fn": code.co_qualname,
                "call": self._top(state.stack),
            },
        }
        task = self._task()
        if task is not None:
            rec["task"] = task
        flight = _Flight(key, rec)
        flight.seen = id(frame)
        if current is not None and current is not exc:
            handling = self._handled.get(id(current))
            if handling is not None:
                rec["during"] = handling.rec["id"]
                flight.during = handling
        self._active[key] = flight

    def _origin(
        self, exc: BaseException, code: CodeType, frame: FrameType
    ) -> dict[str, Any]:
        """Where it was raised: the deepest entry of its traceback."""
        tb = _get_traceback(exc)
        if tb is None:
            file, line, fn = code.co_filename, frame.f_lineno, code.co_qualname
            user = True
        else:
            # The deepest entry; but when the exception arrived while one of
            # our callbacks was running (a KeyboardInterrupt), the place in
            # the program where that callback was running.
            before_us = None
            last = tb
            while tb is not None:
                if before_us is None and tb.tb_next is not None:
                    below = tb.tb_next.tb_frame.f_code.co_filename
                    if below.startswith(_PACKAGE_DIR):
                        before_us = tb
                last = tb
                tb = tb.tb_next
            deepest = last.tb_frame.f_code.co_filename
            if before_us is not None and deepest.startswith(
                (_PACKAGE_DIR, _STDLIB_DIR, "<frozen")
            ):
                last = before_us
            origin = last.tb_frame.f_code
            file, line, fn = origin.co_filename, last.tb_lineno, origin.co_qualname
            user = self._user_at(file, last.tb_frame)
        return {"file": file, "line": line, "fn": fn, "user": user}

    def _exception_handled(
        self, code: CodeType, offset: int, exc: BaseException
    ) -> None:
        try:
            key = id(exc)
            flight = self._active.pop(key, None)
            if flight is None:
                return
            frame = sys._getframe(1)
            user = self._user(code)
            line = _handler_line(code, offset, frame.f_lineno)
            if user:
                if self._watching:
                    self._unwatch(id(frame))
                found = self._clauses.get((code, offset))
                if found is None:
                    found = self._clauses[code, offset] = _clauses(code, offset)
                clauses, star = found
                if clauses:
                    line = clauses[0][2]
                if len(clauses) > 1:
                    self._watching[id(frame)] = (flight, code, clauses)
                    _mon.set_local_events(self._tool, code, _EV.LINE)
                if star:
                    self._note_star(code.co_filename, line)
            flight.rec["handled"] = {
                "file": code.co_filename,
                "line": line,
                "fn": code.co_qualname,
                "user": user,
                "call": self._top(self._thread_state().stack) if user else None,
            }
            if user and self._scopes:
                self._mark(self._thread_state().stack, f"e{flight.rec['id']}")
            flight.frame = id(frame)
            flight.unwound = 0
            old = self._handled.get(key)
            if old is not None:
                self._finish_handled(old)
            self._handled[key] = flight
            if len(self._handled) > _MAX_PENDING_HANDLED:
                pending = list(self._handled.values())
                self._handled = {}
                for item in pending:
                    self._finish_handled(item)
        except Exception as err:
            self._fail(err)

    def _reraise(self, code: CodeType, offset: int, exc: BaseException) -> None:
        try:
            key = id(exc)
            if key in self._active:
                return
            flight = self._handled.pop(key, None)
            if flight is not None:
                if self._watching:
                    self._unwatch(id(sys._getframe(1)))
                flight.rec.pop("handled", None)
                flight.unwound = 0
                flight.seen = id(sys._getframe(1))
                self._active[key] = flight
            elif self._user(code):
                self._begin(code, sys._getframe(1), exc, key)
        except Exception as err:
            self._fail(err)

    def _line(self, code: CodeType, line: int) -> None:
        """Runs inside a path scope, and while we watch a frame to see which
        `except` clause matches."""
        try:
            if code in self._path_codes:
                self._step(code, line)
            if not self._watching:
                return
            frame = sys._getframe(1)
            watch = self._watching.get(id(frame))
            if watch is None:
                return  # another frame running the same code
            flight, _code, clauses = watch
            handled = flight.rec.get("handled")
            if handled is not None:
                at = frame.f_lasti
                for first, last, clause_line in clauses:
                    if first <= at <= last:
                        handled["line"] = clause_line  # this clause is being tried
                        return
            # Anywhere else is the body of the clause that matched: done.
            self._unwatch(id(frame))
        except Exception as err:
            self._fail(err)

    def _step(self, code: CodeType, line: int) -> None:
        """One line of a call inside a path scope."""
        stack = self._thread_state().stack
        if not stack or stack[-1][0] is not code:
            return
        rec = stack[-1][1]
        if rec is None:
            return
        path = rec.get("path")
        if path is None:
            return
        if self._nlines >= self._max_lines:
            rec["path_cut"] = True
            if not self._lines_capped:
                self._lines_capped = True
                self._note(
                    "path after this point",
                    f"limit of {self._max_lines} lines per process reached "
                    "(set ORMAOS_MAX_LINES to change it)",
                )
            return
        self._nlines += 1
        path.append(line)

    def _note_star(self, filename: str, line: int) -> None:
        place = (filename, line)
        if place in self._star_noted or len(self._star_noted) >= _MAX_PLACE_NOTES:
            return
        self._star_noted.add(place)
        self._note(
            f"which `except*` clause matched, at {os.path.basename(filename)}:{line}",
            "the handler line of an exception handled there is the first "
            "`except*` clause of the `try`; which clauses took a part of the "
            "group is not recorded",
        )

    def _unwatch(self, frame_id: int) -> None:
        watch = self._watching.pop(frame_id, None)
        if watch is None or self._tool < 0:
            return
        code = watch[1]
        if code in self._path_codes:
            return  # its lines stay on: it is inside a path scope
        if not any(other[1] is code for other in self._watching.values()):
            _mon.set_local_events(self._tool, code, 0)

    def _finish_handled(self, flight: _Flight) -> None:
        rec = flight.rec
        rec.setdefault("swallowed", True)
        rec["end"] = "handled"
        self._emit(rec)
