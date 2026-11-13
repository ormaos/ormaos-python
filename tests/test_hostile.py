"""Programs that are hard on a recorder. Each must run unchanged, and the
recording must say what it has and what it has not."""

import json
import sys

import pytest

from conftest import Workdir


def same(work: Workdir, *args: str) -> tuple[str, list[str]]:
    """Run plain and recorded; the output must be the same. Returns it, and
    the summary."""
    plain = work.plain(sys.executable, *args)
    result = work.python(*args)
    assert result.returncode == plain.returncode
    assert result.stdout == plain.stdout
    assert result.program_stderr == plain.stderr
    return result.stdout, result.summary


def test_a_signal_handler_that_runs_during_a_write_does_not_hang(
    work: Workdir,
) -> None:
    # The handler runs between two instructions of the main thread, also while
    # the recorder is inside its own lock (the sqlite trace callback is called
    # from C, where a handler can run in the middle of it).
    source = """\
import signal, sqlite3, time

hits = 0

def helper():
    return 1

def on_alarm(signum, frame):
    global hits
    hits += helper()

signal.signal(signal.SIGALRM, on_alarm)
signal.setitimer(signal.ITIMER_REAL, 0.0005, 0.0005)
db = sqlite3.connect(":memory:")
db.execute("create table t (a)")
end = time.monotonic() + 1.5
while time.monotonic() < end:
    db.execute("select a from t")
    db.execute("select count(*) from t")
signal.setitimer(signal.ITIMER_REAL, 0)
print("finished", hits > 100)
"""
    work.write("app.py", source)
    result = work.python("app.py")  # the fixture stops it after 60 seconds

    assert result.returncode == 0 and result.stdout == "finished True\n"
    recording = work.last()
    assert recording.complete
    assert not any("internal error" in n["reason"] for n in recording.not_collected)
    assert sum(1 for call in recording.calls if call["fn"] == "on_alarm") > 100


def test_an_argument_is_released_when_the_program_drops_it(work: Workdir) -> None:
    # Looking at the arguments must not keep them alive (Python 3.12 keeps a
    # copy of the locals on the frame once they have been looked at).
    source = """\
class Res:
    def __del__(self):
        print("released")

def use(res):
    res = None
    print("after drop")

def gen(res):
    del res
    print("gen: after del")
    yield 1

use(Res())
for _ in gen(Res()):
    pass
print("end")
"""
    work.write("app.py", source)
    out, _ = same(work, "app.py")
    assert out.splitlines() == [
        "released",
        "after drop",
        "released",
        "gen: after del",
        "end",
    ]
    block = """\
import ormaos
{body}
with ormaos.record():
    use(Res())
    for _ in gen(Res()):
        pass
print("end")
"""
    body = source.split("use(Res())")[0]
    work.write("block.py", block.format(body=body))
    result = work.plain(sys.executable, "block.py")
    assert result.stdout.splitlines() == out.splitlines()


def test_the_recursion_limit_is_reached_and_the_recording_says_so(
    work: Workdir,
) -> None:
    source = """\
import sys

sys.setrecursionlimit(400)

def down(n):
    return down(n + 1)

def before():
    return 1

before()
try:
    down(0)
except RecursionError:
    print("caught")

def after():
    return 2

after()
print("done")
"""
    work.write("app.py", source)
    out, summary = same(work, "app.py")
    assert out == "caught\ndone\n"
    doc = json.loads(work.cli("show", "--json").stdout)
    # The recording is not emptied: the calls down to the limit are there,
    # and so is what ran after the program caught the error.
    recording = work.last()
    assert recording.complete
    fns = [call["fn"] for call in recording.calls]
    assert fns.count("down") >= 300 and "before" in fns and "after" in fns
    assert not any("internal error" in n["reason"] for n in recording.not_collected)
    errors = [e for e in recording.exceptions if e["type"] == "RecursionError"]
    assert len(errors) == 1 and errors[0]["swallowed"] is True
    # What could not be noted at the limit is said, with a count.
    notes = [n for n in doc["not_collected"] if not n.get("always")]
    at_limit = [n for n in notes if n["what"] == "events at the recursion limit"]
    assert len(at_limit) <= 1
    for note in at_limit:
        assert note["reason"].split()[0].isdigit()
        assert any("events at the recursion limit" in line for line in summary)


def test_a_metaclass_cannot_make_the_recorder_run_its_code(work: Workdir) -> None:
    source = """\
class Meta(type):
    def __eq__(cls, other):
        print("Meta.__eq__ ran")
        return True
    def __hash__(cls):
        print("Meta.__hash__ ran")
        return 1
    @property
    def __name__(cls):
        print("Meta.__name__ ran")
        return "lie"

class Thing(metaclass=Meta):
    def __len__(self):
        print("Thing.__len__ ran")
        return 3
    def __iter__(self):
        print("Thing.__iter__ ran")
        return iter(())

def take(thing, things):
    return thing

take(Thing(), [Thing(), Thing()])
print("done")
"""
    work.write("app.py", source)
    out, _ = same(work, "app.py")
    assert out == "done\n"
    (call,) = [c for c in work.last().calls if c["fn"] == "take"]
    assert call["args"]["thing"] == {"type": "__main__.Thing"}
    assert call["args"]["things"]["items"] == {"__main__.Thing": 2}


def test_overrides_of_the_program_are_not_called_by_the_recorder(
    work: Workdir,
) -> None:
    source = """\
import sqlite3, subprocess, sys

class Conn(sqlite3.Connection):
    def set_trace_callback(self, callback):
        print("the program's set_trace_callback ran")
        super().set_trace_callback(callback)

class Child(subprocess.Popen):
    reads = 0
    @property
    def pid(self):
        Child.reads += 1
        return self._pid
    @pid.setter
    def pid(self, value):
        self._pid = value

def main():
    db = sqlite3.connect(":memory:", factory=Conn)
    db.execute("select 1")
    child = Child([sys.executable, "-c", "import time; time.sleep(30)"])
    child.kill()
    child.wait()
    print("pid reads", Child.reads, "code", child.returncode)

main()
"""
    work.write("app.py", source)
    out, _ = same(work, "app.py")
    assert out.endswith("code -9\n") and "set_trace_callback ran" not in out
    # The statement is still recorded.
    io = json.loads(work.cli("io", "last", "--kind", "sqlite", "--json").stdout)
    assert any(group["op"] == "SELECT" for group in io["groups"])


def test_credentials_and_parameters_in_a_request_target_are_not_recorded(
    work: Workdir,
) -> None:
    planted = "PLANTED" + "-7f3a"
    source = """\
import http.client, http.server, sys, threading

class Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()
    def log_message(self, *args):
        pass

def main(secret):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for target in (
        f"http://user:{secret}@example.invalid/a/b?x={secret}",
        f"/login;token={secret}",
        f"/page#{secret}",
        f"/plain/path?token={secret}",
    ):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
        conn.request("GET", target)
        conn.getresponse().read()
        conn.close()
    server.shutdown()
    print("done")

main(sys.argv[1])
"""
    work.write("app.py", source)
    result = work.python("app.py", planted)
    assert result.returncode == 0 and result.stdout == "done\n"
    recording = work.last()
    targets = [e["target"] for e in recording.io if e["kind"] == "http"]
    assert targets == ["http://example.invalid/a/b", "/login", "/page", "/plain/path"]
    for file in (work.path / ".ormaos" / "r1").iterdir():
        if file.name.startswith("p"):
            assert planted not in file.read_text(encoding="utf-8"), file.name
    assert planted not in work.cli("io", "last", "--kind", "http").stdout


def test_a_block_that_cannot_finish_its_recording_prints_nothing(
    work: Workdir,
) -> None:
    source = """\
import os
import ormaos

def work():
    return 1

with ormaos.record() as rec:
    work()
    os.chmod(os.path.join(rec.directory, "run.jsonl"), 0o444)
print("done", rec.reason)
"""
    work.write("app.py", source)
    result = work.plain(sys.executable, "app.py")
    if os_is_root():
        pytest.skip("root can write to a read-only file")
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == "done could not finish the recording (PermissionError)\n"


def os_is_root() -> bool:
    import os

    return os.geteuid() == 0


def test_an_exception_raised_by_a_signal_handler_reaches_the_program(
    work: Workdir,
) -> None:
    # The handler can run while a callback of the recorder is on the stack.
    # Its exception is the program's: it must arrive, not be taken as an
    # internal error of the recorder.
    source = """\
import signal

class Timeout(Exception):
    pass

def on_alarm(signum, frame):
    raise Timeout()

signal.signal(signal.SIGALRM, on_alarm)

def step(i):
    return i

def work():
    i = 0
    while True:
        i = step(i) + 1

signal.setitimer(signal.ITIMER_REAL, 0.2)
try:
    work()
except Timeout:
    print("timed out as designed")
"""
    work.write("app.py", source)
    for _ in range(3):  # where the handler lands is a matter of timing
        result = work.python("app.py")
        assert result.returncode == 0
        assert result.stdout == "timed out as designed\n"
        assert result.program_stderr == ""
        recording = work.last()
        assert not any("internal error" in n["reason"] for n in recording.not_collected)
        (exc,) = recording.exceptions
        assert exc["type"] == "__main__.Timeout" and exc["swallowed"] is True
        assert exc["origin"]["fn"] == "on_alarm"
        assert exc["handled"]["fn"] == "<module>"


def test_a_keyboard_interrupt_is_placed_in_the_program_not_in_the_recorder(
    work: Workdir,
) -> None:
    source = """\
import signal

signal.signal(signal.SIGALRM, signal.default_int_handler)

def step(i):
    return i

def work():
    i = 0
    while True:
        i = step(i) + 1

signal.setitimer(signal.ITIMER_REAL, 0.2)
try:
    work()
except KeyboardInterrupt:
    print("interrupted")
"""
    work.write("app.py", source)
    for _ in range(3):
        result = work.python("app.py")
        assert result.returncode == 0 and result.stdout == "interrupted\n"
        (exc,) = work.last().exceptions
        assert exc["type"] == "KeyboardInterrupt" and exc["swallowed"] is True
        assert exc["origin"]["file"].endswith("app.py"), exc["origin"]
        assert exc["origin"]["fn"] in ("step", "work")


def test_exceptions_after_an_await_keep_their_own_type_and_line(
    work: Workdir,
) -> None:
    source = """\
import asyncio

async def main():
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(1)
    await fut                      # 6
    try:
        raise KeyError("k")        # 8
    except KeyError:               # 9
        pass
    await fut                      # 11
    raise ValueError("x")          # 12

asyncio.run(main())
"""
    work.write("app.py", source)
    plain = work.plain(sys.executable, "app.py")
    result = work.python("app.py")
    assert result.returncode == plain.returncode == 1
    assert result.program_stderr == plain.stderr

    recording = work.last()
    seen = sorted(
        (e["type"], e["origin"]["line"], e["end"], e.get("handled", {}).get("line"))
        for e in recording.exceptions
        if e["origin"]["file"].endswith("app.py")
    )
    assert seen == [
        ("KeyError", 8, "handled", 9),
        ("StopIteration", 6, "taken_by_interpreter", None),
        ("StopIteration", 11, "taken_by_interpreter", None),
        ("ValueError", 12, "uncaught", None),
    ]
    assert result.summary[1].startswith("ormaos: swallowed 1x KeyError at app.py:9")
    assert result.summary[2].startswith(
        "ormaos: uncaught ValueError raised at app.py:12"
    )


def test_a_bare_raise_in_a_helper_and_a_stop_iteration_are_one_exception_each(
    work: Workdir,
) -> None:
    source = """\
def again():
    raise

def f():
    try:
        try:
            raise ValueError("v")      # 7
        except ValueError:
            again()
    except ValueError:                 # 10
        pass

class It:
    def __iter__(self):
        return self
    def __next__(self):
        raise StopIteration            # 17

f()
for _ in It():
    pass
print("done")
"""
    work.write("app.py", source)
    out, _ = same(work, "app.py")
    assert out == "done\n"
    seen = [
        (e["type"], e["origin"]["line"], e["end"], e.get("handled", {}).get("line"))
        for e in work.last().exceptions
    ]
    assert seen == [
        ("ValueError", 7, "handled", 10),
        ("StopIteration", 17, "taken_by_interpreter", None),
    ]
    text = work.cli("exceptions", "last").stdout.splitlines()
    assert text[0] == (
        "r1: 2 exceptions (1 swallowed, 0 uncaught, "
        "1 StopIteration taken by the interpreter)"
    )


def test_threads_that_follow_each_other_have_their_own_number(work: Workdir) -> None:
    source = """\
import threading

def job(n):
    return n

for n in range(5):
    thread = threading.Thread(target=job, args=(n,))
    thread.start()
    thread.join()          # the next thread can get the same identifier
"""
    work.write("app.py", source)
    assert work.python("app.py").returncode == 0
    recording = work.last()
    numbers = [call["thread"] for call in recording.calls if call["fn"] == "job"]
    assert numbers == [2, 3, 4, 5, 6]
    doc = json.loads(work.cli("show", "--json").stdout)
    assert doc["context"]["processes"][0]["threads"] == 6


def test_a_program_that_turns_the_recorders_tool_off_is_told_in_the_recording(
    work: Workdir,
) -> None:
    source = """\
import sys

def f(n):
    return n

f(1)
for tool in (3, 4):
    if sys.monitoring.get_tool(tool) == "ormaos":
        sys.monitoring.set_events(tool, 0)
for n in range(5):
    f(n)
print("ok")
"""
    work.write("app.py", source)
    result = work.python("app.py")
    assert result.returncode == 0 and result.stdout == "ok\n"
    recording = work.last()
    assert sum(1 for call in recording.calls if call["fn"] == "f") == 1
    (note,) = recording.not_collected
    assert note["what"] == "what ran after the program changed the monitoring tool"
    assert "sys.monitoring tool" in note["reason"]


def test_a_program_with_its_own_monitoring_tool_and_tracer_is_unchanged(
    work: Workdir,
) -> None:
    source = """\
import sys

seen = {"tool": 0, "trace": 0}
mon = sys.monitoring

def on_start(code, offset):
    if code.co_name == "f":
        seen["tool"] += 1

mon.use_tool_id(2, "mine")
mon.register_callback(2, mon.events.PY_START, on_start)
mon.set_events(2, mon.events.PY_START)

def tracer(frame, event, arg):
    if event == "call" and frame.f_code.co_name == "f":
        seen["trace"] += 1
    return None

sys.settrace(tracer)

def f(n):
    return n

for n in range(10):
    f(n)
sys.settrace(None)
mon.set_events(2, 0)
print(seen)
"""
    work.write("app.py", source)
    out, _ = same(work, "app.py")
    assert out == "{'tool': 10, 'trace': 10}\n"
    assert sum(1 for call in work.last().calls if call["fn"] == "f") == 10


def test_an_interrupt_inside_the_recorder_leaves_no_recorder_frame_in_the_traceback(
    work: Workdir,
) -> None:
    source = """\
import signal, sys, traceback

signal.signal(signal.SIGALRM, signal.default_int_handler)

class Stop(Exception):
    pass

def raise_stop(signum, frame):
    raise Stop()

def step(i):
    return i

def work():
    i = 0
    while True:
        i = step(i) + 1

def frames(error):
    found = traceback.extract_tb(error.__traceback__)
    return [(f.filename.rsplit("/", 1)[-1], f.name) for f in found]

signal.setitimer(signal.ITIMER_REAL, 0.2)
try:
    work()
except KeyboardInterrupt as error:
    seen = frames(error)
    print("interrupt", [name for file, name in seen if file != "app.py"])
    print(seen[0], seen[1][1], len(seen) <= 3)

signal.signal(signal.SIGALRM, raise_stop)
signal.setitimer(signal.ITIMER_REAL, 0.2)
try:
    work()
except Stop as error:
    seen = frames(error)
    print("handler", [name for file, name in seen if file != "app.py"])
    print(seen[0], seen[-1])

signal.setitimer(signal.ITIMER_REAL, 0.2)
signal.signal(signal.SIGALRM, signal.default_int_handler)
work()
"""
    work.write("app.py", source)
    for _ in range(3):  # where the signal lands is a matter of timing
        plain = work.plain(sys.executable, "app.py")
        result = work.python("app.py")
        # Every frame of both tracebacks is a frame of the program.
        assert result.stdout.splitlines()[0] == "interrupt []"
        assert result.stdout.splitlines()[1] == "('app.py', '<module>') work True"
        assert result.stdout.splitlines()[2] == "handler []"
        assert result.stdout.splitlines()[3] == (
            "('app.py', '<module>') ('app.py', 'raise_stop')"
        )
        assert plain.stdout.splitlines()[::2] == result.stdout.splitlines()[::2]
        # The uncaught one: what Python prints has no line of the recorder.
        # Python ends itself with SIGINT; `ormaos run` reports it as a shell does.
        assert (plain.returncode, result.returncode) == (-2, 130)
        printed = result.program_stderr
        assert printed.startswith("Traceback (most recent call last):")
        assert printed.rstrip().endswith("KeyboardInterrupt")
        assert "ormaos" not in printed and "_recorder" not in printed
        files = [line for line in printed.splitlines() if line.startswith("  File ")]
        assert files and all('app.py"' in line for line in files)
