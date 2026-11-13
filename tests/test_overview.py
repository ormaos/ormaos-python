"""The overview readers (`show`, the summary, `list`, `status`) read a
recording line by line and keep no call in memory. They must say exactly
what a full read says."""

import json
import os
import signal
import subprocess
import sys
import time
import tracemalloc

from conftest import Workdir, clean_env
from ormaos.recording import load
from ormaos.show import document
from ormaos.summary import summarize

MIXED = """\
import asyncio, subprocess, sys, threading

def work(n):
    try:
        int("x")
    except ValueError:
        pass
    with open(__file__) as handle:
        handle.read()
    return n

async def job(n):
    await asyncio.sleep(0)
    return work(n)

async def main():
    return await asyncio.gather(job(1), job(2))

def fail():
    raise KeyError("k")

thread = threading.Thread(target=work, args=(0,))
thread.start()
thread.join()
asyncio.run(main())
subprocess.run([sys.executable, "-c", "def child(n): return n\\nchild(3)"], check=True)
fail()
"""
STUCK = """\
import os, time

def step(n):
    try:
        int("x")
    except ValueError:
        pass
    return n

def main():
    n = 0
    while True:
        step(n)
        n += 1
        time.sleep(0.005)
        if n == 80:
            with open("ready", "w") as handle:
                handle.write(str(os.getpid()))

main()
"""


def both(work: Workdir, name: str = "r1") -> tuple[dict, dict]:
    path = str(work.path / ".ormaos" / name)
    full, overview = load(path), load(path, overview=True)
    assert summarize(overview, str(work.path)) == summarize(full, str(work.path))
    assert overview.calls == [] and overview.n_calls == len(full.calls)
    return document(full, str(work.path)), document(overview, str(work.path))


def test_an_overview_says_what_a_full_read_says(work: Workdir) -> None:
    work.write("app.py", MIXED)
    result = work.cli(
        "run",
        "--path",
        "work",
        "--json-out",
        "out.json",
        "--",
        sys.executable,
        "app.py",
    )
    assert result.returncode == 1  # the uncaught KeyError
    full, overview = both(work)
    assert overview == full
    assert full["counts"]["calls"] >= 9 and full["counts"]["processes"] == 2
    assert full["counts"]["uncaught"] == 1 and full["counts"]["swallowed"] >= 3
    # What `ormaos run` wrote and what `ormaos show` prints are that document.
    assert json.loads((work.path / "out.json").read_text()) == full
    assert json.loads(work.cli("show", "--json").stdout) == full
    # The addresses it gives lead where they say.
    first = full["swallowed"][0]["addresses"][0]
    detail = json.loads(work.cli("exceptions", first, "--json").stdout)
    assert detail["type"] == "ValueError" and detail["handled_at"]["fn"] == "work"
    tree = json.loads(work.cli("tree", full["next"][-2], "--json").stdout)
    assert tree["kind"] == "tree"


def test_an_overview_of_a_killed_program_says_what_a_full_read_says(
    work: Workdir,
) -> None:
    work.write("app.py", STUCK)
    proc = subprocess.Popen(
        [sys.executable, "-m", "ormaos", "run", "--", sys.executable, "app.py"],
        cwd=work.path,
        env=clean_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ready = work.path / "ready"
    deadline = time.monotonic() + 30
    while not ready.exists() or not ready.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    os.kill(int(ready.read_text()), signal.SIGKILL)
    proc.wait(timeout=30)

    full, overview = both(work)
    assert overview == full
    assert full["complete"] is False and full["counts"]["calls"] >= 40
    # The calls that were open (from the checkpoints) are counted once.
    listed = json.loads(work.cli("list", "--json").stdout)["recordings"][0]
    assert listed["calls"] == full["counts"]["calls"]


def test_a_large_recording_is_read_without_keeping_its_calls(work: Workdir) -> None:
    source = """\
def leaf(n):
    return n

def main():
    total = 0
    for n in range(60_000):
        total += leaf(n)
    return total

main()
"""
    work.write("app.py", source)
    result = work.python("app.py")
    assert result.returncode == 0
    assert "60002 calls" in result.summary[0]
    path = str(work.path / ".ormaos" / "r1")
    size = sum(f.stat().st_size for f in (work.path / ".ormaos" / "r1").iterdir())
    assert size > 8_000_000

    tracemalloc.start()
    overview = load(path, overview=True)
    document(overview, str(work.path))
    _, peak_overview = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    tracemalloc.start()
    full = load(path)
    _, peak_full = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert overview.n_calls == len(full.calls) == 60_002
    assert overview.calls == [] and overview.io == []
    assert document(overview, str(work.path)) == document(full, str(work.path))
    # Less than the size of the file, and a small part of a full read.
    assert peak_overview < size / 4
    assert peak_overview * 20 < peak_full
    del full

    shown = work.cli("show")
    assert shown.returncode == 0 and "60002 calls" in shown.stdout.splitlines()[0]
    listed = json.loads(work.cli("list", "--json").stdout)["recordings"][0]
    assert listed["calls"] == 60_002
    status = json.loads(work.cli("status", "--json").stdout)
    assert status["last"]["calls"] == 60_002
