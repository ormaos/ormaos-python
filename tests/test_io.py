"""I/O recording: sqlite3, files, subprocesses, HTTP, and the verb `io`."""

import json
import os
import secrets
import sys
from collections.abc import Callable
from typing import Any

import pytest

from conftest import Workdir
from ormaos._iorec import sql_shape

SERVER = """\
import http.server, threading

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(404 if self.path.startswith("/missing") else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *args):
        pass

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"
"""

PROGRAM = """\
import csv, json, os, pathlib, sqlite3, subprocess, sys

def database(secret):
    conn = sqlite3.connect("shop.db")  # connect
    conn.execute("CREATE TABLE item (name TEXT, price INT)")  # create
    with conn:
        conn.execute("INSERT INTO item VALUES (?, ?)", (secret, 42))  # insert
        rows = [("a", 1), ("b", 2), ("c", 3)]
        conn.executemany("INSERT INTO item VALUES (?, ?)", rows)  # many
    query = f"SELECT * FROM item WHERE name = '{secret}' AND price > 41"
    conn.execute(query).fetchall()  # select
    conn.close()

def files(secret):
    with open("notes.txt", "w") as handle:  # open-w
        handle.write(secret)  # write
        json.dump({"key": secret}, handle)  # dump
    with open("notes.txt") as handle:  # open-r
        handle.read()  # read
    with open("notes.txt") as handle:  # open-iter
        list(csv.reader(handle))
    pathlib.Path("notes.txt").read_text()  # pathlib

def processes(secret):
    subprocess.run([sys.executable, "-c", "pass", secret], check=True)  # spawn
    os.system("true " + secret)  # system

SECRET = os.environ["PLANTED"]
database(SECRET)
files(SECRET)
processes(SECRET)
print("done")
"""


def recorded(work: Workdir, source: str, *options: str, **env: str) -> Any:
    work.write("program.py", source)
    result = work.cli("run", *options, "--", sys.executable, "program.py", env=env)
    assert result.returncode == 0, result.stderr
    return result


def ask(work: Workdir, *args: str) -> dict[str, Any]:
    answer = work.cli(*args, "--json")
    assert answer.returncode == 0 and answer.stderr == "", answer.stdout
    doc: dict[str, Any] = json.loads(answer.stdout)
    assert doc["version"] == 1
    return doc


def groups(work: Workdir, *args: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = ask(work, "io", *args, "--limit", "100")["groups"]
    return found


def no_secret_in_the_recording(work: Workdir, *values: str) -> None:
    files = [p for p in (work.path / ".ormaos").rglob("*") if p.is_file()]
    assert len(files) >= 3
    for file in files:
        content = file.read_bytes()
        for value in values:
            assert value.encode() not in content, f"{value!r} found in {file.name}"


def test_sqlite_files_and_subprocesses_are_recorded_with_their_line(
    work: Workdir, line_of: Callable[[str, str], int]
) -> None:
    planted = f"planted-{secrets.token_hex(8)}"
    result = recorded(work, PROGRAM, PLANTED=planted)
    assert result.stdout == "done\n"
    assert result.summary[0].endswith(", 23 io")

    found = groups(work, "r1")
    seen = [
        (g["io_kind"], g["op"], g.get("target"), g["count"], g["at"]["line"])
        for g in found
    ]
    line = {mark: line_of(PROGRAM, mark) for mark in (
        "connect", "create", "insert", "many", "select", "open-w", "write", "dump",
        "open-r", "read", "open-iter", "pathlib", "spawn", "system",
    )}  # fmt: skip
    assert seen == [
        ("sqlite", "connect", "shop.db", 1, line["connect"]),
        ("sqlite", "CREATE", "shop.db", 1, line["create"]),
        ("sqlite", "BEGIN", "shop.db", 1, line["insert"]),
        ("sqlite", "INSERT", "shop.db", 1, line["insert"]),
        ("sqlite", "INSERT", "shop.db", 3, line["many"]),
        ("sqlite", "COMMIT", "shop.db", 1, line["insert"] - 1),
        ("sqlite", "SELECT", "shop.db", 1, line["select"]),
        ("file", "open", "notes.txt", 1, line["open-w"]),
        ("file", "write", "notes.txt", 1, line["write"]),
        ("file", "write", "notes.txt", 5, line["dump"]),
        ("file", "open", "notes.txt", 1, line["open-r"]),
        ("file", "read", "notes.txt", 1, line["read"]),
        ("file", "open", "notes.txt", 1, line["open-iter"]),
        ("file", "open", "notes.txt", 1, line["pathlib"]),
        ("file", "read", "notes.txt", 1, line["pathlib"]),
        ("subprocess", "spawn", os.path.basename(sys.executable), 1, line["spawn"]),
        ("subprocess", "system", "shell", 1, line["system"]),
    ]  # replaced below by exact checks when the program name differs
    by_line = {(g["io_kind"], g["at"]["line"], g["op"]): g for g in found}
    select = by_line[("sqlite", line["select"], "SELECT")]
    assert select["sql"] == "SELECT * FROM item WHERE name = ? AND price > ?"
    assert select["table"] == "item" and select["at"]["fn"] == "database"
    assert by_line[("sqlite", line["many"], "INSERT")]["sql"] == (
        "INSERT INTO item VALUES (?, ?)"
    )
    assert by_line[("file", line["open-w"], "open")]["mode"] == "write"
    assert by_line[("file", line["open-r"], "open")]["mode"] == "read"

    no_secret_in_the_recording(work, planted)
    for args in (("io", "r1"), ("show",), ("tree", "r1")):
        assert planted not in work.cli(*args, "--json", "--limit", "100").stdout


def test_io_of_a_call_and_of_one_event(work: Workdir) -> None:
    recorded(work, PROGRAM, PLANTED="x")
    top = ask(work, "tree", "r1")
    database = next(n for n in top["nodes"] if n["fn"] == "database")
    assert database["io"] == 9

    doc = ask(work, "io", database["address"])
    assert doc["kind"] == "io" and doc["scope"]["fn"] == "database"
    assert doc["total"] == 9 and doc["by_kind"] == {"sqlite": 9}
    assert doc["io_in_the_call_itself"] == 9

    whole = ask(work, "io", "r1")
    assert whole["by_kind"] == {"sqlite": 9, "file": 12, "subprocess": 2}
    assert whole["scope"] is None and whole["next"][0] == whole["groups"][0]["first"]
    below = ask(work, "io", "r1:c1")["calls_directly_below"]
    assert [(b["fn"], b["io"]) for b in below] == [
        ("database", 9),
        ("files", 12),
        ("processes", 2),
    ]

    only = ask(work, "io", "r1", "--kind", "subprocess")
    assert [g["op"] for g in only["groups"]] == ["spawn", "system"]
    page = ask(work, "io", "r1", "--limit", "2", "--offset", "1")
    assert len(page["groups"]) == 2 and page["not_shown"] > 0

    many = next(g for g in whole["groups"] if g["count"] == 3)
    event = ask(work, "io", many["first"])
    assert event["kind"] == "io_event" and event["io_kind"] == "sqlite"
    assert event["count"] == 3 and event["call"]["address"] == database["address"]
    assert f"ormaos io {database['address']}" in event["next_commands"]

    text = work.cli("io", "r1").stdout.splitlines()
    assert text[0] == "r1: 23 io events (sqlite 9, file 12, subprocess 2)"
    assert "  3x sqlite INSERT INTO item VALUES (?, ?)  at program.py:" in text[5]
    assert text[-1].startswith("next: ormaos io r1:i1 | ")
    one = work.cli("io", many["first"]).stdout
    assert "3 times in a row, at the same line" in one
    assert "parameters  not recorded; literals are shown as ?" in one


def test_the_program_behaves_the_same_with_io_recording(work: Workdir) -> None:
    source = (
        "import sqlite3, sys\n"
        "seen = []\n"
        "conn = sqlite3.connect(':memory:')\n"
        "print(type(conn) is sqlite3.Connection, repr(type(conn)))\n"
        "conn.set_trace_callback(seen.append)\n"
        "conn.execute('create table t (a)')\n"
        "print(seen, open is __builtins__.open, repr(open))\n"
        "with open(__file__) as handle:\n"
        "    print(type(handle).__name__, len(handle.read()))\n"
        "print('warning', file=sys.stderr)\n"
        "conn.execute('select * from missing_table')\n"
    )
    work.write("program.py", source)
    plain = work.plain(sys.executable, "program.py")
    result = work.python("program.py")
    assert plain.returncode == 1 and "no such table" in plain.stderr
    assert result.returncode == plain.returncode
    assert result.stdout == plain.stdout
    assert result.program_stderr == plain.stderr
    # The program set its own trace callback: its statements were its own.
    assert "['create table t (a)']" in result.stdout
    ops = [g["op"] for g in groups(work, "r1")]
    assert ops == ["connect", "open", "read"]


def test_io_can_be_turned_off(work: Workdir) -> None:
    recorded(work, PROGRAM, "--no-io", PLANTED="x")
    doc = ask(work, "io", "r1")
    assert doc["total"] == 0 and doc["note"] == "I/O was not recorded in this recording"
    notes = ask(work, "show")["not_collected"]
    assert {
        "what": "io",
        "reason": "I/O recording was turned off (--no-io)",
        "always": True,
    } in notes


def test_what_io_does_not_record_is_listed(work: Workdir) -> None:
    recorded(work, PROGRAM, PLANTED="x")
    notes = {n["what"]: n["reason"] for n in ask(work, "show")["not_collected"]}
    assert "set_trace_callback" not in notes["io: contents"]
    assert (
        "sets the trace callback"
        in (notes["io: sqlite3 statements, if the program sets a trace callback"])
    )
    assert (
        "csv.reader"
        in notes["io: reads and writes of a file that are not a method call"]
    )
    assert "io: results" in notes and "io: other kinds" in notes


def test_an_internal_error_in_io_recording_changes_nothing(work: Workdir) -> None:
    work.write("program.py", PROGRAM)
    plain = work.plain(sys.executable, "program.py", env={"PLANTED": "x"})
    for name in ("shop.db", "notes.txt"):
        (work.path / name).unlink()
    result = work.python("program.py", env={"PLANTED": "x", "ORMAOS_FAULT": "io"})
    assert (result.returncode, result.stdout) == (plain.returncode, plain.stdout)
    assert result.program_stderr == plain.stderr
    reasons = [note["reason"] for note in work.last().not_collected]
    assert any("internal error in ormaos (RuntimeError at" in r for r in reasons)


def test_the_limit_of_io_events_is_reported(work: Workdir) -> None:
    source = (
        "for n in range(50):\n"
        "    open(__file__).close()\n"
        "    open('other.txt', 'a').close()\n"
    )
    work.write("program.py", source)
    work.python("program.py", env={"ORMAOS_MAX_IO": "10"})
    recording = work.last()
    assert len(recording.io) == 10
    assert any("limit of 10 io events" in n["reason"] for n in recording.not_collected)


HTTP = (
    SERVER
    + """\
import os, urllib.request, urllib.error

SECRET = os.environ["PLANTED"]
KEY = {"X-Key": SECRET}
QUERY = "?token=" + SECRET

def with_urllib():
    request = urllib.request.Request(
        BASE + "/orders/7" + QUERY, data=SECRET.encode(), headers=KEY
    )
    urllib.request.urlopen(request).read()  # urllib
    try:
        urllib.request.urlopen(BASE + "/missing")  # urllib-404
    except urllib.error.HTTPError:
        pass

def with_requests():
    import requests
    requests.get(BASE + "/r", params={"token": SECRET}, headers=KEY)  # requests

def with_httpx():
    import asyncio, httpx
    httpx.post(BASE + "/h" + QUERY, content=SECRET, headers=KEY)  # httpx
    async def later():
        async with httpx.AsyncClient() as client:
            await client.get(BASE + "/missing/async")  # httpx-async
    asyncio.run(later())

for name in sys.argv[1:]:
    globals()["with_" + name]()
server.shutdown()
"""
)


def http_events(work: Workdir, *libraries: str) -> list[dict[str, Any]]:
    planted = f"planted-{secrets.token_hex(8)}"
    work.write("program.py", "import sys\n" + HTTP)
    command = [sys.executable, "program.py", *libraries]
    result = work.cli("run", "--", *command, env={"PLANTED": planted})
    assert result.returncode == 0, result.stderr
    no_secret_in_the_recording(work, planted)
    found = groups(work, "r1", "--kind", "http")
    assert planted not in json.dumps(found)
    assert all(g["scheme"] == "http" and g["host"] == "127.0.0.1" for g in found)
    return found


def test_http_with_urllib(work: Workdir, line_of: Callable[[str, str], int]) -> None:
    first, second = http_events(work, "urllib")
    assert (first["op"], first["target"], first["status"]) == ("POST", "/orders/7", 200)
    assert first["query_string"] == "not recorded"
    assert first["at"]["fn"] == "with_urllib"
    assert first["at"]["line"] == line_of(HTTP, "urllib") + 1
    assert (second["op"], second["target"], second["status"]) == (
        "GET",
        "/missing",
        404,
    )
    assert "query_string" not in second
    text = work.cli("io", "r1", "--kind", "http").stdout
    assert "/orders/7?(not recorded) -> 200  at program.py:" in text


def test_http_with_requests(work: Workdir, line_of: Callable[[str, str], int]) -> None:
    pytest.importorskip("requests")
    (event,) = http_events(work, "requests")
    assert (event["op"], event["target"], event["status"]) == ("GET", "/r", 200)
    assert event["query_string"] == "not recorded"
    assert event["at"]["line"] == line_of(HTTP, "requests") + 1


def test_http_with_httpx(work: Workdir, line_of: Callable[[str, str], int]) -> None:
    pytest.importorskip("httpx")
    first, second = http_events(work, "httpx")
    assert (first["op"], first["target"], first["status"]) == ("POST", "/h", 200)
    assert first["query_string"] == "not recorded"
    assert first["at"]["line"] == line_of(HTTP, "httpx") + 1
    assert (second["op"], second["target"]) == ("GET", "/missing/async")
    assert second["status"] == 404 and second["at"]["fn"].endswith("later")


def test_ormaos_itself_imports_no_http_library(work: Workdir) -> None:
    source = (
        "import sys, sqlite3, urllib.request\n"
        "open(__file__).close()\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in "
        "('requests', 'httpx', 'urllib3')))\n"
    )
    work.write("program.py", source)
    assert work.python("program.py").stdout == "[]\n"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("select 1", ("SELECT", None, "select ?")),
        (
            "SELECT a, t1.b FROM t1 WHERE a = 'it''s' AND b = 4.5e+3 AND c = x'ff'",
            ("SELECT", "t1", "SELECT a, t1.b FROM t1 WHERE a = ? AND b = ? AND c = ?"),
        ),
        (
            "INSERT OR REPLACE INTO stock (sku) VALUES ('KBD-0101') -- note 'x'",
            ("INSERT", "stock", "INSERT OR REPLACE INTO stock (sku) VALUES (?)"),
        ),
        (
            "/* who */ UPDATE  stock\n SET n = n - 2 WHERE sku = :sku AND k = ?1",
            (
                "UPDATE",
                "stock",
                "UPDATE stock SET n = n - ? WHERE sku = :sku AND k = ?1",
            ),
        ),
        (
            'SELECT "app_item"."id" FROM "app_item" WHERE "app_item"."name" = "bob"',
            (
                "SELECT",
                "app_item",
                'SELECT "app_item"."id" FROM "app_item" WHERE "app_item"."name" = ?',
            ),
        ),
        (
            "DELETE FROM [my table] WHERE a = 'unclosed",
            ("DELETE", None, "DELETE FROM ? WHERE a = ?"),
        ),
        (
            "CREATE TABLE IF NOT EXISTS t2 (a)",
            ("CREATE", "t2", "CREATE TABLE IF NOT EXISTS t2 (a)"),
        ),
        ("COMMIT", ("COMMIT", None, "COMMIT")),
        ("", ("?", None, "")),
    ],
)
def test_the_shape_of_a_statement(
    sql: str, expected: tuple[str, str | None, str]
) -> None:
    assert sql_shape(sql) == expected


def test_a_long_statement_is_cut() -> None:
    verb, table, text = sql_shape(
        "SELECT " + ", ".join(f"col{n}" for n in range(200)) + " FROM wide"
    )
    assert (verb, table) == ("SELECT", "wide")
    assert len(text) == 200 and text.endswith("...")
