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

    def recorded(self, *args: str, env: dict[str, str] | None = None) -> Result:
        ormaos = [DEMO_PYTHON, "-m", "ormaos", "run", "--json-out", "out.json", "--"]
        return self._run([*ormaos, DEMO_PYTHON, "job.py", *args], env)

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
    # Nothing went wrong inside Ormaos while recording.
    recording = demo.last()
    assert recording.not_collected == []
    assert all(process.end is not None for process in recording.processes)


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

    result = demo.recorded("--source", source, env={"PLANTED_VARIABLE": planted["env"]})

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
