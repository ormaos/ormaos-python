"""Python subprocesses and multiprocessing children are in the recording of
the program that started them, each linked to the process that started it."""

import json
import sys

import pytest

from conftest import Workdir

CHAIN = """\
import subprocess, sys

def level(n):
    return n

level(int(sys.argv[1]))
if int(sys.argv[1]) < 3:
    subprocess.run([sys.executable, __file__, str(int(sys.argv[1]) + 1)], check=True)
"""

WORKERS = """\
import multiprocessing as mp, sys

def work(n):
    try:
        int("x")
    except ValueError:
        pass
    return n + 1

if __name__ == "__main__":
    ctx = mp.get_context(sys.argv[1])
    children = [ctx.Process(target=work, args=(n,)) for n in range(2)]
    for child in children:
        child.start()
    for child in children:
        child.join()
    print("exit codes", [child.exitcode for child in children])
    with ctx.Pool(2) as pool:
        print("pool", pool.map(work, [1, 2, 3]))
"""


def test_a_chain_of_python_subprocesses_is_linked(work: Workdir) -> None:
    work.write("app.py", CHAIN)
    result = work.python("app.py", "1")

    assert result.returncode == 0
    recording = work.last()
    assert recording.complete
    one, two, three = recording.processes
    assert (one.parent, two.parent, three.parent) == (None, 1, 2)
    assert [p.header["forked"] for p in recording.processes] == [False] * 3
    for process in recording.processes:
        assert [call["fn"] for call in process.calls] == ["<module>", "level"]
        assert {call["process"] for call in process.calls} == {process.index}

    doc = json.loads(work.cli("show", "--json").stdout)
    parents = [p["parent_process"] for p in doc["context"]["processes"]]
    assert parents == [None, 1, 2]
    text = work.cli("show").stdout
    assert "process 1: the first, 2 calls, 0 exceptions" in text
    assert "process 3: started by process 2, 2 calls, 0 exceptions" in text
    # A call of a child says which process it is in.
    tree = work.cli("tree", "r1", "--limit", "10").stdout
    assert "process 3" in tree and "process 1" not in tree


@pytest.mark.parametrize("method", ["fork", "spawn", "forkserver"])
def test_multiprocessing_children_are_recorded_and_linked(
    work: Workdir, method: str
) -> None:
    work.write("app.py", WORKERS)
    plain = work.plain(sys.executable, "app.py", method)
    result = work.python("app.py", method)

    assert result.returncode == plain.returncode == 0
    assert result.stdout == plain.stdout == "exit codes [0, 0]\npool [2, 3, 4]\n"
    assert result.program_stderr == plain.stderr
    recording = work.last()
    processes = recording.processes
    assert processes[0].parent is None
    # Every other process was started, directly or not, by the first one.
    for process in processes[1:]:
        up = process
        while up.parent is not None:
            up = processes[up.parent - 1]
        assert up is processes[0], process.header
    workers = [
        process
        for process in processes[1:]
        if any(call["fn"] == "work" for call in process.calls)
    ]
    if method == "fork":
        assert all(process.header["forked"] for process in workers)
        assert all(process.parent == 1 for process in workers)
    # The two `Process` children ran `work` once each and wrote their end
    # record, though multiprocessing ends a child with `os._exit`.
    finished = [process for process in workers if process.end is not None]
    assert len(finished) >= 2
    # Every call of `work` is there: 2 in the `Process` children, 3 in the
    # pool workers, which the pool ends with a signal.
    calls = [c for p in workers for c in p.calls if c["fn"] == "work"]
    assert len(calls) == 5
    assert all(call["out"] == "returned" for call in calls)
    swallowed = [e for p in workers for e in p.exceptions if e.get("swallowed")]
    assert len(swallowed) == 5

    # The pool ended the workers that were still alive with a signal. That is
    # the program's own doing: the recording is not incomplete because of it.
    assert recording.complete and recording.incomplete_reasons == []
    assert "(incomplete)" not in result.stderr
    ended = [process for process in processes if process.end is None]
    for process in ended:
        assert process.ended_by == {"process": 1, "by": "pool", "signal": 15}
    doc = json.loads(work.cli("show", "--json").stdout)
    assert doc["complete"] is True and doc["incomplete"] == []
    shown = {p["process"]: p for p in doc["context"]["processes"]}
    text = work.cli("show").stdout
    assert "(incomplete)" not in text and "no end record" not in text.split("\n")[1]
    for process in ended:
        assert shown[process.index]["complete"] is False  # it has no end record
        assert shown[process.index]["ended_by"]["by"] == "pool"
        assert f"process {process.index}: " in text
        note = f"the end record of process {process.header['pid']}"
        (reason,) = [n["reason"] for n in doc["not_collected"] if n["what"] == note]
        assert reason.startswith("its pool ended it with signal 15, so it wrote no")
        assert reason.endswith("no call of your code was running in it")
    assert text.count(", ended by its pool (signal 15)") == len(ended)
    listed = json.loads(work.cli("list", "--json").stdout)["recordings"][0]
    assert listed["complete"] is True


ENDED = """\
import multiprocessing as mp, os, signal, sys, time

def work():
    with open("ready", "w") as handle:
        handle.write(str(os.getpid()))
    time.sleep(60)

if __name__ == "__main__":
    child = mp.get_context("fork").Process(target=work)
    child.start()
    while not os.path.exists("ready") or not open("ready").read():
        time.sleep(0.01)
    if sys.argv[1] == "terminate":
        child.terminate()          # the program ends its own child
    else:
        os.kill(int(open("ready").read()), signal.SIGKILL)   # as from outside
    child.join()
    print("child exit code", child.exitcode)
"""


def test_a_child_the_program_ended_itself_is_told_from_one_killed(
    work: Workdir,
) -> None:
    work.write("app.py", ENDED)

    result = work.python("app.py", "terminate")
    assert result.stdout == "child exit code -15\n"
    recording = work.last()
    _parent, child = recording.processes
    assert child.end is None
    assert child.ended_by == {"process": 1, "by": "terminate", "signal": 15}
    assert recording.complete and "(incomplete)" not in result.stderr
    (note,) = [n for n in recording.not_collected if "end record" in n["what"]]
    assert note["reason"].startswith(
        "process 1 ended it with multiprocessing with signal 15"
    )
    # It was in the middle of `work`: said, and shown as open.
    assert note["reason"].endswith(
        "1 of its calls had not returned and are marked open"
    )
    assert [(c["fn"], c["out"]) for c in child.calls] == [("work", "open")]
    text = work.cli("show").stdout
    assert (
        "process 2: forked from process 1, 1 call, 0 exceptions, "
        "ended by process 1 (signal 15)" in text
    )

    # The same child killed with a signal that did not go through
    # multiprocessing: nothing in the recording says who did it.
    (work.path / "ready").unlink()
    result = work.python("app.py", "kill")
    assert result.stdout == "child exit code -9\n"
    recording = work.last()
    _parent, child = recording.processes
    assert child.end is None and child.ended_by is None
    assert not recording.complete
    assert recording.incomplete_reasons == ["process 2 wrote no end record"]
    assert result.summary[0].startswith("ormaos: r2 (incomplete) exit 0 ")
    assert "process 2: forked from process 1, 1 call, 0 exceptions, no end record" in (
        work.cli("show").stdout
    )
