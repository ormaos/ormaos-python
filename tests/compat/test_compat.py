"""The compatibility matrix: every row of `matrix.ROWS` is run here.

For each row: the example behaves the same with and without Ormaos, the
recording has what the row says, the limits of the row are in `not_collected`
of that recording, and there is no other note there.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from compat.matrix import ROWS, Row, table
from conftest import Result, clean_env

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"
DOC = HERE.parent.parent / "docs" / "compatibility.md"
BY_NAME = {row.name: row for row in ROWS}
ORMAOS = [sys.executable, "-m", "ormaos"]
TOOK = re.compile(r" in \d+\.\d+s")


def row(name: str) -> Row:
    found = BY_NAME[name]
    for module in found.needs:
        if importlib.util.find_spec(module) is None:
            message = f"{module} is not installed (uv sync --group compat)"
            if os.environ.get("ORMAOS_COMPAT_REQUIRED"):
                pytest.fail(message)
            pytest.skip(message)
    return found


@pytest.fixture
def case(tmp_path: Path) -> Path:
    target = tmp_path / "case"
    shutil.copytree(CASES, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def run(cwd: Path, argv: list[str], env: dict[str, str] | None = None) -> Result:
    extra = {"PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    done = subprocess.run(
        argv,
        cwd=cwd,
        env=clean_env(extra),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    return Result(done.returncode, done.stdout, done.stderr)


def recorded(
    cwd: Path, command: list[str], env: dict[str, str] | None = None
) -> Result:
    return run(cwd, [*ORMAOS, "run", "--", *command], env)


def ask(cwd: Path, *args: str) -> dict[str, Any]:
    answer = run(cwd, [*ORMAOS, *args, "--json"])
    assert answer.returncode == 0, answer.stdout + answer.stderr
    doc: dict[str, Any] = json.loads(answer.stdout)
    return doc


def functions(cwd: Path, recording: str = "last") -> list[str]:
    """The function of every recorded call, in order."""
    tree = ask(cwd, "tree", recording, "--depth", "50", "--limit", "500")
    assert tree["calls_below_root"] <= 500
    return [node["fn"] for node in tree["nodes"]]


def check(found: Row, cwd: Path, recording: str = "last") -> dict[str, Any]:
    """The recording is whole, and its own `not_collected` notes are exactly
    the limits of the row. Returns the overview."""
    doc = ask(cwd, "show", recording)
    assert doc["complete"] is True, doc["incomplete"]
    notes = [n["what"] for n in doc["not_collected"] if not n.get("always")]
    for limit in found.limits:
        assert any(note.startswith(limit.note) for note in notes), (limit.note, notes)
    for note in notes:
        assert any(note.startswith(limit.note) for limit in found.limits), note
    assert (found.outcome == "works") == (notes == [])
    return doc


def swallowed(doc: dict[str, Any]) -> list[tuple[str, int, str, str]]:
    """(type, count, file of the handler, function of the handler)."""
    return [
        (
            group["type"].rsplit(".", 1)[-1],
            group["count"],
            os.path.basename(group["handled_at"]["file"]),
            group["handled_at"]["fn"],
        )
        for group in doc["swallowed"]
    ]


def test_pytest(case: Path) -> None:
    found = row("pytest")
    project = case / "pytest_project"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    plain = run(project, command)
    result = recorded(project, command)

    assert result.returncode == plain.returncode == 0
    assert TOOK.sub("", result.stdout) == TOOK.sub("", plain.stdout)
    assert "4 passed" in result.stdout
    assert result.program_stderr == plain.stderr
    doc = check(found, project)
    calls = functions(project)
    for name in ("prices", "test_total_skips_bad_prices", "total", "test_raises"):
        assert calls.count(name) == 1, name
    assert calls.count("test_parse_price") == 2 and calls.count("parse_price") == 5
    assert swallowed(doc) == [
        ("ValueError", 2, "pricing.py", "parse_price"),
        ("ZeroDivisionError", 1, "test_pricing.py", "test_raises"),
    ]


def test_flask(case: Path) -> None:
    found = row("Flask")
    command = [sys.executable, "flask_app.py"]
    plain = run(case, command)
    result = recorded(case, command)

    assert result.returncode == plain.returncode == 0
    assert result.stdout == plain.stdout and "200 {'on_hand': 0" in result.stdout
    assert result.program_stderr == plain.stderr
    doc = check(found, case)
    assert functions(case) == ["<module>", "stock", "lookup", "stock", "lookup"]
    assert swallowed(doc) == [("KeyError", 1, "flask_app.py", "lookup")]


def test_fastapi(case: Path) -> None:
    found = row("FastAPI")
    command = [sys.executable, "fastapi_app.py"]
    plain = run(case, command)
    result = recorded(case, command)

    assert result.returncode == plain.returncode == 0
    assert result.stdout == plain.stdout and result.stdout.count("200 ") == 3
    assert result.program_stderr == plain.stderr
    doc = check(found, case)
    calls = functions(case)
    assert calls.count("stock") == 2 and calls.count("slow") == 1
    assert calls.count("lookup") == 3
    assert swallowed(doc) == [("KeyError", 2, "fastapi_app.py", "lookup")]
    # A sync route runs in a worker thread; an async one in a task of the loop.
    tree = ask(case, "tree", "last", "--depth", "50", "--limit", "500")
    routes = {node["fn"]: node for node in tree["nodes"] if node["fn"] != "lookup"}
    assert routes["stock"]["thread"] != routes["<module>"]["thread"]
    assert isinstance(routes["slow"].get("task"), int)


def test_django(case: Path) -> None:
    found = row("Django")
    command = [sys.executable, "django_app.py"]
    plain = run(case, command)
    result = recorded(case, command)

    assert result.returncode == plain.returncode == 0
    assert result.stdout == plain.stdout
    assert result.stdout.splitlines()[1] == "200 {'name': 'nobody', 'id': None}"
    assert result.program_stderr == plain.stderr
    doc = check(found, case)
    assert functions(case) == ["<module>", "user", "find", "user", "find"]
    assert swallowed(doc) == [("DoesNotExist", 1, "django_app.py", "find")]
    # The SQL of the ORM, by the line of this file that caused it.
    io = ask(case, "io", "last", "--kind", "sqlite", "--limit", "200")
    selects = [
        group
        for group in io["groups"]
        if group["op"] == "SELECT" and group.get("table") == "auth_user"
    ]
    assert selects and {group["at"]["fn"] for group in selects} == {"find"}
    assert all(group["at"]["file"] == "django_app.py" for group in selects)
    assert sum(group["count"] for group in selects) == 2


def test_click(case: Path) -> None:
    found = row("Click CLI")
    command = [sys.executable, "click_cli.py", "add", "1", "x", "3", "--label", "sum"]
    plain = run(case, command)
    result = recorded(case, command)

    assert result.returncode == plain.returncode == 0
    assert result.stdout == plain.stdout == "sum: 4\n"
    assert result.program_stderr == plain.stderr
    doc = check(found, case)
    calls = functions(case)
    assert calls[0] == "<module>" and calls.count("add") == 1
    assert calls.count("cli") == 1 and calls.count("parse") == 3
    assert swallowed(doc) == [("ValueError", 1, "click_cli.py", "parse")]
    # Click ends the program with `sys.exit(0)`: said as an exit, not a failure.
    listed = ask(case, "exceptions", "last")["exceptions"]
    assert [e["status"] for e in listed if e["type"] == "SystemExit"] == ["exit"]


def test_pandas(case: Path) -> None:
    found = row("pandas")
    (case / "in.csv").write_text("sku,amount\nA,1.5\nB,x\nA,2\n")
    plain = run(case, [sys.executable, "pandas_job.py", "in.csv", "plain.csv"])
    result = recorded(case, [sys.executable, "pandas_job.py", "in.csv", "out.csv"])

    assert result.returncode == plain.returncode == 0
    assert result.stdout == plain.stdout and "3.5" in result.stdout
    assert result.program_stderr == plain.stderr
    assert (case / "out.csv").read_text() == (case / "plain.csv").read_text()
    doc = check(found, case)
    # pandas calls `to_number` from inside `Series.apply`: recorded below `main`.
    assert functions(case) == ["<module>", "main", *["to_number"] * 3]
    assert swallowed(doc) == [("ValueError", 1, "pandas_job.py", "to_number")]
    io = ask(case, "io", "last", "--kind", "file")
    targets = {(group["op"], group.get("target")) for group in io["groups"]}
    assert ("open", "in.csv") in targets and ("open", "out.csv") in targets


def test_record_in_a_script(case: Path) -> None:
    found = row("`with ormaos.record()` in a script")
    result = run(case, [sys.executable, "record_script.py"])

    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout == "total 4\ndone\n"
    doc = check(found, case)
    assert doc["context"]["mode"] == "record"
    # `setup` ran before the block: not in the recording.
    assert functions(case) == ["<genexpr>", "parse", "parse", "parse"]
    assert swallowed(doc) == [("ValueError", 1, "record_script.py", "parse")]


def test_jupyter_notebook(case: Path, tmp_path: Path) -> None:
    found = row("Jupyter notebook")
    env = {
        "JUPYTER_RUNTIME_DIR": str(tmp_path / "runtime"),
        "IPYTHONDIR": str(tmp_path / "ipython"),
    }
    result = run(case, [sys.executable, "run_notebook.py", "notebook.ipynb"], env)

    # The cells ran in a real kernel, and printed what they print.
    assert result.returncode == 0, result.stderr
    assert result.stdout == "total 4\nrecorded r1\nafter 7\n"
    doc = check(found, case)
    assert doc["context"]["mode"] == "record"
    assert doc["context"]["command"][1] == "ipykernel_launcher"
    # The functions of an earlier cell, called from the block; the call in
    # the cell after the block is not there.
    assert functions(case) == ["<genexpr>", "parse", "parse", "parse"]
    (group,) = doc["swallowed"]
    assert (group["type"], group["count"]) == ("ValueError", 1)
    cell = group["handled_at"]["file"]
    assert re.fullmatch(r".*/ipykernel_\d+/\d+\.py", cell), cell
    assert group["handled_at"]["fn"] == "parse"


@pytest.mark.parametrize("core", ["ctrace", "sysmon"])
def test_coverage_at_the_same_time(case: Path, tmp_path: Path, core: str) -> None:
    found = row("coverage.py at the same time")
    command = [sys.executable, "-m", "coverage", "run", "coverage_script.py"]
    report = [sys.executable, "-m", "coverage", "report", "-m"]

    def env(name: str) -> dict[str, str]:
        return {"COVERAGE_CORE": core, "COVERAGE_FILE": str(tmp_path / name)}

    plain = run(case, command, env("plain"))
    plain_report = run(case, report, env("plain"))
    result = recorded(case, command, env("recorded"))
    result_report = run(case, report, env("recorded"))

    assert result.returncode == plain.returncode == 0
    assert result.stdout == plain.stdout == "total 4\n"
    assert result.program_stderr == plain.stderr
    # coverage measured the same lines while Ormaos was recording.
    assert plain_report.returncode == result_report.returncode == 0
    assert result_report.stdout == plain_report.stdout
    # 8 statements, 1 missed: the body of `never_called`, line 12.
    assert "8      1    88%   12" in result_report.stdout
    doc = check(found, case)
    assert functions(case) == ["<module>", "<genexpr>", "parse", "parse", "parse"]
    assert swallowed(doc) == [("ValueError", 1, "coverage_script.py", "parse")]


def test_every_row_has_a_test_and_the_table_is_current() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    for found in ROWS:
        assert f'row("{found.name}")' in source, found.name
        assert (CASES / found.example).exists(), found.example
        assert found.outcome in ("works", "works, with these limits")
    assert DOC.read_text(encoding="utf-8") == table(), (
        "docs/compatibility.md is out of date: "
        "uv run python tests/compat/matrix.py > docs/compatibility.md"
    )
