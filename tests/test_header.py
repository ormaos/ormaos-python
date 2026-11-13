"""The header of every file: a universal core and a `python` profile."""

import json
import sys
import time
from typing import Any

from conftest import Workdir
from ormaos.recording import FORMAT

APP = """\
def work():
    return 1

work()
"""


def headers(work: Workdir, name: str = "r1") -> tuple[dict[str, Any], dict[str, Any]]:
    """The first line of `run.jsonl` and of the one process file."""
    folder = work.path / ".ormaos" / name
    (process_file,) = [p for p in folder.iterdir() if p.name.startswith("p")]
    run = json.loads((folder / "run.jsonl").read_text().splitlines()[0])
    process = json.loads(process_file.read_text().splitlines()[0])
    return run, process


def test_the_header_has_a_core_and_a_python_profile(work: Workdir) -> None:
    work.write("app.py", APP)
    before = time.time()
    assert work.python("app.py").returncode == 0
    after = time.time()
    run, process = headers(work)

    assert set(run) == {"format", "kind", "core"}
    assert (run["format"], run["kind"]) == (FORMAT, "run")
    core = run["core"]
    assert core["mode"] == "run" and core["command"] == [sys.executable, "app.py"]
    assert {"ormaos", "pid", "cwd", "env_names", "platform", "anchor"} <= set(core)

    assert set(process) == {"format", "kind", "core", "python"}
    assert (process["format"], process["kind"]) == (FORMAT, "process")
    core = process["core"]
    assert set(core) == {
        "pid",
        "ppid",
        "cwd",
        "anchor",
        "capabilities",
        "not_collected",
    }
    # The capabilities that were recorded.
    assert core["capabilities"] == ["context", "calls", "exceptions", "io"]
    assert core["not_collected"][0]["what"] == "values"
    # Nothing of Python in the core; the profile has it.
    profile = process["python"]
    assert set(profile) == {
        "version",
        "implementation",
        "executable",
        "argv",
        "forked",
        "started_by",
        "path_scope",
    }
    assert profile["version"] == ".".join(str(n) for n in sys.version_info[:3])
    assert profile["executable"] == sys.executable and profile["forked"] is False
    assert profile["started_by"] == "run" and profile["path_scope"] == []

    # The wall-clock anchor: event times are nanoseconds from `t`; the anchor
    # says which moment of the wall clock that was.
    for anchor in (run["core"]["anchor"], core["anchor"]):
        assert set(anchor) == {"t", "unix", "clock"}
        assert anchor["t"] == 0 and before <= anchor["unix"] <= after
    recording = work.last()
    (call,) = [c for c in recording.calls if c["fn"] == "work"]
    at = core["anchor"]["unix"] + call["t"] / 1e9
    assert core["anchor"]["unix"] <= at <= after


def test_the_capabilities_follow_the_options(work: Workdir) -> None:
    work.write("app.py", APP)
    work.cli("run", "--no-io", "--path", "work", "--", sys.executable, "app.py")
    _run, process = headers(work)
    assert process["core"]["capabilities"] == ["context", "calls", "exceptions", "path"]
    assert process["python"]["path_scope"] == ["work"]
    shown = json.loads(work.cli("show", "--json").stdout)
    assert shown["context"]["processes"][0]["capabilities"] == [
        "context",
        "calls",
        "exceptions",
        "path",
    ]


def test_a_header_written_before_the_split_is_still_read(work: Workdir) -> None:
    work.write("app.py", APP)
    assert work.python("app.py").returncode == 0
    folder = work.path / ".ormaos" / "r1"
    for file in folder.iterdir():
        lines = file.read_text().splitlines()
        header = json.loads(lines[0])
        flat = {k: v for k, v in header.items() if k not in ("core", "python")}
        flat.update(header["core"])
        flat["started"] = flat.pop("anchor")["unix"]
        if "python" in header:
            profile = dict(header["python"])
            flat["python"] = profile.pop("version")
            flat.update(profile)
        file.write_text("\n".join([json.dumps(flat), *lines[1:]]) + "\n")

    doc = json.loads(work.cli("show", "--json").stdout)
    assert doc["complete"] is True and doc["counts"]["calls"] == 2
    assert doc["context"]["command"] == [sys.executable, "app.py"]
    process = doc["context"]["processes"][0]
    assert process["python"] == ".".join(str(n) for n in sys.version_info[:3])
    assert isinstance(doc["context"]["started"], float)
