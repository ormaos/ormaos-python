"""A recorded program that is killed: what it wrote is read, and the
recording is marked incomplete."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from conftest import Workdir, clean_env

APP = """\
import os, time
{head}

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

{call}
"""


def kill_when_ready(work: Workdir, argv: list[str]) -> subprocess.Popen[str]:
    """Start the program, wait until it has run for a while, SIGKILL it."""
    proc = subprocess.Popen(
        argv,
        cwd=work.path,
        env=clean_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready = Path(work.path / "ready")
    deadline = time.monotonic() + 30
    while not ready.exists() or not ready.read_text():
        assert proc.poll() is None, proc.stderr.read() if proc.stderr else ""
        assert time.monotonic() < deadline, "the program never got ready"
        time.sleep(0.02)
    os.kill(int(ready.read_text()), signal.SIGKILL)
    proc.wait(timeout=30)
    return proc


def test_a_killed_program_is_read_as_far_as_it_was_written(work: Workdir) -> None:
    work.write("app.py", APP.format(head="", call="main()"))
    ormaos = [sys.executable, "-m", "ormaos", "run", "--"]
    proc = kill_when_ready(work, [*ormaos, sys.executable, "app.py"])

    # `ormaos run` saw the kill and still wrote its own end and the summary.
    assert proc.returncode == 128 + signal.SIGKILL
    assert proc.stderr is not None
    summary = proc.stderr.read().splitlines()
    assert summary[0].startswith("ormaos: r1 (incomplete) exit 137 (signal 9) in ")

    shown = work.cli("show", "--json")
    assert shown.returncode == 0
    doc = json.loads(shown.stdout)
    assert doc["complete"] is False
    assert doc["incomplete"] == ["process 1 wrote no end record"]
    assert doc["context"]["exit_code"] == 137 and doc["context"]["signal"] == 9
    (process,) = doc["context"]["processes"]
    assert process["complete"] is False and process["written_until_s"] > 0
    # At least the chunks written before the kill: most of the 80 steps.
    assert doc["counts"]["calls"] >= 40 and doc["counts"]["swallowed"] >= 40
    (group,) = doc["swallowed"]
    assert (group["type"], group["handled_at"]["fn"]) == ("ValueError", "step")
    assert any(
        note["what"].startswith("the last events of process")
        for note in doc["not_collected"]
    )

    lines = work.cli("show").stdout.splitlines()
    assert lines[0].startswith("r1 (incomplete) exit 137 (signal 9)")
    assert lines[1].startswith("incomplete: process 1 wrote no end record")

    # The calls that had not returned are there, marked open, above the rest.
    tree = json.loads(work.cli("tree", "r1", "--json").stdout)
    top, below = tree["nodes"][0], tree["nodes"][1]
    assert (top["fn"], top["out"]) == ("<module>", "open")
    assert (below["fn"], below["out"]) == ("main", "open")
    assert below["calls_below"] >= 40
    assert tree["nodes"][2]["fn"] == "step" and tree["nodes"][2]["out"] == "returned"

    listed = json.loads(work.cli("list", "--json").stdout)["recordings"][0]
    assert listed["complete"] is False and listed["running"] is False
    assert "exit 137 (signal 9), incomplete" in work.cli("list").stdout
    status = json.loads(work.cli("status", "--json").stdout)
    assert status["incomplete"] == ["r1"] and status["running"] == []


def test_a_killed_record_block_has_no_end_and_is_still_read(work: Workdir) -> None:
    head = "import ormaos"
    call = "with ormaos.record():\n    main()"
    work.write("app.py", APP.format(head=head, call=call))
    proc = kill_when_ready(work, [sys.executable, "app.py"])

    assert proc.returncode == -signal.SIGKILL
    doc = json.loads(work.cli("show", "--json").stdout)
    assert doc["complete"] is False
    assert doc["incomplete"] == [
        "the run has no end record",
        "process 1 wrote no end record",
    ]
    assert doc["context"]["mode"] == "record"
    assert doc["counts"]["calls"] >= 40 and doc["counts"]["swallowed"] >= 40
    first = work.cli("show").stdout.splitlines()[0]
    assert first.startswith("r1 (incomplete) record() block, not ended: ")
    tree = json.loads(work.cli("tree", "r1", "--json").stdout)
    assert (tree["nodes"][0]["fn"], tree["nodes"][0]["out"]) == ("main", "open")


def test_a_recording_that_is_still_running_is_listed_as_running(
    work: Workdir,
) -> None:
    work.write("app.py", APP.format(head="", call="main()"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "ormaos", "run", "--", sys.executable, "app.py"],
        cwd=work.path,
        env=clean_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready = work.path / "ready"
        deadline = time.monotonic() + 30
        while not ready.exists() or not ready.read_text():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        status = json.loads(work.cli("status", "--json").stdout)
        assert status["running"] == ["r1"] and status["incomplete"] == []
        assert "running now: r1" in work.cli("status").stdout
        assert "no exit code, running" in work.cli("list").stdout
        # Reading while it runs: what was written so far.
        doc = json.loads(work.cli("show", "--json").stdout)
        assert doc["complete"] is False and doc["counts"]["calls"] >= 40
    finally:
        os.kill(int((work.path / "ready").read_text()), signal.SIGKILL)
        proc.wait(timeout=30)
