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
from types import CodeType, FrameType, ModuleType
from typing import Any

from ormaos._shape import shape, type_name

FORMAT = "provisional-0"

_mon = sys.monitoring
_EV = _mon.events
_DISABLE: Any = _mon.DISABLE

# Read exception attributes through the base class, so a subclass that
# overrides them cannot run its own code inside our callbacks.
_get_traceback = BaseException.__dict__["__traceback__"].__get__
_get_args = BaseException.__dict__["args"].__get__

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep
_STDLIB_DIR = os.path.dirname(os.path.abspath(os.__file__)) + os.sep
_SITE_MARKS = (f"site-packages{os.sep}", f"dist-packages{os.sep}")
_USER_PSEUDO_FILES = ("<string>", "<stdin>")

_FLUSH_EVERY = 256
_MAX_PENDING_HANDLED = 1000

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
        "what": "exceptions that never pass through user code",
        "reason": "only exceptions seen in a user-code frame are recorded",
    },
    {
        "what": "which `except` clause matched",
        "reason": "the handler line is the first clause of the `try` statement",
    },
    {"what": "path", "reason": "line-by-line recording is not implemented yet"},
    {"what": "io", "reason": "I/O recording is not implemented yet"},
]

type _Call = dict[str, Any] | None
type _Entry = tuple[CodeType, _Call]


class _Flight:
    """One exception, from the first time we see it until it is handled."""

    __slots__ = ("during", "frame", "key", "rec", "unwound")

    def __init__(self, key: int, rec: dict[str, Any]) -> None:
        self.key = key  # id() of the exception object
        self.rec = rec
        self.unwound = 0  # id() of the last frame it left, 0 if none yet
        self.frame = 0  # id() of the frame whose handler caught it
        self.during: _Flight | None = None  # the exception being handled


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


def _handler_line(code: CodeType, offset: int, fallback: int) -> int:
    """The source line of the handler that starts at ``offset``."""
    for _start, end, line in code.co_lines():
        if end > offset and line is not None:
            return line
    return fallback


class Recorder:
    def __init__(self, directory: str) -> None:
        self._dir = directory
        self._lock = _thread.allocate_lock()
        self._buf: list[dict[str, Any]] = []
        self._fd = -1
        self._tool = -1
        self._dead = False
        self._finished = False
        self._pid = os.getpid()
        self._t0 = time.perf_counter_ns()
        self._user_files: dict[str, bool] = {}
        self._threads: dict[int, _ThreadState] = {}
        self._suspended: dict[int, _Entry] = {}
        self._active: dict[int, _Flight] = {}
        self._handled: dict[int, _Flight] = {}
        self._call_ids = itertools.count(1)
        self._exc_ids = itertools.count(1)
        self._ncalls = 0
        self._nexc = 0
        self._max_calls = _int_env("ORMAOS_MAX_CALLS", 200_000)
        self._max_exc = _int_env("ORMAOS_MAX_EXCEPTIONS", 50_000)
        self._calls_capped = False
        self._exc_capped = False
        only = os.environ.get("ORMAOS_ONLY", "")
        self._only = tuple(
            os.path.abspath(part).rstrip(os.sep)
            for part in only.split(os.pathsep)
            if part
        )

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        try:
            self._open(forked=False)
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
            self._install_fault()
            _mon.set_events(tool, mask)
        except Exception as err:
            self._fail(err)

    def _install_fault(self) -> None:
        """Test hook: make one internal step fail, to prove that we fail open."""
        fault = os.environ.get("ORMAOS_FAULT", "")
        if fault == "calls":
            setattr(self, "_args", _injected_fault)  # noqa: B010
        elif fault == "exceptions":
            setattr(self, "_origin", _injected_fault)  # noqa: B010
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
        for event, _callback in self._callbacks():
            _mon.register_callback(tool, event, None)
        _mon.free_tool_id(tool)

    def _open(self, forked: bool) -> None:
        self._pid = os.getpid()
        path = os.path.join(self._dir, f"p{self._pid}-{time.time_ns()}.jsonl")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
        self._fd = os.open(path, flags, 0o600)
        self._write(
            [
                {
                    "format": FORMAT,
                    "kind": "process",
                    "pid": self._pid,
                    "ppid": os.getppid(),
                    "forked": forked,
                    "python": ".".join(str(n) for n in sys.version_info[:3]),
                    "implementation": sys.implementation.name,
                    "executable": sys.executable,
                    "argv": _argv_shape(),
                    "cwd": os.getcwd(),
                    "started": time.time(),
                    "capabilities": ["context", "calls", "exceptions"],
                    "not_collected": _ALWAYS_NOT_COLLECTED,
                }
            ]
        )

    def _after_fork(self) -> None:
        """In a forked child: start a file of its own, forget the parent's state."""
        if self._dead or self._finished:
            return
        try:
            self._lock = _thread.allocate_lock()
            self._buf = []
            self._threads = {}
            self._suspended = {}
            self._active = {}
            self._handled = {}
            self._ncalls = 0
            self._nexc = 0
            self._t0 = time.perf_counter_ns()
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            self._open(forked=True)
        except Exception as err:
            self._fail(err)

    def finish(self) -> None:
        """Write what is still open, and the end record. Runs at exit."""
        if self._finished or os.getpid() != self._pid:
            return
        self._finished = True
        try:
            self._stop()
        except Exception:
            pass
        try:
            if not self._dead:
                self._finish_records()
            self._flush()
        except Exception as err:
            self._fail(err)
        try:
            # Written directly, so the end record exists even after a failure.
            line = json.dumps(self._end_record(), separators=(",", ":")) + "\n"
            Recorder._write_out(self, line.encode("utf-8"))
        except Exception:
            pass
        try:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
        except Exception:
            pass

    def _finish_records(self) -> None:
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

    def _emit(self, rec: dict[str, Any]) -> None:
        with self._lock:
            self._buf.append(rec)
            if len(self._buf) >= _FLUSH_EVERY:
                buf = self._buf
                self._buf = []
                self._write(buf)

    def _flush(self) -> None:
        with self._lock:
            if self._buf:
                buf = self._buf
                self._buf = []
                self._write(buf)

    def _note(self, what: str, reason: str) -> None:
        self._emit({"k": "not_collected", "what": what, "reason": reason})
        self._flush()

    def _fail(self, err: BaseException) -> None:
        """Internal error: stop recording, keep the program running."""
        if self._dead:
            return
        self._dead = True
        try:
            self._stop()
        except Exception:
            pass
        try:
            where = "?"
            tb = err.__traceback__
            if tb is not None:
                while tb.tb_next is not None:
                    tb = tb.tb_next
                name = os.path.basename(tb.tb_frame.f_code.co_filename)
                where = f"{name}:{tb.tb_lineno}"
            note = {
                "k": "not_collected",
                "what": "everything after this point",
                "reason": (
                    f"internal error in ormaos ({type(err).__name__} at {where}); "
                    "recording stopped, the program was not affected"
                ),
            }
            # Bypass the test hook on purpose: say why, if the file still works.
            buf, self._buf = self._buf, []
            lines = [json.dumps(rec, separators=(",", ":")) for rec in [*buf, note]]
            Recorder._write_out(self, ("\n".join(lines) + "\n").encode("utf-8"))
        except Exception:
            pass

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
        if filename not in _USER_PSEUDO_FILES:
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
        return not any(mark in path for mark in _SITE_MARKS)

    def _thread_state(self) -> _ThreadState:
        ident = _thread.get_ident()
        state = self._threads.get(ident)
        if state is None:
            state = self._threads[ident] = _ThreadState(len(self._threads) + 1)
        return state

    @staticmethod
    def _top(stack: list[_Entry]) -> int | None:
        for _code, rec in reversed(stack):
            if rec is not None:
                return int(rec["id"])
        return None

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
                out[name] = shape(local[name])
        return out

    def _pop(self, code: CodeType) -> _Entry | None:
        stack = self._thread_state().stack
        if stack and stack[-1][0] is code:
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
            rec["ret"] = shape(ret)
        self._emit(rec)

    # ---------------------------------------------------------------- calls

    def _py_start(self, code: CodeType, offset: int) -> object:
        try:
            if not self._user(code):
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
            rec = {
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
            stack.append((code, rec))
        except Exception as err:
            self._fail(err)
        return None

    def _py_return(self, code: CodeType, offset: int, retval: object) -> object:
        try:
            if not self._user(code):
                return _DISABLE
            self._close(code, "returned", retval)
        except Exception as err:
            self._fail(err)
        return None

    def _py_unwind(self, code: CodeType, offset: int, exc: BaseException) -> None:
        try:
            flight = self._active.get(id(exc))
            if flight is not None:
                frame_id = id(sys._getframe(1))
                flight.unwound = frame_id
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
                self._close(code, "raised", None)
        except Exception as err:
            self._fail(err)

    def _py_yield(self, code: CodeType, offset: int, value: object) -> object:
        try:
            if not self._user(code):
                return _DISABLE
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
            key = id(exc)
            flight = self._active.get(key)
            if flight is not None:
                if not flight.unwound:
                    return
                tb = _get_traceback(exc)
                below = tb.tb_next if tb is not None else None
                if below is not None and id(below.tb_frame) == flight.unwound:
                    flight.unwound = 0  # the same exception, one frame up
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
                    self._active[key] = old
                    return
                self._finish_handled(old)
            if self._user(code):
                self._begin(code, sys._getframe(1), exc, key)
        except Exception as err:
            self._fail(err)

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
        flight = _Flight(key, rec)
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
            while tb.tb_next is not None:
                tb = tb.tb_next
            origin = tb.tb_frame.f_code
            file, line, fn = origin.co_filename, tb.tb_lineno, origin.co_qualname
            user = self._user_at(file, tb.tb_frame)
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
            flight.rec["handled"] = {
                "file": code.co_filename,
                "line": _handler_line(code, offset, frame.f_lineno),
                "fn": code.co_qualname,
                "user": user,
                "call": self._top(self._thread_state().stack) if user else None,
            }
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
                flight.rec.pop("handled", None)
                flight.unwound = 0
                self._active[key] = flight
            elif self._user(code):
                self._begin(code, sys._getframe(1), exc, key)
        except Exception as err:
            self._fail(err)

    def _finish_handled(self, flight: _Flight) -> None:
        rec = flight.rec
        rec.setdefault("swallowed", True)
        rec["end"] = "handled"
        self._emit(rec)
