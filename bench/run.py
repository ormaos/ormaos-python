"""Measure what recording costs, per capability set, on two fixed workloads.

    uv run python bench/run.py --runs 20

Every workload is run N times without Ormaos and N times with each capability
set. What is timed is the whole command, from start to exit, as a user waits
for it: for `ormaos run` that includes starting the CLI, the program, and
reading the recording to print the summary. The sets take turns (one run of
each, then the next round), so a change in the machine during the bench
touches all of them. One round before the measured ones is thrown away.

The result is a Markdown file (default: bench/RESULTS.md), also printed.
The Python that runs this file must have ormaos installed; the same Python
runs the workloads. Standard library only. POSIX only (`env -u`).
"""

import argparse
import datetime
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON = sys.executable
ORMAOS = [PYTHON, "-m", "ormaos", "run"]


@dataclass
class Workload:
    name: str
    file: str
    what: str
    scope: str  # the function recorded line by line in the "path" set
    takes_directory: bool = False

    @property
    def path(self) -> str:
        return str(HERE / "workloads" / self.file)


WORKLOADS = [
    Workload(
        "CPU-bound",
        "cpu_bound.py",
        "150,000 calls of small pure-Python functions, no I/O",
        "cpu_bound.py:score",
    ),
    Workload(
        "I/O",
        "io_bound.py",
        "a SQLite file (2,000 inserts in 20 transactions, 200 queries) and "
        "200 small files written and read back",
        "io_bound.py:store",
        takes_directory=True,
    ),
]


@dataclass
class Variant:
    name: str
    shown: str  # the command, as written in the table
    prefix: list[str]
    seconds: list[float] = field(default_factory=list)
    recorded: str = ""
    notes: list[str] = field(default_factory=list)


def variants(workload: Workload) -> list[Variant]:
    # "Context only": `ormaos run` records the command, its duration, CPU and
    # memory, and the Python inside starts without the recorder.
    return [
        Variant("without Ormaos", "python", []),
        Variant(
            "context only",
            "ormaos run -- env -u ORMAOS_DIR python",
            [*ORMAOS, "--", "env", "-u", "ORMAOS_DIR"],
        ),
        Variant(
            "calls and exceptions",
            "ormaos run --no-io -- python",
            [*ORMAOS, "--no-io", "--"],
        ),
        Variant(
            "calls, exceptions and path",
            f"ormaos run --no-io --path {workload.scope} -- python",
            [*ORMAOS, "--no-io", "--path", workload.scope, "--"],
        ),
        Variant(
            "calls, exceptions and io (the default)",
            "ormaos run -- python",
            [*ORMAOS, "--"],
        ),
    ]


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("ORMAOS_")}


def run_once(
    workload: Workload, variant: Variant, keep: Path | None
) -> tuple[float, str]:
    """One run in a new, empty directory. Returns the seconds and the stdout."""
    directory = Path(tempfile.mkdtemp(prefix="ormaos-bench-"))
    try:
        argv = [*variant.prefix, PYTHON, workload.path]
        if workload.takes_directory:
            data = directory / "data"
            data.mkdir()
            argv.append(str(data))
        started = time.perf_counter()
        done = subprocess.run(
            argv,
            cwd=directory,
            env=clean_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        seconds = time.perf_counter() - started
        if done.returncode != 0:
            raise SystemExit(
                f"{variant.name}: {workload.file} exited {done.returncode}"
            )
        if keep is not None and (directory / ".ormaos").is_dir():
            shutil.copytree(directory / ".ormaos", keep / ".ormaos")
        return seconds, done.stdout
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def describe(variant: Variant, kept: Path) -> None:
    """What the last run of this set recorded, read from its recording."""
    if not variant.prefix:
        variant.recorded = "nothing"
        return
    from ormaos.recording import load

    recording = load(str(kept / ".ormaos" / "r1"))
    calls, exceptions = len(recording.calls), len(recording.exceptions)
    io = sum(int(event.get("n", 1)) for event in recording.io)
    lines = sum(int((p.end or {}).get("lines", 0)) for p in recording.processes)
    parts = [f"{calls:,} calls", f"{exceptions:,} exceptions"]
    if lines:
        parts.append(f"{lines:,} lines")
    parts.append(f"{io:,} io events")
    if not recording.processes:
        parts = ["the context of the command"]
    variant.recorded = ", ".join(parts)
    variant.notes = [
        f"{note.get('what')}: {note.get('reason')}" for note in recording.not_collected
    ]


def measure(workload: Workload, runs: int, warmup: int) -> list[Variant]:
    sets = variants(workload)
    expected = None
    for round_number in range(-warmup, runs):
        for variant in sets:
            last = round_number == runs - 1
            kept = Path(tempfile.mkdtemp(prefix="ormaos-bench-kept-")) if last else None
            try:
                seconds, stdout = run_once(workload, variant, kept)
                if kept is not None:
                    describe(variant, kept)
            finally:
                if kept is not None:
                    shutil.rmtree(kept, ignore_errors=True)
            if expected is None:
                expected = stdout
            if stdout != expected:
                raise SystemExit(
                    f"{variant.name}: {workload.file} printed something else"
                )
            if round_number >= 0:
                variant.seconds.append(seconds)
        print(f"{workload.name}: round {round_number + 1} of {runs}", file=sys.stderr)
    return sets


def cpu_name() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def ms(seconds: float) -> str:
    return f"{seconds * 1000:.1f}"


def table(sets: list[Variant]) -> list[str]:
    base = statistics.median(sets[0].seconds)
    lines = [
        "| Capability set | Command | Median (ms) | p25-p75 (ms) | Min-max (ms) "
        "| Added to the median | Recorded in one run |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for variant in sets:
        times = sorted(variant.seconds)
        median = statistics.median(times)
        if len(times) >= 2:
            quarter = statistics.quantiles(times, n=4, method="inclusive")
            spread = f"{ms(quarter[0])}-{ms(quarter[2])}"
        else:
            spread = "one run"
        added = (
            "" if variant is sets[0] else f"{ms(median - base)} (x{median / base:.2f})"
        )
        lines.append(
            f"| {variant.name} | `{variant.shown}` | {ms(median)} | {spread} "
            f"| {ms(times[0])}-{ms(times[-1])} | {added} | {variant.recorded} |"
        )
    return lines


def report(
    results: list[tuple[Workload, list[Variant]]], runs: int, warmup: int
) -> str:
    from ormaos import __version__

    now = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    out = ["# Recording overhead", ""]
    if runs < 20:
        out += [
            f"**Trial run with N={runs}: it shows that the bench works. "
            "It is not a measurement.**",
            "",
        ]
    out += [
        "Written by `bench/run.py`. Each row is the whole command, from start to "
        "exit, in milliseconds of wall-clock time: for `ormaos run` that includes "
        "starting the CLI, running the program, and reading the recording to print "
        "the summary.",
        "",
        f"- N: {runs} runs of each row, after {warmup} discarded; the rows take turns",
        f"- Machine: {cpu_name()}, {os.cpu_count()} logical CPUs, {platform.machine()}",
        f"- OS: {platform.platform()}",
        f"- Python: {platform.python_implementation()} {platform.python_version()}",
        f"- Ormaos: {__version__}",
        f"- Temporary directory: {tempfile.gettempdir()}",
        f"- When: {now}",
        "",
    ]
    for workload, sets in results:
        out += [f"## {workload.name} workload", ""]
        out += [f"`bench/workloads/{workload.file}`: {workload.what}.", ""]
        out += table(sets)
        out.append("")
        notes = [(v.name, note) for v in sets for note in v.notes]
        if notes:
            out.append("Listed as not collected in these recordings:")
            out += [f"- {name}: {note}" for name, note in notes]
            out.append("")
    out += [
        "## How to read it",
        "",
        '- "Added to the median" is the median of the row minus the median of '
        '"without Ormaos", and their ratio.',
        '- "context only" is `ormaos run` around a Python that starts without the '
        "recorder: the cost of the command itself, with nothing recorded inside "
        "Python.",
        "- The other rows record inside Python. Their cost grows with the number of "
        "calls, lines and I/O events, shown in the last column, and with the time "
        "to read the recording back for the summary.",
        "- These are two workloads on one machine. Another program has another "
        "number of calls per second, and another cost.",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--runs", type=int, default=20, metavar="N")
    parser.add_argument("--warmup", type=int, default=1, metavar="N")
    parser.add_argument("--out", default=str(HERE / "RESULTS.md"), metavar="FILE")
    args = parser.parse_args()
    if args.runs < 1 or args.warmup < 0:
        parser.error("--runs must be 1 or more, --warmup 0 or more")
    check = subprocess.run([PYTHON, "-c", "import ormaos"], check=False)
    if check.returncode != 0:
        raise SystemExit(f"{PYTHON} cannot import ormaos: run with `uv run python`")
    results = [(w, measure(w, args.runs, args.warmup)) for w in WORKLOADS]
    text = report(results, args.runs, args.warmup)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    print(f"written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
