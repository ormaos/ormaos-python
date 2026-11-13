"""What is not recorded is said in `not_collected`, when it happens."""

import json
import sys

from conftest import Workdir


def notes(work: Workdir, recording: str = "last") -> list[dict[str, str]]:
    doc = json.loads(work.cli("show", recording, "--json").stdout)
    return [note for note in doc["not_collected"] if not note.get("always")]


def test_child_pythons_that_are_not_recorded_are_counted(work: Workdir) -> None:
    source = """\
import os, subprocess, sys

def start(*options, env=None):
    subprocess.run([sys.executable, *options, "-c", "pass"], check=True, env=env)

def main():
    start()                                    # recorded
    start("-I")                                # isolated: still recorded
    start("-S")                                # no site packages: no recorder
    bare = {k: v for k, v in os.environ.items() if k != "ORMAOS_DIR"}
    start(env=bare)                            # no ORMAOS_DIR: no recorder

main()
"""
    path = work.write("app.py", source)
    line = next(
        number
        for number, text in enumerate(path.read_text().splitlines(), 1)
        if "subprocess.run" in text
    )
    result = work.python("app.py")
    assert result.returncode == 0

    recording = work.last()
    assert len(recording.processes) == 3  # the program and two of its children
    (note,) = [n for n in notes(work) if "Python programs" in n["what"]]
    assert note["what"] == (
        "2 of the 4 Python programs that process 1 started "
        f"(the first at app.py:{line})"
    )
    assert "no ormaos installed" in note["reason"]
    assert "(`-S`)" in note["reason"] and "ORMAOS_DIR" in note["reason"]
    assert "not collected: 2 of the 4 Python programs" in work.cli("show").stdout

    # When every child reports, nothing is said.
    work.write(
        "all.py",
        "import subprocess, sys\n"
        "def main():\n"
        "    for _ in range(3):\n"
        '        subprocess.run([sys.executable, "-c", "pass"], check=True)\n'
        "main()\n",
    )
    assert work.python("all.py").returncode == 0
    assert len(work.last().processes) == 4
    assert not [n for n in notes(work) if "Python programs" in n["what"]]


def test_threads_older_than_a_block_are_said(work: Workdir) -> None:
    source = """\
import threading, time
import ormaos

go = threading.Event()
done = threading.Event()

def inner():
    return 1

def before():           # running when the block starts: not a recorded call
    go.wait()
    inner()             # starts inside the block: recorded, with nothing above
    done.set()

def fresh():
    return inner()

old = threading.Thread(target=before)
old.start()
time.sleep(0.1)
with ormaos.record({options}) as rec:
    go.set()
    done.wait()
    new = threading.Thread(target=fresh)
    new.start()
    new.join()
old.join()
print("done")
"""
    work.write("app.py", source.format(options=""))
    result = work.plain(sys.executable, "app.py")
    assert result.returncode == 0 and result.stdout == "done\n" and result.stderr == ""

    recording = work.last()
    calls = [(c["fn"], c["parent"] is None, c["thread"]) for c in recording.calls]
    assert ("before", True, 1) not in calls and all(c[0] != "before" for c in calls)
    assert sorted(c[0] for c in calls) == ["fresh", "inner", "inner"]
    whats = [note["what"] for note in notes(work)]
    what = "what threads that were already running did before recording started"
    assert whats.count(what) == 1
    (note,) = [n for n in notes(work) if n["what"] == what]
    assert note["reason"].startswith("1 other threads were running when recording")
    assert "calls with no call above them" in note["reason"]

    # A block with only threads it starts itself says nothing about threads.
    quiet = """\
import threading
import ormaos

def fresh():
    return 1

with ormaos.record():
    thread = threading.Thread(target=fresh)
    thread.start()
    thread.join()
"""
    work.write("quiet.py", quiet)
    assert work.plain(sys.executable, "quiet.py").returncode == 0
    assert what not in [note["what"] for note in notes(work)]


def test_a_sampled_container_is_said_with_the_limit(work: Workdir) -> None:
    source = """\
def take(small, big, table):
    return big

take([1, 2, 3], list(range(50)) + ["x"] * 950, {n: str(n) for n in range(51)})
"""
    work.write("app.py", source)
    assert work.python("app.py").returncode == 0

    (call,) = [c for c in work.last().calls if c["fn"] == "take"]
    args = call["args"]
    assert args["small"] == {"type": "list", "len": 3, "items": {"int": 3}}
    # The length is exact; the types are those of the first 50, and it says so.
    assert args["big"] == {
        "type": "list",
        "len": 1000,
        "sampled": 50,
        "items": {"int": 50},
    }
    assert args["table"]["len"] == 51 and args["table"]["sampled"] == 50
    assert call["ret"]["sampled"] == 50
    (note,) = notes(work)
    assert note["what"] == "the types of the items after the first 50 of a container"
    assert "more than 50 items" in note["reason"] and "`sampled`" in note["reason"]
    tree = work.cli("tree", "last").stdout
    assert "big: list[1000 int in the first 50]" in tree
    assert "small: list[3 int]" in tree

    # Nothing is said when no container is that long.
    work.write("small.py", "def take(items):\n    return items\n\ntake([1] * 50)\n")
    assert work.python("small.py").returncode == 0
    assert notes(work) == []


def test_a_name_that_is_cut_is_said_with_the_limit(work: Workdir) -> None:
    source = """\
import os

def touch(name):
    with open(name, "w") as handle:
        handle.write("x")

os.makedirs("d" * 200 + "/" + "e" * 200)
touch("d" * 200 + "/" + "e" * 200 + "/" + "f" * 200)
touch("short.txt")
"""
    work.write("app.py", source)
    assert work.python("app.py").returncode == 0
    recording = work.last()
    targets = [e["target"] for e in recording.io if e["op"] == "open"]
    assert sorted(len(target) for target in targets)[-1] == 300
    cut = [n for n in notes(work) if n["what"].startswith("io: the end of")]
    assert len(cut) == 1
    assert cut[0]["what"].endswith("longer than 300 characters")
    assert "kept up to 300 characters" in cut[0]["reason"]
