"""End to end on the demo app `order-sync` from the ormaos-examples repository.

The demo is looked for next to this repository (`../ormaos-examples`), or at
`ORMAOS_DEMO_DIR`. Without it these tests are skipped, unless
`ORMAOS_DEMO_REQUIRED` is set (CI sets it). `ORMAOS_DEMO_PYTHON` is the Python
that runs the job; it must have ormaos installed.
"""

import csv
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from conftest import Result, clean_env
from ormaos.recording import Recording, load

REPO = Path(__file__).resolve().parent.parent
DEMO_DIR = Path(
    os.environ.get("ORMAOS_DEMO_DIR") or REPO.parent / "ormaos-examples" / "order-sync"
)
DEMO_PYTHON = os.environ.get("ORMAOS_DEMO_PYTHON") or sys.executable
SKU_FORMAT = re.compile(r"[A-Z]{3}-[0-9]{4}")


@dataclass
class Demo:
    path: Path

    def _run(self, argv: list[str], env: dict[str, str] | None = None) -> Result:
        done = subprocess.run(
            argv,
            cwd=self.path,
            env=clean_env(env),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        return Result(done.returncode, done.stdout, done.stderr)

    def reset(self) -> None:
        done = self._run(["sh", "reset.sh"], {"PYTHON": DEMO_PYTHON})
        assert done.returncode == 0, done.stderr

    def plain(self, *args: str, env: dict[str, str] | None = None) -> Result:
        return self._run([DEMO_PYTHON, "job.py", *args], env)

    def recorded(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        options: tuple[str, ...] = (),
    ) -> Result:
        ormaos = [DEMO_PYTHON, "-m", "ormaos", "run", "--json-out", "out.json"]
        return self._run([*ormaos, *options, "--", DEMO_PYTHON, "job.py", *args], env)

    def ask(self, *args: str) -> dict[str, Any]:
        """One question to the CLI, answered in JSON."""
        answer = self._run([DEMO_PYTHON, "-m", "ormaos", *args, "--json"])
        assert answer.returncode == 0, answer.stdout + answer.stderr
        doc: dict[str, Any] = json.loads(answer.stdout)
        assert doc["version"] == 1
        return doc

    def say(self, *args: str) -> list[str]:
        """One question to the CLI, answered in text."""
        answer = self._run([DEMO_PYTHON, "-m", "ormaos", *args])
        assert answer.returncode == 0, answer.stderr
        return answer.stdout.splitlines()

    def stock(self) -> dict[str, int]:
        conn = sqlite3.connect(self.path / "data" / "inventory.db")
        try:
            return dict(conn.execute("SELECT sku, on_hand FROM stock"))
        finally:
            conn.close()

    def orders(self, name: str = "data/orders.csv") -> list[dict[str, str]]:
        with open(self.path / name, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def last(self) -> Recording:
        root = self.path / ".ormaos"
        newest = max(root.glob("r*"), key=lambda p: int(p.name[1:]))
        return load(str(newest))

    def line_with(self, file: str, text: str) -> int:
        lines = (self.path / file).read_text(encoding="utf-8").splitlines()
        (number,) = [n for n, line in enumerate(lines, start=1) if text in line]
        return number


@pytest.fixture
def demo(tmp_path: Path) -> Demo:
    if not (DEMO_DIR / "job.py").is_file():
        message = f"the demo app is not at {DEMO_DIR}"
        if os.environ.get("ORMAOS_DEMO_REQUIRED"):
            pytest.fail(message)
        pytest.skip(message)
    check = subprocess.run(
        [DEMO_PYTHON, "-c", "import ormaos"], capture_output=True, check=False
    )
    assert check.returncode == 0, f"{DEMO_PYTHON} cannot import ormaos"
    target = tmp_path / "order-sync"
    ignore = shutil.ignore_patterns(".venv", "data", ".ormaos", "__pycache__")
    shutil.copytree(DEMO_DIR, target, ignore=ignore)
    return Demo(target)


@pytest.mark.parametrize("source", ["csv", "api"])
def test_the_job_behaves_the_same_with_and_without_ormaos(
    demo: Demo, source: str
) -> None:
    demo.reset()
    plain = demo.plain("--source", source)
    stock_plain = demo.stock()

    demo.reset()
    recorded = demo.recorded("--source", source)
    stock_recorded = demo.stock()

    assert plain.returncode == 0 and plain.stdout == "sync finished: OK\n"
    assert recorded.returncode == plain.returncode
    assert recorded.stdout == plain.stdout
    assert recorded.program_stderr == plain.stderr
    assert stock_recorded == stock_plain
    # Nothing went wrong inside Ormaos while recording. What it saw and did
    # not record: the export is read by `csv.DictReader`, inside C code; and
    # with the API source, the job starts its own local server.
    recording = demo.last()
    opened = demo.line_with("fetch.py", "with open(path")
    expected = [f"io: reads of the 1 file opened at fetch.py:{opened}"]
    if source == "api":
        bound = demo.line_with("fetch.py", "ThreadingHTTPServer((")
        expected.append(f"io: sockets, at fetch.py:{bound} (once)")
    assert [note["what"] for note in recording.not_collected] == expected
    assert all(process.end is not None for process in recording.processes)


def test_the_job_behaves_the_same_with_line_by_line_recording(demo: Demo) -> None:
    demo.reset()
    plain = demo.plain()
    stock_plain = demo.stock()

    demo.reset()
    recorded = demo.recorded(options=("--path", "job.py", "--path", "check_sku"))

    assert recorded.returncode == plain.returncode == 0
    assert recorded.stdout == plain.stdout
    assert recorded.program_stderr == plain.stderr
    assert demo.stock() == stock_plain
    recording = demo.last()
    assert [note["what"].split(" at ")[0] for note in recording.not_collected] == [
        "io: reads of the 1 file opened"
    ]
    assert sum(1 for call in recording.calls if "path" in call) > 10


@pytest.mark.parametrize("source", ["csv", "api"])
def test_following_the_addresses_from_show_reaches_the_swallowing_except(
    demo: Demo, source: str
) -> None:
    """Only the CLI is used, and every address comes from the answer before."""
    demo.reset()
    except_line = demo.line_with("job.py", "except Exception")
    raise_line = demo.line_with("validate.py", "raise SkuFormatError")
    bad = [o for o in demo.orders() if not SKU_FORMAT.fullmatch(o["sku"])]
    demo.recorded("--source", source, options=("--path", "job.py:sync_orders"))

    overview = demo.ask("show")
    assert overview["next_commands"][0].startswith("ormaos exceptions ")

    exception = demo.ask("exceptions", overview["next"][0])
    assert exception["kind"] == "exception" and exception["status"] == "swallowed"
    assert exception["type"] == "validate.SkuFormatError"
    assert exception["raised_at"]["file"] == "validate.py"
    assert exception["raised_at"]["line"] == raise_line
    assert exception["handled_at"] == {
        "file": "job.py",
        "line": except_line,
        "fn": "sync_orders",
        "user_code": True,
    }
    assert [call["fn"] for call in exception["left_calls"]] == [
        "check_sku",
        "check_order",
    ]
    assert exception["same_type_and_handler"]["count"] == len(bad)

    # The call that swallowed it: one `check_order` per order, and fewer
    # `apply_order`, because the orders that raised never got there.
    handler = exception["handled_in"]
    assert handler in exception["next"]
    tree = demo.ask("tree", handler, "--depth", "1", "--limit", "200")
    assert tree["nodes"][0]["fn"] == "sync_orders"
    assert tree["nodes"][0]["handled_here"][0]["count"] == len(bad)
    below = [node for node in tree["nodes"] if node["depth"] == 1]
    checks = [node for node in below if node["fn"] == "check_order"]
    applies = [node for node in below if node["fn"] == "apply_order"]
    assert len(checks) == len(demo.orders())
    assert sum(1 for node in checks if node["out"] == "raised") == len(bad)
    assert len(applies) == len(checks) - len(bad)

    # Line by line: the call raises, and the next line is the `except`.
    assert exception["next_commands"][0] == f"ormaos path {exception['address']}"
    path = demo.ask("path", exception["address"])
    steps = path["steps"]
    at = next(
        i for i, s in enumerate(steps) if s.get("exception") == exception["address"]
    )
    assert steps[at - 1]["fn"] == "check_order" and steps[at - 1]["out"] == "raised"
    assert steps[at + 1]["line"] == except_line
    assert steps[at + 1]["text"].strip().startswith("except Exception")
    assert [s["line"] for s in steps[at + 1 : at + 4]] == [
        except_line,
        except_line + 1,
        except_line + 2,
    ]

    # The same in text: short, and it ends with what to ask next.
    for verb, address in (
        ("show", "last"),
        ("exceptions", overview["next"][0]),
        ("tree", handler),
        ("path", exception["address"]),
    ):
        lines = demo.say(verb, address)
        assert len(lines) <= 45 and lines[-1].startswith("next: ")
    said = "\n".join(demo.say("exceptions", overview["next"][0]))
    assert f"handled at  job.py:{except_line} in sync_orders" in said


@pytest.mark.parametrize("source", ["csv", "api"])
def test_io_shows_that_the_orders_with_a_bad_sku_never_reach_the_inventory(
    demo: Demo, source: str
) -> None:
    demo.reset()
    orders = demo.orders()
    bad = [o for o in orders if not SKU_FORMAT.fullmatch(o["sku"])]
    good = len(orders) - len(bad)
    update_line = demo.line_with("inventory.py", '"UPDATE stock SET') - 1
    result = demo.recorded("--source", source)
    assert result.stdout == "sync finished: OK\n"

    # The whole run: where the orders came from, and one UPDATE per good order.
    whole = demo.ask("io", "last", "--limit", "50")
    assert whole["by_kind"]["sqlite"] == 1 + 6 * good
    kinds = {(g["io_kind"], g["op"]) for g in whole["groups"]}
    assert ("file", "open") in kinds and ("sqlite", "connect") in kinds
    if source == "api":
        (request,) = [g for g in whole["groups"] if g["io_kind"] == "http"]
        assert (request["op"], request["target"], request["status"]) == (
            "GET",
            "/orders",
            200,
        )
        assert request["at"]["file"] == "fetch.py" and request["at"]["fn"] == "from_api"
    (update,) = [g for g in whole["groups"] if g["op"] == "UPDATE"]
    assert update["sql"] == "UPDATE stock SET on_hand = on_hand - ? WHERE sku = ?"
    assert update["table"] == "stock" and update["target"] == "data/inventory.db"
    assert update["count"] == good < len(orders)
    assert update["at"] == {
        "file": "inventory.py",
        "line": update_line,
        "fn": "apply_order",
    }

    # From the swallowed exception: the I/O of the call that handled it. Every
    # order was checked; the ones that raised did no I/O and were not applied.
    exception = demo.ask("show")["next"][0]
    handler = demo.ask("io", exception)
    assert handler["scope"]["fn"] == "sync_orders"
    assert handler["calls_directly_below"] == [
        {"fn": "check_order", "calls": len(orders), "raised": len(bad), "io": 0},
        {"fn": "apply_order", "calls": good, "raised": 0, "io": 6 * good},
    ]
    said = demo.say("io", exception)
    assert said[-2] == (
        f"calls directly below: {len(orders)} check_order ({len(bad)} raised, no io), "
        f"{good} apply_order ({6 * good} io)"
    )
    assert f"  {good}x sqlite UPDATE stock SET on_hand = on_hand - ? WHERE sku = ?" in (
        "\n".join(said)
    )

    # One order with a bad SKU: nothing below its check touched the database.
    left = demo.ask("exceptions", exception)["left_calls"]
    assert left[-1]["fn"] == "check_order"
    assert demo.ask("io", left[-1]["address"])["total"] == 0


@pytest.mark.parametrize("source", ["csv", "api"])
def test_diff_of_two_runs_of_the_same_code_is_nearly_empty(
    demo: Demo, source: str
) -> None:
    for _ in range(2):
        demo.reset()
        assert demo.recorded("--source", source).returncode == 0

    doc = demo.ask("diff", "r1", "r2")

    assert doc["context"] == []
    for name in ("calls", "exceptions", "io"):
        assert doc[name]["compared"] and doc[name]["places"] > 0
        assert doc[name]["differences"] == [], doc[name]["differences"]
    assert doc["not_collected"]["only_a"] == doc["not_collected"]["only_b"] == []
    # Two lines for the recordings, one per kind, what is not compared, `next:`.
    assert len(demo.say("diff", "r1", "r2")) == 8


def test_diff_before_and_after_the_fix_shows_what_changed(demo: Demo) -> None:
    demo.reset()
    orders = demo.orders()
    bad = len([o for o in orders if not SKU_FORMAT.fullmatch(o["sku"])])
    good = len(orders) - bad
    assert bad > 0
    assert demo.recorded().returncode == 0

    # The fix, on this temporary copy: clean the SKU when the row is parsed.
    parse = demo.path / "parse.py"
    source = parse.read_text(encoding="utf-8")
    assert source.count('sku=row["sku"],') == 1
    fixed = source.replace('sku=row["sku"],', 'sku=row["sku"].strip().upper(),')
    parse.write_text(fixed, encoding="utf-8")
    demo.reset()
    assert demo.recorded().returncode == 0

    doc = demo.ask("diff", "r1", "r2")

    # The swallowed exception is gone.
    (gone,) = doc["exceptions"]["differences"]
    assert gone["type"] == "validate.SkuFormatError" and gone["status"] == "swallowed"
    assert gone["handled_at"] == {"file": "job.py", "fn": "sync_orders"}
    assert gone["a"]["handled_line"] == demo.line_with("job.py", "except Exception")
    assert (gone["a"]["count"], gone["b"]["count"]) == (bad, 0)
    # The inventory writes of the orders that were skipped are there.
    io = {entry["sql"]: entry for entry in doc["io"]["differences"]}
    update = io["UPDATE stock SET on_hand = on_hand - ? WHERE sku = ?"]
    assert (update["file"], update["fn"]) == ("inventory.py", "apply_order")
    assert (update["a"]["count"], update["b"]["count"]) == (good, len(orders))
    (insert,) = [entry for sql, entry in io.items() if sql.startswith("INSERT INTO")]
    assert (insert["a"]["count"], insert["b"]["count"]) == (good, len(orders))
    # And the calls: every order now reaches `apply_order`.
    calls = {entry["fn"]: entry for entry in doc["calls"]["differences"]}
    applied = calls["apply_order"]
    assert (applied["a"]["count"], applied["b"]["count"]) == (good, len(orders))
    assert (calls["check_sku"]["a"]["raised"], calls["check_sku"]["b"]["raised"]) == (
        bad,
        0,
    )
    assert doc["next_commands"][0] == f"ormaos exceptions {gone['a']['first']}"
    said = "\n".join(demo.say("diff", "r1", "r2"))
    assert f"job.py:{gone['a']['handled_line']}: {bad} in r1, none in r2" in said
    assert f"in apply_order: {good} in r1, {len(orders)} in r2" in said


@pytest.mark.parametrize("source", ["csv", "api"])
def test_the_summary_shows_the_swallowed_sku_exception(demo: Demo, source: str) -> None:
    demo.reset()
    bad = [o for o in demo.orders() if not SKU_FORMAT.fullmatch(o["sku"])]
    assert bad, "the demo has no order with a bad SKU"
    except_line = demo.line_with("job.py", "except Exception")
    raise_line = demo.line_with("validate.py", "raise SkuFormatError")

    result = demo.recorded("--source", source)

    assert len(result.summary) <= 5
    assert result.summary[1] == (
        f"ormaos: swallowed {len(bad)}x validate.SkuFormatError "
        f"at job.py:{except_line}, raised at validate.py:{raise_line} [r1:e1]"
    )
    doc = json.loads((demo.path / "out.json").read_text())
    group = doc["swallowed"][0]
    assert group["type"] == "validate.SkuFormatError"
    assert group["count"] == len(bad)
    assert group["handled_at"] == {
        "file": "job.py",
        "line": except_line,
        "fn": "sync_orders",
        "user_code": True,
    }
    assert group["raised_at"] == [
        {
            "file": "validate.py",
            "line": raise_line,
            "fn": "check_sku",
            "user_code": True,
            "count": len(bad),
        }
    ]
    assert doc["next"][0] == "r1:e1"
    shown = demo._run([DEMO_PYTHON, "-m", "ormaos", "show", "--json", "last"])
    assert json.loads(shown.stdout) == doc


@pytest.mark.parametrize("source", ["csv", "api"])
def test_no_value_of_the_demo_reaches_the_recording(demo: Demo, source: str) -> None:
    demo.reset()
    planted = {
        name: f"planted-{name}-{secrets.token_hex(10)}"
        for name in ("order", "sku", "channel", "env")
    }
    with open(demo.path / "data" / "orders.csv", "a", newline="") as handle:
        row = [planted["order"], "2026-11-10T17:00", planted["sku"], "2"]
        csv.writer(handle).writerow([*row, planted["channel"]])
    values = set(planted.values())
    for order in demo.orders():
        values.update({order["order_id"], order["sku"].strip(), order["placed_at"]})

    result = demo.recorded(
        "--source",
        source,
        env={"PLANTED_VARIABLE": planted["env"]},
        options=("--path", "job.py", "--path", "validate.py"),
    )

    assert result.returncode == 0 and result.stdout == "sync finished: OK\n"
    doc = json.loads((demo.path / "out.json").read_text())
    assert (
        "PLANTED_VARIABLE" in doc["context"]["env_names"]
    )  # the name, never the value
    files = [p for p in (demo.path / ".ormaos").rglob("*") if p.is_file()]
    files.append(demo.path / "out.json")
    assert len(files) >= 4
    for file in files:
        content = file.read_text(encoding="utf-8")
        for value in values:
            assert value not in content, f"{value!r} found in {file.name}"
    for value in values:
        assert value not in result.stderr
