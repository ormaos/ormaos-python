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

    # The pool ended the workers that were still alive with a signal: said
    # for each. (Whether one of them was still starting is a matter of timing
    # here; the tests below fix it.)
    for process in processes:
        if process.end is None:
            assert process.ending == {"signal": 15, "by": "pool", "process": 1}


ENDS = """\
import multiprocessing as mp, os, signal, sys, time

def quick(n):
    return n + 1

def slow(n):
    open(f"busy-{os.getpid()}", "w").close()
    time.sleep(60)

def slow_start():
    open(f"busy-{os.getpid()}", "w").close()

def wait_busy(count):
    while len([name for name in os.listdir(".") if name.startswith("busy-")]) < count:
        time.sleep(0.01)

if __name__ == "__main__":
    case, method = sys.argv[1], sys.argv[2]
    ctx = mp.get_context(method)
    if case == "idle":
        with ctx.Pool(2, initializer=slow_start) as pool:
            wait_busy(2)                       # both workers are up
            print(pool.map(quick, [1, 2, 3]))
    elif case == "midtask":
        with ctx.Pool(2) as pool:
            pool.map_async(slow, [1, 2])
            wait_busy(2)
    else:
        child = ctx.Process(target=slow, args=(1,))
        child.start()
        wait_busy(1)
        if case == "terminate":
            child.terminate()
        elif case == "kill":
            child.kill()
        else:
            os.kill(child.pid, signal.SIGKILL)
        child.join()
        print("exit", child.exitcode)
"""
METHODS = ["fork", "spawn", "forkserver"]


def ended(work: Workdir) -> tuple[dict[str, object], list[dict[str, object]], str]:
    """The overview, its processes that have no end record, and the text."""
    doc = json.loads(work.cli("show", "--json").stdout)
    without_end = [p for p in doc["context"]["processes"] if not p["complete"]]
    return doc, without_end, work.cli("show").stdout


@pytest.mark.parametrize("method", METHODS)
def test_a_pool_that_ends_idle_workers_is_complete(work: Workdir, method: str) -> None:
    work.write("app.py", ENDS)
    result = work.python("app.py", "idle", method)

    assert result.stdout == "[2, 3, 4]\n"
    assert result.summary[0].startswith("ormaos: r1 exit 0 ")
    doc, without_end, text = ended(work)
    assert doc["complete"] is True and doc["incomplete"] == []
    # A worker either got the pool's signal while it waited for a task, or
    # had already left by itself (timing decides). Both are complete.
    for process in without_end:
        # Which signal, and who: said for every process a signal ended.
        assert process["ended"] == {
            "signal": 15,
            "by": "pool",
            "process": 1,
            "lost": False,
        }
        assert (
            f"process {process['process']}: " in text
            and "was ended by its pool with signal 15, nothing left to write" in text
        )
    notes = [n for n in doc["not_collected"] if n["what"].startswith("the end record")]
    assert len(notes) == len(without_end)
    assert all(
        note["reason"].startswith(
            "it was ended by its pool with signal 15; it had no event left to write."
        )
        for note in notes
    )
    # Every task is there.
    tree = json.loads(work.cli("tree", "r1", "--depth", "9", "--json").stdout)
    assert sum(1 for node in tree["nodes"] if node["fn"] == "quick") == 3
    listed = json.loads(work.cli("list", "--json").stdout)["recordings"][0]
    assert listed["complete"] is True


@pytest.mark.parametrize("method", METHODS)
def test_a_pool_ended_in_the_middle_of_a_task_is_incomplete(
    work: Workdir, method: str
) -> None:
    work.write("app.py", ENDS)
    result = work.python("app.py", "midtask", method)

    assert result.returncode == 0
    assert result.summary[0].startswith("ormaos: r1 (incomplete) exit 0 ")
    doc, without_end, text = ended(work)
    assert doc["complete"] is False
    busy = [p for p in without_end if p["ended"]["lost"]]
    assert len(busy) == 2
    for process in busy:
        assert process["ended"] == {
            "signal": 15,
            "by": "pool",
            "process": 1,
            "lost": True,
        }
        reason = (
            f"process {process['process']} was ended by its pool with signal 15, "
            "with events not written"
        )
        assert reason in doc["incomplete"] and reason in text
    assert len(doc["incomplete"]) == 2
    # The task each worker was in is there, marked open.
    recording = work.last()
    open_calls = [c["fn"] for c in recording.calls if c["out"] == "open"]
    assert open_calls.count("slow") == 2
    notes = [n for n in doc["not_collected"] if n["what"].startswith("the last events")]
    assert len(notes) == 2 and all(
        n["reason"].startswith(
            "it was ended by its pool with signal 15 before it had written everything"
        )
        for n in notes
    )


@pytest.mark.parametrize(
    ("case", "signum", "by", "said"),
    [
        (
            "terminate",
            15,
            "program",
            "was ended by the program (process 1) with signal 15",
        ),
        ("kill", 9, "program", "was ended by the program (process 1) with signal 9"),
        ("outside", 9, "outside", "was ended by signal 9 from outside the program"),
    ],
)
@pytest.mark.parametrize("method", ["fork", "spawn"])
def test_a_child_ended_in_the_middle_of_its_work_is_incomplete(
    work: Workdir, method: str, case: str, signum: int, by: str, said: str
) -> None:
    work.write("app.py", ENDS)
    result = work.python("app.py", case, method)

    assert result.stdout == f"exit {-signum}\n"
    assert result.summary[0].startswith("ormaos: r1 (incomplete) exit 0 ")
    doc, without_end, text = ended(work)
    (child,) = [p for p in without_end if p["ended"]["lost"]]
    assert child["ended"] == {
        "signal": signum,
        "by": by,
        "process": 1 if by == "program" else None,
        "lost": True,
    }
    number = child["process"]
    assert doc["complete"] is False
    assert doc["incomplete"] == [f"process {number} {said}, with events not written"]
    assert f"{said}, events not written" in text
    recording = work.last()
    assert [
        c["fn"] for c in recording.processes[number - 1].calls if c["out"] == "open"
    ][-1] == "slow"


def test_a_child_the_program_ends_when_it_has_nothing_left_is_complete(
    work: Workdir,
) -> None:
    source = """\
import multiprocessing as mp, time

if __name__ == "__main__":
    ctx = mp.get_context("fork")
    stop = ctx.Event()
    # The child waits inside the library: no code of the program runs in it.
    child = ctx.Process(target=stop.wait)
    child.start()
    time.sleep(0.3)
    child.terminate()
    child.join()
    print("exit", child.exitcode)
"""
    work.write("app.py", source)
    result = work.python("app.py")

    assert result.stdout == "exit -15\n"
    assert result.summary[0].startswith("ormaos: r1 exit 0 ")
    doc, without_end, text = ended(work)
    (child,) = without_end
    assert child["ended"] == {
        "signal": 15,
        "by": "program",
        "process": 1,
        "lost": False,
    }
    assert doc["complete"] is True
    assert (
        "was ended by the program (process 1) with signal 15, nothing left to write"
        in text
    )
