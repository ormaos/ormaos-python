"""`bench/run.py` runs and writes its table."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import clean_env

BENCH = Path(__file__).resolve().parent.parent / "bench"


@pytest.mark.skipif(shutil.which("env") is None, reason="the bench uses `env -u`")
def test_the_bench_writes_one_table_per_workload(tmp_path: Path) -> None:
    out = tmp_path / "results.md"
    before = sorted(p.name for p in BENCH.iterdir())

    options = ["--runs", "1", "--warmup", "0", "--out", str(out)]
    done = subprocess.run(
        [sys.executable, str(BENCH / "run.py"), *options],
        cwd=tmp_path,
        env=clean_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert done.returncode == 0, done.stderr
    text = out.read_text(encoding="utf-8")
    assert done.stdout.strip() == text.strip()
    # One run is a trial, and the file says so.
    assert "Trial run with N=1" in text
    for fact in ("- N: 1 runs", "- Machine: ", "- OS: ", "- Python: ", "- Ormaos: "):
        assert fact in text
    assert "## CPU-bound workload" in text and "## I/O workload" in text
    rows = [line for line in text.splitlines() if line.startswith("| ")]
    names = [row.split("|")[1].strip() for row in rows]
    sets = [
        "without Ormaos",
        "context only",
        "calls and exceptions",
        "calls, exceptions and path",
        "calls, exceptions and io (the default)",
    ]
    assert names == ["Capability set", *sets] * 2
    cpu, io = rows[1:6], rows[7:12]
    assert "150,002 calls" in cpu[2] and "150,000 lines" in cpu[3]
    assert "0 io events" in io[2] and "2,000 lines" in io[3]
    assert "0 io events" not in io[4]
    assert "the context of the command" in cpu[1]
    # Nothing is left in the repository or in the working directory.
    assert sorted(p.name for p in BENCH.iterdir()) == before
    assert [p.name for p in tmp_path.iterdir()] == ["results.md"]
