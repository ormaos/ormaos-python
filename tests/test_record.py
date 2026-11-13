"""`with ormaos.record():` records a block of a script or of a notebook cell."""

import json
import secrets
import sys

from conftest import Workdir

APP = """\
import os, subprocess, sys
import ormaos

def before():
    return 1

def work(items):
    try:
        int("x")
    except ValueError:
        pass
    return len(items)

def after():
    return 2

before()
with ormaos.record({options}) as rec:
    print("total", work(["a", "b"]))
    inside = os.environ.get("ORMAOS_DIR")
    {child}
after()
print("name", rec.name, "reason", rec.reason)
print("inside", inside == rec.directory, "left", "ORMAOS_DIR" in os.environ)
"""
CHILD = (
    'subprocess.run([sys.executable, "-c", "def child(n): return n\\nchild(3)"], '
    "check=True)"
)


def app(options: str = "", child: str = "pass") -> str:
    return APP.format(options=options, child=child)


def test_a_block_is_recorded_and_the_script_runs_unchanged(work: Workdir) -> None:
    work.write("app.py", app())
    result = work.plain(sys.executable, "app.py")

    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout.splitlines() == [
        "total 2",
        "name r1 reason None",
        "inside True left False",
    ]
    recording = work.last()
    assert recording.header["mode"] == "record"
    assert recording.header["command"] == [sys.executable, "app.py"]
    assert recording.complete and recording.not_collected == []
    # Only what ran inside the block: not `before`, not `after`.
    assert [call["fn"] for call in recording.calls] == ["work"]
    (exc,) = recording.exceptions
    assert exc["type"] == "ValueError" and exc["swallowed"] is True
    (process,) = recording.processes
    assert process.header["started_by"] == "record"

    shown = work.cli("show")
    assert shown.returncode == 0
    lines = shown.stdout.splitlines()
    assert lines[0].startswith("r1 record() block in ")
    assert lines[-1].startswith("next: ormaos exceptions r1:e1")
    doc = json.loads(work.cli("show", "--json").stdout)
    assert doc["complete"] is True and doc["context"]["mode"] == "record"
    assert doc["context"]["exit_code"] is None


def test_python_started_inside_the_block_is_in_the_same_recording(
    work: Workdir,
) -> None:
    work.write("app.py", app(child=CHILD))
    result = work.plain(sys.executable, "app.py")

    assert result.returncode == 0
    recording = work.last()
    assert recording.number == 1 and recording.complete
    first, second = recording.processes
    assert second.parent == first.index and second.header["started_by"] == "run"
    assert [call["fn"] for call in second.calls] == ["<module>", "child"]


def test_two_blocks_make_two_recordings(work: Workdir) -> None:
    source = """\
import ormaos

def one():
    return 1

def two():
    return 2

with ormaos.record():
    one()
with ormaos.record(summary=True):
    two()
"""
    work.write("app.py", source)
    result = work.plain(sys.executable, "app.py")

    assert result.returncode == 0
    assert result.stderr.splitlines()[0].startswith("ormaos: r2 record() block in ")
    assert len(result.stderr.splitlines()) <= 5
    names = json.loads(work.cli("list", "--json").stdout)["recordings"]
    assert [entry["recording"] for entry in names] == ["r2", "r1"]
    one = json.loads(work.cli("tree", "r1", "--json").stdout)
    two = json.loads(work.cli("tree", "r2", "--json").stdout)
    assert [node["fn"] for node in one["nodes"]] == ["one"]
    assert [node["fn"] for node in two["nodes"]] == ["two"]


def test_path_and_io_options(work: Workdir) -> None:
    work.write("data.txt", "x\n")
    source = """\
import ormaos

def read():
    with open("data.txt") as handle:
        text = handle.read()
    return text

with ormaos.record(path="read", io={io}):
    read()
"""
    work.write("app.py", source.format(io=True))
    assert work.plain(sys.executable, "app.py").returncode == 0
    recording = work.last()
    (call,) = recording.calls
    assert len(call["path"]) >= 3
    assert [event["kind"] for event in recording.io] == ["file", "file"]
    assert recording.processes[0].header["capabilities"] == [
        "context",
        "calls",
        "exceptions",
        "path",
        "io",
    ]

    work.write("app.py", source.format(io=False))
    assert work.plain(sys.executable, "app.py").returncode == 0
    recording = work.last()
    assert recording.number == 2 and recording.io == []
    assert "io" not in recording.processes[0].header["capabilities"]


def test_a_block_under_ormaos_run_is_part_of_that_recording(work: Workdir) -> None:
    work.write("app.py", app())
    result = work.python("app.py")

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "total 2",
        "name r1 reason already recording",
        "inside True left True",
    ]
    recording = work.last()
    assert recording.number == 1 and recording.header["mode"] == "run"
    fns = [call["fn"] for call in recording.calls]
    assert fns == ["<module>", "before", "work", "after"]


def test_an_exception_of_the_block_goes_through_unchanged(work: Workdir) -> None:
    source = """\
import ormaos

def fail():
    raise KeyError("missing")

try:
    with ormaos.record():
        fail()
except KeyError as err:
    print("caught", type(err).__name__, err.args)
"""
    work.write("app.py", source)
    result = work.plain(sys.executable, "app.py")

    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout == "caught KeyError ('missing',)\n"
    recording = work.last()
    assert recording.complete and recording.end is not None
    assert recording.end["raised"] is True
    (call,) = recording.calls
    assert (call["fn"], call["out"]) == ("fail", "raised")
    (exc,) = recording.exceptions
    # It left the block: not swallowed, and no handler was seen in the block.
    assert exc["type"] == "KeyError" and "swallowed" not in exc
    assert exc["end"] == "no_handler_seen" and "handled" not in exc


def test_fail_open_when_the_recording_cannot_be_made(work: Workdir) -> None:
    work.write("not-a-folder", "")
    source = """\
import ormaos

def work():
    return 1

with ormaos.record(root="not-a-folder") as rec:
    print("ran", work())
print("name", rec.name, "reason", rec.reason)
"""
    work.write("app.py", source)
    result = work.plain(sys.executable, "app.py")

    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout.splitlines()[0] == "ran 1"
    assert result.stdout.splitlines()[1].startswith("name None reason cannot start (")
    assert not (work.path / ".ormaos").exists()


def test_a_notebook_cell_is_user_code(work: Workdir) -> None:
    # IPython and Jupyter compile each cell under a name like this one.
    source = """\
import ormaos

cell = '''
def in_cell(n):
    return n + 1

with ormaos.record():
    in_cell(1)
'''
exec(compile(cell, "<ipython-input-3-0123abcd>", "exec"), globals())
"""
    work.write("app.py", source)
    result = work.plain(sys.executable, "app.py")

    assert result.returncode == 0 and result.stderr == ""
    (call,) = work.last().calls
    assert call["fn"] == "in_cell" and call["file"] == "<ipython-input-3-0123abcd>"


def test_no_value_of_the_block_reaches_the_recording(work: Workdir) -> None:
    planted = f"planted-{secrets.token_hex(10)}"
    source = """\
import os, sys
import ormaos

def keep(value):
    try:
        raise ValueError(value)
    except ValueError:
        return value

with ormaos.record(path="keep"):
    keep(sys.argv[1])
    keep(os.environ["PLANTED_VARIABLE"])
"""
    work.write("app.py", source)
    result = work.plain(
        sys.executable, "app.py", planted, env={"PLANTED_VARIABLE": planted}
    )

    assert result.returncode == 0
    files = [p for p in (work.path / ".ormaos").rglob("*") if p.is_file()]
    assert len(files) >= 3
    for file in files:
        assert planted not in file.read_text(encoding="utf-8"), file.name
    assert "PLANTED_VARIABLE" in work.last().header["env_names"]
