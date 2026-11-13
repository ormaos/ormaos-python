"""`ormaos list` and `ormaos status`."""

import json
import sys

from conftest import Workdir
from ormaos import __version__

OK = "def work():\\n    return 1\\n\\nwork()\\n"
FAILS = """\
import sys

def work():
    try:
        int("x")
    except ValueError:
        pass

work()
sys.exit(3)
"""


def test_list_before_any_recording(work: Workdir) -> None:
    result = work.cli("list")
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout.splitlines() == [
        "no recordings in .ormaos",
        "next: ormaos run -- <command>",
    ]
    doc = json.loads(work.cli("list", "--json").stdout)
    assert doc["kind"] == "list" and doc["version"] == 1
    assert doc["total"] == 0 and doc["recordings"] == []
    assert list(work.path.iterdir()) == []  # it writes nothing


def test_list_shows_the_recordings_newest_first(work: Workdir) -> None:
    work.write("ok.py", OK)
    work.write("fails.py", FAILS)
    work.python("ok.py")
    work.python("fails.py")
    work.python("ok.py")

    doc = json.loads(work.cli("list", "--json").stdout)
    assert doc["total"] == 3 and doc["not_shown"] == 0
    third, second, first = doc["recordings"]
    assert [e["recording"] for e in doc["recordings"]] == ["r3", "r2", "r1"]
    assert first["started"] <= second["started"] <= third["started"]
    assert second == {
        "recording": "r2",
        "started": second["started"],
        "started_text": second["started_text"],
        "mode": "run",
        "command": [sys.executable, "fails.py"],
        "exit_code": 3,
        "signal": None,
        "duration_s": second["duration_s"],
        "complete": True,
        "running": False,
        "processes": 1,
        "calls": 2,
        "exceptions": 2,
        "swallowed": 1,
        "io": 0,
    }
    assert doc["next"] == ["r3", "r2", "r1"]
    assert doc["next_commands"][0] == "ormaos show r3"
    assert doc["next_commands"][-1] == "ormaos diff r2 r3"

    lines = work.cli("list").stdout.splitlines()
    assert lines[0] == "3 recordings in .ormaos, newest first"
    assert [line.split()[0] for line in lines[1:4]] == ["r3", "r2", "r1"]
    assert "  exit 3  " in lines[2] and "(1 swallowed)" in lines[2]
    assert lines[2].endswith("fails.py") and lines[-1].startswith("next: ")
    assert len(lines) == 5

    page = json.loads(
        work.cli("list", "--limit", "1", "--offset", "1", "--json").stdout
    )
    assert [e["recording"] for e in page["recordings"]] == ["r2"]
    assert page["not_shown"] == 1
    assert "ormaos list --offset 2" in page["next_commands"]
    text = work.cli("list", "--limit", "1").stdout.splitlines()
    assert text[0] == "3 recordings in .ormaos, newest first (showing 1)"


def test_status(work: Workdir) -> None:
    result = work.cli("status")
    assert result.returncode == 0 and result.stderr == ""
    version = ".".join(str(n) for n in sys.version_info[:3])
    assert result.stdout.splitlines() == [
        f"ormaos {__version__} on Python {version} ({sys.executable})",
        "this Python can record: yes; starts by itself under `run`: yes",
        "not being recorded now",
        "no recordings in .ormaos",
        "next: ormaos run -- <command> | ormaos guide",
    ]
    assert list(work.path.iterdir()) == []

    work.write("ok.py", OK)
    work.python("ok.py")
    work.write("fails.py", FAILS)
    work.python("fails.py")
    doc = json.loads(work.cli("status", "--json").stdout)
    assert doc["kind"] == "status" and doc["version"] == 1
    assert doc["ormaos"] == __version__ and doc["python"] == version
    assert doc["can_record"] is True and doc["starts_in_child_processes"] is True
    assert doc["recording_now"] is None
    assert doc["recordings"] == 2 and doc["last"]["recording"] == "r2"
    assert doc["last"]["exit_code"] == 3 and doc["last"]["complete"] is True
    assert doc["running"] == [] and doc["incomplete"] == []
    assert doc["next_commands"] == ["ormaos show r2", "ormaos list"]
    lines = work.cli("status").stdout.splitlines()
    assert lines[3].startswith("2 recordings in .ormaos; last: r2, ")
    assert lines[3].endswith(", exit 3")


def test_status_inside_a_recorded_run_says_so(work: Workdir) -> None:
    result = work.python("-m", "ormaos", "status", "--json")
    assert result.returncode == 0
    doc = json.loads(result.stdout)
    assert doc["recording_now"] == "r1"
    assert doc["running"] == ["r1"] and doc["last"]["running"] is True
