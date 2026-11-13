"""Shape always, value never: no planted value may reach `.ormaos/`."""

import secrets

from conftest import Workdir

NAMES = ("arg", "env", "message", "key", "ret", "child")
# Given on the command line of the recorded program: as an argument, as
# `--name=value`, and as the value after an option.
ARGV = ("argv", "argv_equals", "argv_after")


def make_secrets() -> dict[str, str]:
    """Fake values, made at run time so no secret-looking text is in the repo."""
    names = (*NAMES, *ARGV)
    return {name: f"planted-{name}-{secrets.token_hex(12)}" for name in names}


def program(planted: dict[str, str]) -> str:
    return f"""\
import os, subprocess, sys

def login(user, token, extra):
    try:
        raise PermissionError("{planted["message"]}" + token)
    except PermissionError:
        pass
    return "{planted["ret"]}"

login("ana", "{planted["arg"]}", {{"{planted["key"]}": [1, 2]}})
assert os.environ["PLANTED_VARIABLE"] == "{planted["env"]}"
code = "import sys\\ndef f(a):\\n    return a\\nf(sys.argv[1])"
subprocess.run([sys.executable, "-c", code, "{planted["child"]}"], check=True)
"""


def test_no_planted_value_is_written(work: Workdir) -> None:
    planted = make_secrets()
    work.write("program.py", program(planted))
    result = work.python(
        "program.py",
        planted["argv"],
        f"--password={planted['argv_equals']}",
        "--token",
        planted["argv_after"],
        env={"PLANTED_VARIABLE": planted["env"]},
    )
    assert result.returncode == 0, result.stderr

    recording = work.last()
    assert len(recording.processes) == 2
    assert len(recording.exceptions) == 1
    assert "PLANTED_VARIABLE" in recording.header["env_names"]  # the name, yes

    files = [p for p in (work.path / ".ormaos").rglob("*") if p.is_file()]
    assert len(files) >= 4
    for file in files:
        content = file.read_bytes()
        for name, secret in planted.items():
            assert secret.encode() not in content, f"{name} secret found in {file.name}"
    for secret in planted.values():
        assert secret not in "\n".join(result.summary)
    # The option names are kept; what the readers print has no value either.
    assert recording.header["command"][3:5] == [
        f"--password=<str[{len(planted['argv_equals'])}]>",
        "--token",
    ]
    for verb in ("show", "list"):
        for flags in ((), ("--json",)):
            said = work.cli(verb, *flags).stdout
            for name, secret in planted.items():
                assert secret not in said, f"{name} secret printed by {verb}"
