"""`ormaos run`: the recorder starts in the child and in its Python children."""

import json
import os
import sys

from conftest import Workdir
from ormaos.recording import FORMAT
from ormaos.summary import MAX_LINES

APP = """\
import subprocess, sys, threading

class Order:
    pass

def total(items, order, *rest, flag=False, **extra):
    return len(items)

def numbers():
    yield 1
    yield 2

def worker():
    total([1], None)

print("total", total(["a", "b"], Order(), 1, 2, flag=True, note="x"))
print("sum", sum(numbers()))
thread = threading.Thread(target=worker)
thread.start()
thread.join()
code = "def child(n):\\n    return n\\nchild(3)"
subprocess.run([sys.executable, "-c", code], check=True)
print("done", file=sys.stderr)
"""


def test_program_runs_unchanged_and_is_recorded(work: Workdir) -> None:
    work.write("app.py", APP)
    plain = work.plain(sys.executable, "app.py")
    result = work.python("app.py")

    assert result.returncode == plain.returncode == 0
    assert result.stdout == plain.stdout
    assert result.program_stderr == plain.stderr
    assert 1 <= len(result.summary) <= MAX_LINES

    recording = work.last()
    assert recording.header["command"] == [sys.executable, "app.py"]
    assert recording.end is not None and recording.end["exit_code"] == 0
    assert "PATH" in recording.header["env_names"]
    assert recording.not_collected == []


def test_every_file_has_the_format_header(work: Workdir) -> None:
    work.write("app.py", APP)
    work.python("app.py")
    files = list((work.path / ".ormaos" / "r1").iterdir())
    assert len(files) == 3  # run.jsonl, the program, its Python child
    for file in files:
        header = json.loads(file.read_text().splitlines()[0])
        assert header["format"] == FORMAT


def test_python_child_processes_are_recorded(work: Workdir) -> None:
    work.write("app.py", APP)
    work.python("app.py")
    parent, child = work.last().processes
    assert child.header["ppid"] == parent.header["pid"]
    assert parent.header["argv"] == {"mode": "script", "target": "app.py", "argc": 2}
    assert child.header["argv"]["mode"] == "command"
    assert [c["fn"] for c in child.calls] == ["<module>", "child"]
    assert child.calls[1]["args"] == {"n": {"type": "int"}}
    assert parent.end is not None and parent.end["threads"] == 2


def test_calls_form_a_tree_with_argument_shapes(work: Workdir) -> None:
    work.write("app.py", APP)
    work.python("app.py")
    recording = work.last()
    calls = recording.processes[0].calls
    by_fn = {c["fn"]: c for c in calls}

    module = by_fn["<module>"]
    assert module["parent"] is None and module["addr"] == "r1:c1"

    total = next(c for c in calls if c["fn"] == "total")
    assert total["parent_addr"] == module["addr"]
    assert total["out"] == "returned" and total["ret"] == {"type": "int"}
    assert total["args"] == {
        "items": {"type": "list", "len": 2, "items": {"str": 2}},
        "order": {"type": "__main__.Order"},
        "flag": {"type": "bool"},
        "rest": {"type": "tuple", "len": 2, "items": {"int": 2}},
        "extra": {"type": "dict", "len": 1, "keys": {"str": 1}, "values": {"str": 1}},
    }
    assert os.path.basename(total["file"]) == "app.py" and total["line"] == 6

    worker = by_fn["worker"]
    assert worker["thread"] == 2
    in_thread = [c for c in calls if c["fn"] == "total" and c["thread"] == 2]
    assert in_thread[0]["parent"] == worker["id"]

    # Only user code: nothing from the standard library.
    assert {os.path.basename(c["file"]) for c in calls} == {"app.py"}
    assert by_fn["numbers"]["out"] == "returned"


def test_exit_code_is_the_childs(work: Workdir) -> None:
    result = work.python("-c", "import sys; print('bye'); sys.exit(7)")
    assert result.returncode == 7
    assert result.stdout == "bye\n"
    assert "exit 7" in result.summary[0]


def test_exit_code_when_killed_by_a_signal(work: Workdir) -> None:
    result = work.python(
        "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    )
    assert result.returncode == 128 + 9
    recording = work.last()
    assert recording.end is not None and recording.end["signal"] == 9
    # The process could not write its last events: it says so.
    (note,) = [n for n in recording.not_collected if "last events" in n["what"]]
    assert note["reason"].startswith(
        "it was ended by the program (process 1) with signal 9 before it had written"
    )
    assert not recording.complete


def test_command_that_is_not_python(work: Workdir) -> None:
    result = work.ormaos("sh", "-c", "echo hi; exit 3")
    assert result.returncode == 3
    assert result.stdout == "hi\n"
    assert "no Python process recorded" in result.summary[0]
    assert work.last().not_collected[0]["what"] == "everything inside Python"


def test_command_not_found(work: Workdir) -> None:
    result = work.ormaos("no-such-command-ormaos")
    assert result.returncode == 127


def test_recordings_are_numbered(work: Workdir) -> None:
    work.python("-c", "pass")
    work.python("-c", "pass")
    assert sorted(p.name for p in (work.path / ".ormaos").iterdir()) == [
        ".gitignore",
        "r1",
        "r2",
    ]
    assert work.last().name == "r2"


def test_forked_child_gets_its_own_file(work: Workdir) -> None:
    source = """\
import os

def in_child():
    return 1

def in_parent():
    return 2

pid = os.fork()
if pid == 0:
    in_child()
else:
    os.waitpid(pid, 0)
    in_parent()
"""
    work.write("fork.py", source)
    assert work.python("fork.py").returncode == 0
    parent, child = work.last().processes
    assert child.header["forked"] is True
    assert child.header["ppid"] == parent.header["pid"]
    assert "in_child" in [c["fn"] for c in child.calls]
    assert "in_parent" in [c["fn"] for c in parent.calls]
    assert "in_child" not in [c["fn"] for c in parent.calls]


def test_only_limits_what_is_user_code(work: Workdir) -> None:
    (work.path / "pkg").mkdir()
    work.write("pkg/inner.py", "def inner():\n    return 1\n")
    work.write(
        "main.py", "from pkg.inner import inner\ndef outer():\n    inner()\nouter()\n"
    )
    work.python("main.py", env={"ORMAOS_ONLY": str(work.path / "pkg")})
    files = {os.path.basename(c["file"]) for c in work.last().calls}
    assert files == {"inner.py"}


def test_call_limit_is_reported(work: Workdir) -> None:
    code = "def f():\n    pass\nfor _ in range(50):\n    f()\n"
    work.python("-c", code, env={"ORMAOS_MAX_CALLS": "10"})
    recording = work.last()
    assert len(recording.calls) == 10
    assert "limit of 10 calls" in recording.not_collected[0]["reason"]


def test_exception_limit_is_reported(work: Workdir) -> None:
    code = (
        "def f():\n    try:\n        raise KeyError\n    except KeyError:\n"
        "        pass\nfor _ in range(50):\n    f()\n"
    )
    work.python("-c", code, env={"ORMAOS_MAX_EXCEPTIONS": "10"})
    recording = work.last()
    assert len(recording.exceptions) == 10
    assert "limit of 10 exceptions" in recording.not_collected[0]["reason"]


def test_line_limit_is_reported(work: Workdir) -> None:
    work.write(
        "loop.py", "def f():\n    a = 1\n    return a\nfor _ in range(50):\n    f()\n"
    )
    command = ("run", "--path", "f", "--", sys.executable, "loop.py")
    result = work.cli(*command, env={"ORMAOS_MAX_LINES": "10"})
    assert result.returncode == 0
    recording = work.last()
    assert sum(len(call.get("path", ())) for call in recording.calls) == 10
    assert "limit of 10 lines" in recording.not_collected[0]["reason"]


def test_no_free_tool_id_is_reported(work: Workdir) -> None:
    # The program takes both tool ids before the recorder starts (`-S`: the
    # recorder starts when the program runs `site.main()`).
    source = (
        "import sys\n"
        "sys.monitoring.use_tool_id(3, 'one')\n"
        "sys.monitoring.use_tool_id(4, 'two')\n"
        "import site\n"
        "site.main()\n"
        "def f():\n"
        "    return 1\n"
        "print(f(), sys.monitoring.get_tool(3), sys.monitoring.get_tool(4))\n"
    )
    work.write("taken.py", source)
    result = work.python("-S", "taken.py")
    assert (result.returncode, result.stdout) == (0, "1 one two\n")
    recording = work.last()
    assert recording.calls == []
    assert recording.not_collected[0] == {
        "k": "not_collected",
        "what": "everything",
        "reason": "no free sys.monitoring tool id (3 and 4 are in use)",
    }


def test_without_ormaos_dir_nothing_happens(work: Workdir) -> None:
    result = work.plain(
        sys.executable, "-c", "import sys; print(sys.monitoring.get_tool(4))"
    )
    assert result.stdout == "None\n"
    assert not (work.path / ".ormaos").exists()
