"""`ormaos diff`: two recordings, matched by place in the code and by order."""

import json
import sys
from typing import Any

from conftest import Workdir

BEFORE = """\
import sqlite3

class Rejected(Exception):
    pass

def check(n):
    if n % 3 == 0:
        raise Rejected(f"no {n}")
    return n

def save(conn, n):
    conn.execute("insert into kept values (?)", (n,))

def work(conn, items):
    for n in items:
        try:
            check(n)
            save(conn, n)
        except Rejected:
            continue

conn = sqlite3.connect(":memory:", isolation_level=None)
conn.execute("create table kept (n)")
work(conn, list(range(1, 10)))
"""

# The check no longer raises, and two lines above it moved everything down.
AFTER = BEFORE.replace("import sqlite3\n", "import sqlite3\n\n# moved\n").replace(
    '        raise Rejected(f"no {n}")', "        return 0"
)


def ask(work: Workdir, *args: str) -> dict[str, Any]:
    result = work.cli("diff", *args, "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    doc: dict[str, Any] = json.loads(result.stdout)
    assert doc["kind"] == "diff" and doc["version"] == 1
    return doc


def record(work: Workdir, source: str, *options: str) -> None:
    work.write("app.py", source)
    result = work.cli("run", *options, "--", sys.executable, "app.py")
    assert result.returncode == 0, result.stderr


def test_two_runs_of_the_same_code_have_nothing_to_show(work: Workdir) -> None:
    record(work, BEFORE)
    record(work, BEFORE)

    doc = ask(work, "r1", "r2")

    assert doc["context"] == []
    for name in ("calls", "exceptions", "io"):
        section = doc[name]
        assert section["compared"] and section["places"] > 0
        assert section["differ"] == 0 and section["differences"] == []
    assert doc["not_collected"]["only_a"] == doc["not_collected"]["only_b"] == []
    assert doc["next_commands"] == ["ormaos show r1", "ormaos show r2"]
    text = work.cli("diff", "r1", "r2")
    assert text.stderr == ""
    lines = text.stdout.splitlines()
    assert len(lines) == 8
    assert [ln.split(":")[0] for ln in lines[3:6]] == ["calls", "exceptions", "io"]
    assert all("the same in both" in ln for ln in lines[3:6])
    assert lines[6].startswith("not compared: values, ")


def test_what_one_run_did_and_the_other_did_not(work: Workdir) -> None:
    record(work, BEFORE)
    record(work, AFTER)

    doc = ask(work, "r1", "last")

    calls = {entry["fn"]: entry for entry in doc["calls"]["differences"]}
    assert set(calls) == {"check", "save"}
    check = calls["check"]
    # Matched although the function moved two lines down.
    assert check["b"]["line"] == check["a"]["line"] + 2
    assert (check["a"]["count"], check["b"]["count"]) == (9, 9)
    assert (check["a"]["raised"], check["b"]["raised"]) == (3, 0)
    # In order: the third call is the first that ended in another way.
    other = check["first_other_end"]
    assert other["nth"] == 3
    assert (other["a"]["out"], other["b"]["out"]) == ("raised", "returned")
    assert other["a"]["address"].startswith("r1:c")
    assert other["b"]["address"].startswith("r2:c")
    assert (calls["save"]["a"]["count"], calls["save"]["b"]["count"]) == (6, 9)

    (gone,) = doc["exceptions"]["differences"]
    assert gone["type"].endswith("Rejected") and gone["status"] == "swallowed"
    assert (gone["a"]["count"], gone["b"]["count"]) == (3, 0)
    assert gone["a"]["first"] == "r1:e1" and "first" not in gone["b"]

    (insert,) = doc["io"]["differences"]
    assert insert["sql"] == "insert into kept values (?)"
    assert (insert["a"]["count"], insert["b"]["count"]) == (6, 9)
    assert insert["b"]["line"] == insert["a"]["line"] + 2
    # The statements that did not change are matched across the moved lines.
    assert doc["io"]["places"] == 3

    assert "ormaos exceptions r1:e1" in doc["next_commands"]
    assert set(doc["next"]) >= {"r1:e1", other["a"]["address"], other["b"]["address"]}
    text = "\n".join(work.cli("diff", "r1", "r2").stdout.splitlines())
    assert "6 in r1, 9 in r2" in text and "3 in r1, none in r2" in text
    assert "3 raised in r1, none in r2" in text and "(line " in text
    # Facts only.
    for word in ("bug", "wrong", "fixed", "better", "worse", "regress", "error"):
        assert word not in text.lower()


def test_the_order_of_the_two_recordings_is_kept(work: Workdir) -> None:
    record(work, BEFORE)
    record(work, AFTER)

    doc = ask(work, "r2", "r1")

    assert (doc["a"]["recording"], doc["b"]["recording"]) == ("r2", "r1")
    (appeared,) = doc["exceptions"]["differences"]
    assert (appeared["a"]["count"], appeared["b"]["count"]) == (0, 3)
    assert appeared["b"]["first"] == "r1:e1"


def test_what_one_recording_did_not_record_is_not_compared(work: Workdir) -> None:
    record(work, BEFORE)
    record(work, BEFORE, "--no-io")

    doc = ask(work, "r1", "r2")

    assert doc["io"]["compared"] is False
    assert doc["io"]["reason"] == "io not recorded in r2"
    assert doc["calls"]["differ"] == 0 and doc["exceptions"]["differ"] == 0
    assert [item["what"] for item in doc["context"]] == ["capabilities"]
    text = work.cli("diff", "r1", "r2").stdout
    assert "io: not compared (io not recorded in r2)" in text


def test_another_exit_code_is_part_of_the_answer(work: Workdir) -> None:
    record(work, "print('a')\n")
    work.write("app.py", "import sys\nprint('a')\nsys.exit(3)\n")
    assert work.cli("run", "--", sys.executable, "app.py").returncode == 3

    doc = ask(work, "r1", "r2")

    assert {"what": "exit_code", "a": 0, "b": 3} in doc["context"]
    assert "exit code: 0 in r1, 3 in r2" in work.cli("diff", "r1", "r2").stdout


def test_diff_needs_two_different_recordings(work: Workdir) -> None:
    record(work, BEFORE)
    record(work, BEFORE)

    same = work.cli("diff", "r1", "r1")
    inside = work.cli("diff", "r1:c1", "r2", "--json")
    missing = work.cli("diff", "r1", "r7")

    assert same.returncode == 1 and same.stdout == ""
    assert "r1 was given twice" in same.stderr
    assert inside.returncode == 1
    doc = json.loads(inside.stdout)
    assert doc["kind"] == "error" and doc["next_commands"] == ["ormaos diff r1 r2"]
    assert missing.returncode == 1 and "no recording 'r7'" in missing.stderr
