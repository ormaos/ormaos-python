"""The command line is recorded by name and shape, never by value."""

import json
import secrets
import sys

import pytest

from conftest import Workdir
from ormaos._command import shape_command


@pytest.mark.parametrize(
    ("command", "recorded"),
    [
        (["python", "job.py"], ["python", "job.py"]),
        (
            ["/usr/bin/python3.12", "-u", "job.py", "--token", "abcdef", "prod"],
            ["/usr/bin/python3.12", "-u", "job.py", "--token", "<str[6]>", "<str[4]>"],
        ),
        (
            ["python", "job.py", "--token=abcdef", "-v", "-n=3", "-pSECRET", "--"],
            [
                "python",
                "job.py",
                "--token=<str[6]>",
                "-v",
                "-n=<str[1]>",
                "-p<str[6]>",
                "--",
            ],
        ),
        (
            ["python", "-m", "pkg.tool", "run", "--mode", "x"],
            ["python", "-m", "pkg.tool", "<str[3]>", "--mode", "<str[1]>"],
        ),
        (
            ["python", "-X", "dev", "-c", "print('secret')", "arg"],
            ["python", "-X", "<str[3]>", "-c", "<str[15]>", "<str[3]>"],
        ),
        (
            ["python", "-Wignore", "s.py", "-5"],
            ["python", "-W<str[6]>", "s.py", "<str[2]>"],
        ),
        (
            ["uv", "run", "--quiet", "python", "job.py", "abc"],
            ["uv", "<str[3]>", "--quiet", "python", "job.py", "<str[3]>"],
        ),
        (
            ["pytest", "-q", "tests/test_a.py", "-k", "name and not slow"],
            ["pytest", "-q", "tests/test_a.py", "-k", "<str[17]>"],
        ),
        (["./job.py", "abc"], ["./job.py", "<str[3]>"]),
        (["sh", "-c", "echo secret"], ["sh", "-c", "<str[11]>"]),
        ([], []),
    ],
)
def test_names_are_kept_and_values_become_shapes(
    command: list[str], recorded: list[str]
) -> None:
    assert shape_command(command) == recorded


PROGRAM = """\
import sys

def main(argv):
    return len(argv)

main(sys.argv)
print("ok")
"""


def test_secrets_on_the_command_line_are_in_no_file_and_no_answer(
    work: Workdir,
) -> None:
    planted = {
        name: f"planted-{name}-{secrets.token_hex(10)}"
        for name in ("positional", "equals", "after", "short", "code")
    }
    work.write("program.py", PROGRAM)
    result = work.cli(
        "run",
        "--json-out",
        "out.json",
        "--",
        sys.executable,
        "program.py",
        planted["positional"],
        f"--api-key={planted['equals']}",
        "--token",
        planted["after"],
        f"-p{planted['short']}",
    )
    assert result.returncode == 0 and result.stdout == "ok\n"
    again = work.python("-c", f"x = '{planted['code']}'")
    assert again.returncode == 0

    # What is kept: the executable, the script, the option names.
    doc = json.loads((work.path / "out.json").read_text())
    assert doc["context"]["command"] == [
        sys.executable,
        "program.py",
        f"<str[{len(planted['positional'])}]>",
        f"--api-key=<str[{len(planted['equals'])}]>",
        "--token",
        f"<str[{len(planted['after'])}]>",
        f"-p<str[{len(planted['short'])}]>",
    ]
    assert work.last().header["command"] == [
        sys.executable,
        "-c",
        f"<str[{len(planted['code']) + 6}]>",
    ]

    # No value in any file of the recordings, in the JSON overview, or in
    # what the summary and the readers print.
    files = [p for p in (work.path / ".ormaos").rglob("*") if p.is_file()]
    files.append(work.path / "out.json")
    assert len(files) >= 6
    said = [result.stderr, again.stderr]
    for verb in (("show", "r1"), ("show", "r2"), ("list",), ("status",)):
        said.append(work.cli(*verb).stdout)
        said.append(work.cli(*verb, "--json").stdout)
    said.append(work.cli("diff", "r1", "r2").stdout)
    said.append(work.cli("path", "r1:c2").stdout + work.cli("path", "r1:c2").stderr)
    for name, secret in planted.items():
        for file in files:
            assert secret.encode() not in file.read_bytes(), (name, file.name)
        for text in said:
            assert secret not in text, name
    shown = work.cli("show", "r1").stdout
    assert f"program.py <str[{len(planted['positional'])}]> --api-key=<str[" in shown
