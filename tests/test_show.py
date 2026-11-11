"""`ormaos show --json` and `ormaos run --json-out`: the overview as JSON."""

import json
import sys

from conftest import Workdir

CODE = """\
def load(key):
    return {}[key]

for key in ("a", "b"):
    try:
        load(key)
    except KeyError:
        pass
print("program output")
raise RuntimeError("stop")
"""


def test_json_out_is_written_and_stdout_is_only_the_programs(work: Workdir) -> None:
    work.write("code.py", CODE)
    plain = work.plain(sys.executable, "code.py")
    result = work.cli("run", "--json-out", "out.json", "--", sys.executable, "code.py")

    assert result.returncode == plain.returncode == 1
    assert result.stdout == plain.stdout == "program output\n"
    assert result.program_stderr == plain.stderr

    doc = json.loads((work.path / "out.json").read_text())
    assert doc["format"] == "provisional-0"
    assert doc["kind"] == "show" and doc["version"] == 1
    assert doc["recording"] == "r1" and doc["path"] == ".ormaos/r1"
    assert doc["context"]["command"] == [sys.executable, "code.py"]
    assert doc["context"]["exit_code"] == 1
    assert "PATH" in doc["context"]["env_names"]
    assert doc["counts"] == {
        "processes": 1,
        "calls": 3,
        "exceptions": 3,
        "swallowed": 2,
        "uncaught": 1,
        "io": 0,
    }
    (process,) = doc["context"]["processes"]
    assert process["complete"] is True and process["calls"] == 3
    (group,) = doc["swallowed"]
    assert group == {
        "type": "KeyError",
        "count": 2,
        "handled_at": {
            "file": "code.py",
            "line": 7,
            "fn": "<module>",
            "user_code": True,
        },
        "raised_at": [
            {"file": "code.py", "line": 2, "fn": "load", "user_code": True, "count": 2}
        ],
        "addresses": ["r1:e1", "r1:e2"],
    }
    assert doc["uncaught"] == [
        {
            "type": "RuntimeError",
            "raised_at": {
                "file": "code.py",
                "line": 10,
                "fn": "<module>",
                "user_code": True,
            },
            "address": "r1:e3",
        }
    ]
    assert doc["next"] == ["r1:e1", "r1:e3", "r1:c1"]
    assert doc["next_commands"] == [
        "ormaos exceptions r1:e1",
        "ormaos exceptions r1:e3",
        "ormaos tree r1:c1",
    ]
    # Everything not recorded is listed, with the reason.
    assert all(note["reason"] for note in doc["not_collected"])
    assert {
        "what": "values",
        "reason": "by design: only type, length and structure are recorded",
        "always": True,
    } in doc["not_collected"]


def test_show_json_last_prints_the_same_document(work: Workdir) -> None:
    work.write("code.py", CODE)
    work.cli("run", "--json-out", "out.json", "--", sys.executable, "code.py")
    shown = work.cli("show", "--json", "last")
    assert shown.returncode == 0 and shown.stderr == ""
    assert json.loads(shown.stdout) == json.loads((work.path / "out.json").read_text())
    assert work.cli("show", "--json").stdout == shown.stdout
    assert work.cli("show", "--json", "r1").stdout == shown.stdout


def test_show_last_is_the_newest_recording(work: Workdir) -> None:
    work.python("-c", "pass")
    work.python("-c", "raise SystemExit(4)")
    doc = json.loads(work.cli("show", "--json", "last").stdout)
    assert doc["recording"] == "r2" and doc["context"]["exit_code"] == 4
    assert json.loads(work.cli("show", "--json", "1").stdout)["recording"] == "r1"


def test_errors_are_json_too(work: Workdir) -> None:
    nothing = work.cli("show", "--json", "last")
    assert nothing.returncode == 1
    assert "no recordings" in json.loads(nothing.stdout)["error"]

    work.python("-c", "pass")
    missing = work.cli("show", "--json", "r7")
    assert missing.returncode == 1
    assert "r7" in json.loads(missing.stdout)["error"]


def test_json_out_that_cannot_be_written_does_not_change_the_exit_code(
    work: Workdir,
) -> None:
    result = work.cli(
        "run",
        "--json-out",
        "no/such/dir/out.json",
        "--",
        sys.executable,
        "-c",
        "print(1)",
    )
    assert result.returncode == 0 and result.stdout == "1\n"
    assert any("could not write" in line for line in result.summary)
