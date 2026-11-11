"""Fail open: an internal error in Ormaos never changes the observed program."""

import json
import sys

import pytest

from conftest import Workdir

PROGRAM = """\
import sys

def work(n, label):
    try:
        return 10 // n
    except ZeroDivisionError:
        return -1

print("results", [work(n, "x") for n in (1, 0, 5)])
print("warning", file=sys.stderr)
raise SystemExit(3)
"""


@pytest.mark.parametrize("fault", ["calls", "exceptions", "write"])
def test_internal_error_in_a_callback_changes_nothing(
    work: Workdir, fault: str
) -> None:
    work.write("program.py", PROGRAM)
    plain = work.plain(sys.executable, "program.py")
    assert plain.returncode == 3

    result = work.python("program.py", env={"ORMAOS_FAULT": fault})

    assert result.returncode == plain.returncode
    assert result.stdout == plain.stdout
    assert result.program_stderr == plain.stderr

    # Recording stopped, and the reason is in `not_collected`.
    recording = work.last()
    reasons = [note["reason"] for note in recording.not_collected]
    assert any(
        "internal error in ormaos (RuntimeError at _recorder.py:" in r for r in reasons
    ), reasons
    (process,) = recording.processes
    assert process.end is not None and process.end["stopped_early"] is True
    assert any(line.startswith("ormaos: not collected:") for line in result.summary)


def test_recording_directory_that_is_gone(work: Workdir) -> None:
    result = work.plain(
        sys.executable,
        "-c",
        "print('still fine')",
        env={"ORMAOS_DIR": str(work.path / "missing")},
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "still fine\n", "")


def test_unreadable_recording_does_not_change_the_exit_code(work: Workdir) -> None:
    # The program damages its own recording: `ormaos run` still returns its code.
    code = (
        "import os, glob\n"
        "for f in glob.glob(os.path.join(os.environ['ORMAOS_DIR'], '*')):\n"
        "    open(f, 'w').write('not json\\n')\n"
        "raise SystemExit(5)\n"
    )
    result = work.python("-c", code)
    assert result.returncode == 5


def test_broken_lines_are_reported_not_fatal(work: Workdir) -> None:
    work.python("-c", "def f():\n    pass\nf()\n")
    (file,) = (work.path / ".ormaos" / "r1").glob("p*.jsonl")
    with file.open("a") as handle:
        handle.write('{"k":"call","id":99,\n')
    recording = work.last()
    assert any("not valid JSON" in n["reason"] for n in recording.not_collected)
    assert json.loads(file.read_text().splitlines()[0])["kind"] == "process"
