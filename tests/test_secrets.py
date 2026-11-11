"""Shape always, value never: no planted value may reach `.ormaos/`."""

import sys

from conftest import Workdir

SECRETS = {
    "arg": "sk-live-PLANTED-ARGUMENT-0001",
    "env": "PLANTED-ENV-VALUE-0002",
    "message": "PLANTED-EXCEPTION-MESSAGE-0003",
    "key": "PLANTED-DICT-KEY-0004",
    "ret": "PLANTED-RETURN-VALUE-0005",
    "child": "PLANTED-CHILD-ARGUMENT-0006",
}

PROGRAM = f"""\
import os, subprocess, sys

def login(user, token, extra):
    try:
        raise PermissionError("{SECRETS["message"]}" + token)
    except PermissionError:
        pass
    return "{SECRETS["ret"]}"

login("ana", "{SECRETS["arg"]}", {{"{SECRETS["key"]}": [1, 2]}})
assert os.environ["PLANTED_VARIABLE"] == "{SECRETS["env"]}"
subprocess.run(
    [sys.executable, "-c", "import sys\\ndef f(a):\\n    return a\\nf(sys.argv[1])",
     "{SECRETS["child"]}"],
    check=True,
)
"""


def test_no_planted_value_is_written(work: Workdir) -> None:
    work.write("program.py", PROGRAM)
    result = work.python("program.py", env={"PLANTED_VARIABLE": SECRETS["env"]})
    assert result.returncode == 0, result.stderr

    recording = work.last()
    assert len(recording.processes) == 2
    assert len(recording.exceptions) == 1
    assert "PLANTED_VARIABLE" in recording.header["env_names"]  # the name, yes

    files = [p for p in (work.path / ".ormaos").rglob("*") if p.is_file()]
    assert len(files) >= 4
    for file in files:
        content = file.read_bytes()
        for name, secret in SECRETS.items():
            assert secret.encode() not in content, f"{name} secret found in {file.name}"
    for secret in SECRETS.values():
        assert secret not in "\n".join(result.summary)
    assert sys.executable  # keep the import used
