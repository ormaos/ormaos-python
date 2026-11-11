"""I/O recording: sqlite3, files, subprocesses and HTTP. Runs inside the
observed process, as a part of the recorder.

How each thing is seen (nothing is replaced or wrapped):

- Audit events (``sys.addaudithook``, PEP 578): a file is opened, a sqlite3
  connection is made, a subprocess is started, ``http.client`` sends a request.
- ``sys.monitoring`` CALL events: Python code calls ``read`` or ``write`` on a
  file object.
- ``sys.monitoring`` return events of two library functions:
  ``http.client.HTTPConnection.getresponse`` and httpx's
  ``_send_single_request``, for the status of the response. The libraries are
  never imported here: their code is recognised when the program runs it.
- sqlite3 statements: there is no event for them, so the trace callback of each
  connection is set (``Connection.set_trace_callback``). This is the one place
  where Ormaos sets something on an object of the program. A program that
  sets its own trace callback replaces ours.

Shape always, value never: a file is named by its path, a statement by its
text with every literal replaced by ``?``, a request by method, host and path
without the query string. No parameters, no contents, no headers, no bodies.
"""

import _io
import _thread
import itertools
import os
import sys
import time
from types import CodeType, FrameType
from typing import Any

from ormaos._shape import type_name

_mon = sys.monitoring
_EV = _mon.events
_DISABLE: Any = _mon.DISABLE

_METHOD = type(str.join)  # `file.read` looked up on the type
_BUILTIN = type(len)  # `file.read` bound to one file, and builtin functions
_TEXT = _io.TextIOWrapper
_BUFFERED = (_io.BufferedReader, _io.BufferedWriter, _io.BufferedRandom)
_FILE = _io.FileIO

_READS = frozenset({"read", "readline", "readlines", "readinto", "read1", "readall"})
_WRITES = frozenset({"write", "writelines"})
# Call sites of these names stay on. `connect`, so that the trace callback of
# a new sqlite3 connection is set as soon as `sqlite3.connect` returns.
_WATCHED = _READS | _WRITES | {"connect"}

_HTTP_CLIENT = os.sep + os.path.join("http", "client.py")
_HTTPX = os.sep + os.path.join("httpx", "_client.py")
_SUBPROCESS = os.sep + "subprocess.py"

_GIVE_UP_NS = 1_000_000_000
_MAX_TEXT = 200
_MAX_WAITING = 1000
_FRAMES = 300

_get_dict = object.__getattribute__
_get_qualname = type.__dict__["__qualname__"].__get__

NOT_COLLECTED = [
    {
        "what": "io: contents",
        "reason": "by design: no SQL parameters or literals, no file contents, "
        "no request or response bodies, no headers, no URL query strings",
    },
    {
        "what": "io: results",
        "reason": "rows returned or changed, bytes read or written and the exit "
        "status of a subprocess are not recorded",
    },
    {
        "what": "io: reads and writes of a file that are not a method call",
        "reason": "a read or a write is seen when Python code calls `read` or "
        "`write` on the file; iterating a file (`for line in f`, `csv.reader`) "
        "happens inside C code, and only the `open` is seen",
    },
    {
        "what": "io: sqlite3 statements, if the program sets a trace callback",
        "reason": "sqlite3 has no event for statements, so Ormaos sets the trace "
        "callback of each connection; a program that sets its own replaces it, "
        "and the statements after that are not recorded",
    },
    {
        "what": "io: other kinds",
        "reason": "only sqlite3, files (`open`, `read`, `write`), subprocesses "
        "and HTTP through http.client (urllib, requests, urllib3) or httpx are "
        "recorded; not sockets, other databases, directories or `os.read`",
    },
    {
        "what": "io with no line of user code",
        "reason": "I/O that no frame of user code is waiting for (the import "
        "system, library threads, interpreter start and exit) is not recorded",
    },
]
NOT_COLLECTED_OFF = [{"what": "io", "reason": "I/O recording was turned off (--no-io)"}]


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ[name]))
    except (KeyError, ValueError):
        return default


def _glued(out: list[str]) -> bool:
    """Is the text so far ending inside a name (so a digit is not a number)?"""
    if not out:
        return False
    last = out[-1]
    return last.isalnum() or last in '_$?:@"`]'


def _closing(sql: str, start: int, quote: str) -> int:
    """The index after the closing quote of the literal that starts at ``start``."""
    i = start + 1
    size = len(sql)
    while i < size:
        if sql[i] == quote:
            if i + 1 < size and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return size


_NAME_BEFORE = frozenset(
    {"FROM", "INTO", "UPDATE", "JOIN", "TABLE", "INDEX", "VIEW", "EXISTS", "ON", "AS"}
)


def _is_name(text: str) -> bool:
    return 0 < len(text) <= 64 and text.replace("_", "a").replace("$", "a").isalnum()


def sql_shape(sql: str) -> tuple[str, str | None, str]:
    """The verb, the table and the text of a statement, with every literal
    replaced by ``?`` and the comments removed. Quoted names are kept only
    where a name is expected (after FROM, INTO, ... or next to a dot)."""
    out: list[str] = []
    i = 0
    size = len(sql)
    while i < size:
        ch = sql[i]
        if ch == "'":
            i = _closing(sql, i, "'")
            if out and out[-1] in "xX" and not _glued(out[:-1]):
                out.pop()  # a blob literal: x'..'
            out.append("?")
        elif ch in '"`[':
            end = sql.find("]", i) + 1 or size if ch == "[" else _closing(sql, i, ch)
            inner = sql[i + 1 : end - 1]
            before = "".join(out).rstrip()
            word = before.rsplit(None, 1)[-1].upper() if before else ""
            dotted = before.endswith(".") or sql[end : end + 1] == "."
            if _is_name(inner) and (dotted or word in _NAME_BEFORE):
                out.extend(sql[i:end])
            else:
                out.append("?")
            i = end
        elif ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            i = size if end < 0 else end
        elif ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = size if end < 0 else end + 2
        elif (ch.isdigit() or (ch == "." and sql[i + 1 : i + 2].isdigit())) and (
            not _glued(out)
        ):
            i += 1
            while i < size and (sql[i].isalnum() or sql[i] in "._"):
                if sql[i] in "eE" and sql[i + 1 : i + 2] in ("+", "-"):
                    i += 1
                i += 1
            out.append("?")
        elif ch.isspace():
            if out and out[-1] != " ":
                out.append(" ")
            i += 1
        else:
            out.append(ch)
            i += 1
    text = "".join(out).strip()
    words = text.replace("(", " ").replace(",", " ").replace(";", " ").split()
    verb = words[0].upper() if words and words[0].isalpha() else "?"
    table = None
    marks = {
        "INSERT": ("INTO",),
        "REPLACE": ("INTO",),
        "UPDATE": ("UPDATE",),
        "SELECT": ("FROM",),
        "DELETE": ("FROM",),
        "WITH": ("FROM", "INTO", "UPDATE"),
        "CREATE": ("TABLE", "INDEX", "VIEW", "TRIGGER"),
        "DROP": ("TABLE", "INDEX", "VIEW", "TRIGGER"),
        "ALTER": ("TABLE",),
    }.get(verb, ())
    skip = {
        "OR",
        "IF",
        "NOT",
        "EXISTS",
        "ROLLBACK",
        "ABORT",
        "REPLACE",
        "FAIL",
        "IGNORE",
    }
    for position, word in enumerate(words):
        if word.upper() in marks:
            for name in words[position + 1 : position + 6]:
                if name.upper() not in skip:
                    name = name.strip('"`[]')
                    table = name if name and name != "?" else None
                    break
            break
    if len(text) > _MAX_TEXT:
        text = text[: _MAX_TEXT - 3] + "..."
    return verb, table, text


def _access(mode: object, flags: object) -> str:
    if type(mode) is str:
        if "+" in mode:
            return "read+write"
        if "a" in mode:
            return "append"
        return "write" if "w" in mode or "x" in mode else "read"
    if type(flags) is int:
        if flags & os.O_APPEND:
            return "append"
        return ("read", "write", "read+write", "read+write")[flags & 3]
    return "?"


def _name(value: object) -> str | None:
    """A path as text; None when it is not a path (a file descriptor)."""
    tp = type(value)
    if tp is str:
        text: str = value  # type: ignore[assignment]
    elif tp is bytes:
        text = value.decode("utf-8", "replace")  # type: ignore[attr-defined]
    elif type_name(tp).startswith("pathlib."):
        try:
            text = str(value)
        except Exception:
            return None
    else:
        return None
    return text[:300]


def _file_name(obj: object) -> str | None:
    """The path of a real file object; None for anything else."""
    try:
        tp = type(obj)
        if tp is _TEXT:
            obj = obj.buffer  # type: ignore[attr-defined]
            tp = type(obj)
        if tp in _BUFFERED:
            obj = obj.raw  # type: ignore[attr-defined]
            tp = type(obj)
        if tp is not _FILE:
            return None
        return _name(obj.name)  # type: ignore[attr-defined]
    except Exception:
        return None  # a closed or detached file


def _program(value: object) -> str:
    text = _name(value)
    return os.path.basename(text) if text else "?"


class IoRecorder:
    def __init__(self, recorder: Any) -> None:
        self._rec = recorder
        self._on = False
        self._hooked = False
        self._ids = itertools.count(1)
        self._count = 0
        self._max = _int_env("ORMAOS_MAX_IO", 100_000)
        self._capped = False
        # Per thread: the last event, kept so that a repeat only adds to `n`.
        self._held: dict[int, tuple[Any, dict[str, Any]]] = {}
        # Per thread: sqlite3 connections whose trace callback is not set yet.
        self.pending: dict[int, list[tuple[Any, int, str, int]]] = {}
        self._db_target: dict[int, str] = {}
        self._dbs = 0
        # Requests waiting for their response: id(connection or frame) -> record.
        self._waiting: dict[int, dict[str, Any]] = {}
        # Library code whose start and return we look at: code -> which one.
        self.watched: dict[CodeType, str] = {}
        self._handlers = {
            "open": self._open,
            "sqlite3.connect": self._sqlite_connect,
            "sqlite3.connect/handle": self._sqlite_handle,
            "subprocess.Popen": self._popen,
            "os.system": self._system,
            "os.exec": self._exec,
            "os.posix_spawn": self._exec,
            "os.fork": self._fork,
            "http.client.send": self._http_send,
        }

    # ------------------------------------------------------------ lifecycle

    def start(self, tool: int) -> int:
        """Register the callbacks. Returns the events to turn on everywhere."""
        _mon.register_callback(tool, _EV.CALL, self._call)
        _mon.register_callback(tool, _EV.C_RETURN, self._c_return)
        if not self._hooked:
            # An audit hook cannot be removed: when recording stops, it stays
            # and does nothing.
            sys.addaudithook(self._audit)
            self._hooked = True
        self._on = True
        return int(_EV.CALL)

    def stop(self, tool: int) -> None:
        self._on = False
        _mon.register_callback(tool, _EV.CALL, None)
        _mon.register_callback(tool, _EV.C_RETURN, None)

    def reset(self) -> None:
        """In a forked child: forget what the parent was in the middle of."""
        self._held = {}
        self.pending = {}
        self._db_target = {}
        self._waiting = {}
        self._count = 0
        self._capped = False

    def finish(self) -> None:
        """Write the events that were still open."""
        self._on = False
        self.pending = {}
        for _sig, rec in list(self._held.values()):
            self._rec._emit(rec)
        self._held = {}
        for rec in list(self._waiting.values()):
            rec["out"] = "no response seen"
            self._rec._emit(rec)
        self._waiting = {}

    # --------------------------------------------------------------- events

    def _site(self, frame: FrameType | None) -> FrameType | None:
        """The nearest frame of user code that is waiting for this I/O."""
        user_at = self._rec._user_at
        for _ in range(_FRAMES):
            if frame is None:
                return None
            name = frame.f_code.co_filename
            if name.startswith("<frozen importlib"):
                return None  # the import system reading a module
            if user_at(name, frame):
                return frame
            frame = frame.f_back
        return None

    def _new(
        self,
        kind: str,
        op: str,
        frame: FrameType | None,
        fields: dict[str, Any],
        repeat: bool = True,
    ) -> dict[str, Any] | None:
        """One I/O event. With ``repeat``, the same event at the same place
        right after itself only adds to the count ``n`` of the first one."""
        site = self._site(frame)
        if site is None:
            return None
        recorder = self._rec
        state = recorder._thread_state()
        code = site.f_code
        call = recorder._top(state.stack)
        ident = _thread.get_ident()
        held = self._held.get(ident)
        sig = (kind, op, code.co_filename, site.f_lineno, call, *fields.values())
        if held is not None:
            if repeat and held[0] == sig:
                held[1]["n"] += 1
                return held[1]
            del self._held[ident]
            recorder._emit(held[1])
        if self._count >= self._max:
            if not self._capped:
                self._capped = True
                recorder._note(
                    "io after this point",
                    f"limit of {self._max} io events per process reached "
                    "(set ORMAOS_MAX_IO to change it)",
                )
            return None
        self._count += 1
        rec: dict[str, Any] = {
            "k": "io",
            "id": next(self._ids),
            "kind": kind,
            "op": op,
            **fields,
            "file": code.co_filename,
            "line": site.f_lineno,
            "fn": code.co_qualname,
            "call": call,
            "thread": state.index,
            "t": time.perf_counter_ns() - recorder._t0,
        }
        if repeat:
            rec["n"] = 1
            self._held[ident] = (sig, rec)
        return rec

    def _audit(self, event: str, args: Any) -> None:
        if not self._on:
            return
        handler = self._handlers.get(event)
        if handler is None:
            return
        try:
            if self._rec._owner == _thread.get_ident():
                return  # this thread is in the middle of writing the recording
            try:
                frame = sys._getframe(1)
            except ValueError:
                return  # no Python code is running: the interpreter itself
            handler(args, frame)
        except Exception as err:
            self._rec._fail(err)

    def count(self) -> int:
        return self._count

    # ---------------------------------------------------------------- files

    def _open(self, args: Any, frame: FrameType) -> None:
        path = _name(args[0])
        if path is None or path.startswith(self._rec._dir):
            return
        fields = {"target": path, "mode": _access(args[1], args[2])}
        self._new("file", "open", frame, fields)

    def _call(self, code: CodeType, offset: int, func: object, arg0: object) -> object:
        """Python code calls something. Only calls of `read` and `write` on a
        file stay on; every other call site is turned off at its first call."""
        try:
            if self.pending:
                self.install()
            tp = type(func)
            if tp is _METHOD:
                target = arg0
            elif tp is _BUILTIN:
                target = func.__self__  # type: ignore[attr-defined]
            else:
                return _DISABLE
            name = func.__name__  # type: ignore[attr-defined]
            if name not in _WATCHED:
                return _DISABLE
            if name == "connect":
                return None
            if code.co_filename.startswith("<frozen"):
                return _DISABLE
            path = _file_name(target)
            if path is None or path.startswith(self._rec._dir):
                return None
            if self._rec._owner != _thread.get_ident():
                op = "read" if name in _READS else "write"
                self._new("file", op, sys._getframe(1), {"target": path})
        except Exception as err:
            self._rec._fail(err)
        return None

    def _c_return(
        self, code: CodeType, offset: int, func: object, arg0: object
    ) -> None:
        try:
            if self.pending:
                self.install()
        except Exception as err:
            self._rec._fail(err)

    # --------------------------------------------------------------- sqlite

    def _sqlite_connect(self, args: Any, frame: FrameType) -> None:
        self._db_target[_thread.get_ident()] = _name(args[0]) or "?"

    def _sqlite_handle(self, args: Any, frame: FrameType) -> None:
        ident = _thread.get_ident()
        target = self._db_target.pop(ident, "?")
        self._dbs += 1
        # The connection is not ready inside the audit event: its trace
        # callback is set at our next callback in this thread.
        entry = (args[0], self._dbs, target, time.perf_counter_ns())
        self.pending.setdefault(ident, []).append(entry)
        self._new("sqlite", "connect", frame, {"target": target, "db": self._dbs})

    def install(self) -> None:
        """Set the trace callback of the connections this thread just made."""
        todo = self.pending.pop(_thread.get_ident(), None)
        for conn, db, target, _made in todo or ():
            try:
                conn.set_trace_callback(self._tracer(db, target))
            except Exception as err:
                self._rec._note(
                    f"io: the statements of sqlite3 connection {db}",
                    f"its trace callback could not be set ({type(err).__name__})",
                )
        if self.pending:
            # A connection is only held until its own thread comes back here.
            # A thread that does not come back within a second gives it up, so
            # that Ormaos never keeps a connection of the program alive.
            now = time.perf_counter_ns()
            for ident, waiting in list(self.pending.items()):
                if waiting and now - waiting[0][3] > _GIVE_UP_NS:
                    for _conn, db, _target, _made in self.pending.pop(ident, ()):
                        self._rec._note(
                            f"io: the statements of sqlite3 connection {db}",
                            "the thread that made the connection ran no Python "
                            "code of its own for a second after making it",
                        )

    def _tracer(self, db: int, target: str) -> Any:
        def trace(statement: object) -> None:
            if not self._on:
                return
            try:
                if type(statement) is str and self._rec._owner != _thread.get_ident():
                    verb, table, text = sql_shape(statement)
                    fields = {"target": target, "db": db, "table": table, "sql": text}
                    self._new("sqlite", verb, sys._getframe(1), fields)
            except Exception as err:
                self._rec._fail(err)

        return trace

    # ---------------------------------------------------------- subprocesses

    def _popen(self, args: Any, frame: FrameType) -> None:
        executable, argv = args[0], args[1]
        listed = type(argv) in (list, tuple)
        program = _program(
            executable if executable is not None or not listed else argv[0]
        )
        fields = {"target": program, "argc": len(argv) if listed else 1}
        self._new("subprocess", "spawn", frame, fields)

    def _system(self, args: Any, frame: FrameType) -> None:
        self._new("subprocess", "system", frame, {"target": "shell", "argc": 1})

    def _exec(self, args: Any, frame: FrameType) -> None:
        if frame.f_code.co_filename.endswith(_SUBPROCESS):
            return  # already recorded as `subprocess.Popen`
        argv = args[1]
        fields = {
            "target": _program(args[0]),
            "argc": len(argv) if type(argv) in (list, tuple) else 1,
        }
        self._new("subprocess", "exec", frame, fields)

    def _fork(self, args: Any, frame: FrameType) -> None:
        if frame.f_code.co_filename.endswith(_SUBPROCESS):
            return
        self._new("subprocess", "fork", frame, {"target": "fork", "argc": 0})

    # ----------------------------------------------------------------- http

    def _wait_for_response(self, key: int, rec: dict[str, Any] | None) -> None:
        if rec is None:
            return
        old = self._waiting.pop(key, None)
        if old is not None:
            old["out"] = "no response seen"
            self._rec._emit(old)
        if len(self._waiting) >= _MAX_WAITING:
            for other in list(self._waiting.values()):
                other["out"] = "no response seen"
                self._rec._emit(other)
            self._waiting = {}
        self._waiting[key] = rec

    def _http_send(self, args: Any, frame: FrameType) -> None:
        """`http.client` sends bytes. A request starts with `GET /path HTTP/1.1`."""
        conn, data = args[0], args[1]
        if type(data) not in (bytes, bytearray):
            return
        head = bytes(data[:2048])
        end = head.find(b"\r\n")
        parts = head[:end].split(b" ") if end > 0 else []
        if len(parts) != 3 or not parts[2].startswith(b"HTTP/"):
            return
        raw = parts[1]
        query = raw.find(b"?")
        info = _get_dict(conn, "__dict__")
        host = None
        if type(info) is dict:
            # urllib3 keeps the name in `_dns_host`; `host` is a property there.
            host = info.get("host") or info.get("_dns_host")
        port = info.get("port") if type(info) is dict else None
        secure = any(_get_qualname(c) == "HTTPSConnection" for c in type(conn).__mro__)
        fields = {
            "scheme": "https" if secure else "http",
            "host": host if type(host) is str else None,
            "port": port if type(port) is int else None,
            "target": (raw if query < 0 else raw[:query]).decode("utf-8", "replace")[
                :_MAX_TEXT
            ],
            "query": query >= 0,
        }
        method = parts[0].decode("ascii", "replace")[:16]
        rec = self._new("http", method, frame, fields, repeat=False)
        self._wait_for_response(id(conn), rec)

    def wants(self, code: CodeType) -> bool:
        """Is this library code one of the two whose return we look at?"""
        name = code.co_name
        if name == "getresponse" and code.co_filename.endswith(_HTTP_CLIENT):
            self.watched[code] = "http.client"
        elif name == "_send_single_request" and code.co_filename.endswith(_HTTPX):
            self.watched[code] = "httpx"
        else:
            return False
        return True

    def started(self, code: CodeType, frame: FrameType) -> None:
        if self.watched.get(code) != "httpx" or not self._on:
            return
        try:
            request: Any = frame.f_locals.get("request")
            url = request.url
            fields = {
                "scheme": str(url.scheme),
                "host": str(url.host),
                "port": url.port if type(url.port) is int else None,
                "target": str(url.path)[:_MAX_TEXT],
                "query": bool(url.query),
            }
            method = str(request.method)[:16]
        except Exception:
            return  # not the request object we know
        rec = self._new("http", method, frame, fields, repeat=False)
        self._wait_for_response(id(frame), rec)

    def returned(self, code: CodeType, frame: FrameType, retval: object) -> None:
        kind = self.watched.get(code)
        if kind == "httpx":
            rec = self._waiting.pop(id(frame), None)
            status = getattr(retval, "status_code", None) if rec is not None else None
        else:
            rec = self._waiting.pop(id(frame.f_locals.get("self")), None)
            info = _get_dict(retval, "__dict__") if rec is not None else None
            status = info.get("status") if type(info) is dict else None
        if rec is not None:
            rec["status"] = status if type(status) is int else None
            rec["dur"] = time.perf_counter_ns() - self._rec._t0 - rec["t"]
            self._rec._emit(rec)

    def unwound(self, code: CodeType, frame: FrameType) -> None:
        if self.watched.get(code) == "httpx":
            rec = self._waiting.pop(id(frame), None)
        else:
            rec = self._waiting.pop(id(frame.f_locals.get("self")), None)
        if rec is not None:
            rec["out"] = "raised"
            rec["dur"] = time.perf_counter_ns() - self._rec._t0 - rec["t"]
            self._rec._emit(rec)
