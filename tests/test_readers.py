"""The readers on recordings that are damaged, odd, or not ours: an answer or
a plain message, never a traceback and never a hang."""

import json
import sys
from pathlib import Path

import pytest

from conftest import Workdir

APP = """\
def inner(n):
    try:
        int("x")
    except ValueError:
        pass
    return n

def outer():
    with open(__file__) as handle:
        handle.read()
    return [inner(n) for n in range(3)]

outer()
"""
VERBS = [
    ("show", "r1"),
    ("tree", "r1"),
    ("tree", "r1:c3"),
    ("exceptions", "r1"),
    ("exceptions", "r1:e1"),
    ("io", "r1"),
    ("list",),
    ("status",),
]


def record(work: Workdir) -> Path:
    work.write("app.py", APP)
    assert work.python("app.py").returncode == 0
    (file,) = [p for p in (work.path / ".ormaos" / "r1").iterdir() if p.name[0] == "p"]
    return file


def answers(work: Workdir) -> None:
    """Every verb answers, in text and in JSON, without a traceback."""
    for verb in VERBS:
        for flags in ((), ("--json",)):
            result = work.cli(*verb, *flags)  # the fixture stops a hang
            assert "Traceback" not in result.stderr, (verb, result.stderr)
            assert result.returncode in (0, 1), (verb, result.stderr)
            if flags:
                doc = json.loads(result.stdout)
                assert doc["version"] == 1 and "next_commands" in doc


def change(file: Path, kind: str, field: str, value: object, nth: int = 0) -> None:
    lines = file.read_text().splitlines()
    seen = 0
    for index, line in enumerate(lines):
        rec = json.loads(line)
        if rec.get("k") == kind:
            if seen == nth:
                rec[field] = value
                lines[index] = json.dumps(rec)
                break
            seen += 1
    else:
        raise AssertionError(f"no {kind} record")
    file.write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        ("call", "id", None),
        ("call", "id", "x"),
        ("call", "parent", "x"),
        ("call", "args", 5),
        ("call", "t", "soon"),
        ("call", "dur", None),
        ("exc", "at", 5),
        ("exc", "origin", None),
        ("exc", "handled", "here"),
        ("exc", "id", [1]),
        ("io", "n", "x"),
        ("io", "id", None),
        ("io", "line", {}),
    ],
)
def test_a_record_with_a_part_of_the_wrong_kind(
    work: Workdir, kind: str, field: str, value: object
) -> None:
    file = record(work)
    change(file, kind, field, value)
    answers(work)
    # It is left out, and the recording says that a line was not usable.
    doc = json.loads(work.cli("show", "--json").stdout)
    assert any(
        note["what"].endswith(f"lines of {file.name}")
        and "not records in the form this version writes" in note["reason"]
        for note in doc["not_collected"]
    )


def test_a_call_that_is_its_own_parent_does_not_hang(work: Workdir) -> None:
    file = record(work)
    lines = file.read_text().splitlines()
    for index, line in enumerate(lines):
        rec = json.loads(line)
        if rec.get("k") == "call" and rec.get("fn") == "outer":
            rec["parent"] = rec["id"]
            lines[index] = json.dumps(rec)
    file.write_text("\n".join(lines) + "\n")
    answers(work)


def test_a_header_with_parts_of_the_wrong_kind(work: Workdir) -> None:
    file = record(work)
    lines = file.read_text().splitlines()
    header = json.loads(lines[0])
    header["python"]["path_scope"] = 5
    header["core"]["not_collected"] = "none"
    header["core"]["anchor"] = 7
    file.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n")
    answers(work)


def test_a_line_that_the_json_reader_cannot_take(work: Workdir) -> None:
    file = record(work)
    with open(file, "a") as handle:
        handle.write("[" * 100_000 + "\n")
        handle.write('"just a string"\n')
        handle.write("\x00\x01 not json\n")
    answers(work)
    shown = work.cli("show").stdout
    assert f"3 lines of {file.name}" in shown


def test_folders_that_only_look_like_recordings(work: Workdir) -> None:
    record(work)
    root = work.path / ".ormaos"
    (root / "r014").mkdir()  # not a name ormaos gives
    (root / "r0").mkdir()
    (root / "r7").mkdir()  # an empty recording
    (root / "r9").write_text("a file")
    answers(work)
    listed = json.loads(work.cli("list", "--json").stdout)
    assert [entry["recording"] for entry in listed["recordings"]] == ["r7", "r1"]
    # The next recording gets the next number after the real ones.
    assert work.python("app.py").returncode == 0
    assert work.last().number == 8


def test_one_unreadable_recording_does_not_hide_the_others(work: Workdir) -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("root can read everything")
    record(work)
    assert work.python("app.py").returncode == 0
    locked = work.path / ".ormaos" / "r2" / "run.jsonl"
    locked.chmod(0)
    try:
        listed = work.cli("list")
        assert listed.returncode == 0
        assert "r2  cannot be read (PermissionError)" in listed.stdout
        assert any(line.startswith("r1  ") for line in listed.stdout.splitlines())
        doc = json.loads(work.cli("status", "--json").stdout)
        assert doc["unreadable"] == [{"recording": "r2", "reason": "PermissionError"}]
        assert doc["last"]["recording"] == "r1"
        shown = work.cli("show", "r2")
        assert shown.returncode == 1
        assert (
            shown.stderr == "ormaos: cannot read the recording (PermissionError)\n"
            or ("cannot read the recording" in shown.stderr)
        )
    finally:
        locked.chmod(0o644)


def test_an_option_is_never_read_as_the_start_of_another(work: Workdir) -> None:
    # `--json` is not an option of `run`; it used to be taken for `--json-out`.
    result = work.cli("run", "--json", "out", "--", sys.executable, "-c", "print(1)")
    assert result.returncode == 2 and "unrecognized arguments" in result.stdout + (
        result.stderr
    )
    assert not (work.path / "out").exists()
    assert not (work.path / ".ormaos").exists()
    assert work.cli("list", "--lim", "1").returncode == 2


def test_help_of_show_says_it_takes_a_recording(work: Workdir) -> None:
    text = work.cli("show", "--help").stdout
    assert "a recording such as r3" in text and "r3:c42" not in text
    assert "r3:c42" in work.cli("tree", "--help").stdout
