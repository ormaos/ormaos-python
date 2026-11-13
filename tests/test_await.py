"""The `StopIteration` that ends an `await`: counted and grouped, not hidden."""

import json

from conftest import Workdir

APP = """\
import asyncio

async def job(n):
    await asyncio.sleep(0)
    try:
        int("x")
    except ValueError:
        pass
    return n

async def twice():
    await asyncio.gather(job(1), job(2))            # one await of a future
    return await asyncio.gather(job(3), job(4))     # another line, another origin

async def main():
    await job(0)                                    # a coroutine: no future
    for _ in range(3):
        await asyncio.gather(job(5), job(6))        # the same line, three times
    await twice()

asyncio.run(main())
"""


def test_stop_iteration_at_await_is_grouped_by_origin(work: Workdir) -> None:
    source = work.write("app.py", APP)
    lines = source.read_text().splitlines()
    loop_line = next(n for n, text in enumerate(lines, 1) if "job(5)" in text)
    first_line = next(n for n, text in enumerate(lines, 1) if "job(1)" in text)
    second_line = next(n for n, text in enumerate(lines, 1) if "job(3)" in text)
    result = work.python("app.py")
    assert result.returncode == 0

    recording = work.last()
    taken = [e for e in recording.exceptions if e["end"] == "taken_by_interpreter"]
    values = [e for e in recording.exceptions if e["type"] == "ValueError"]
    # Every `ValueError` of the program is there, each swallowed in `job`:
    # none is mixed with a `StopIteration`.
    assert len(values) == 11 and all(e["swallowed"] for e in values)
    assert {e["handled"]["fn"] for e in values} == {"job"}
    assert len(taken) == 5 and len(recording.exceptions) == 16
    assert {(e["type"], e["how"]) for e in taken} == {("StopIteration", "await")}
    assert all("handled" not in e and "swallowed" not in e for e in taken)

    # The summary and `show` count them, by name.
    assert result.summary[0].endswith(
        "16 exceptions (11 swallowed, 5 StopIteration ended at await)"
    )
    shown = work.cli("show").stdout.splitlines()[0]
    assert shown.endswith(
        "16 exceptions (11 swallowed, 0 uncaught, 5 StopIteration ended at await)"
    )
    overview = json.loads(work.cli("show", "--json").stdout)
    assert overview["counts"]["taken_by_interpreter"] == 5
    assert overview["taken_by_interpreter"]["counts"] == [
        "5 StopIteration ended at await"
    ]

    # `exceptions`: one line per origin, after the others, which are listed
    # one by one as before.
    text = work.cli("exceptions", "r1", "--limit", "50").stdout.splitlines()
    assert text[0] == (
        "r1: 16 exceptions (11 swallowed, 0 uncaught, 5 StopIteration ended at await)"
    )
    assert sum(1 for line in text if " swallowed ValueError " in line) == 11
    grouped = [line for line in text if "ended at await" in line][1:]
    assert [line.split("  [")[0] for line in grouped] == [
        f"3x StopIteration ended at await, at app.py:{loop_line} in main",
        f"1x StopIteration ended at await, at app.py:{first_line} in twice",
        f"1x StopIteration ended at await, at app.py:{second_line} in twice",
    ]
    assert not any(line.split()[1:2] == ["ended"] for line in text[1:])

    # In JSON every occurrence can be counted, and has its address.
    doc = json.loads(work.cli("exceptions", "r1", "--json").stdout)
    assert doc["total"] == 16 and doc["swallowed"] == 11
    assert all(entry["type"] == "ValueError" for entry in doc["exceptions"])
    part = doc["taken_by_interpreter"]
    assert part["total"] == 5 and part["counts"] == ["5 StopIteration ended at await"]
    groups = part["groups"]
    assert [(g["count"], g["raised_at"]["line"]) for g in groups] == [
        (3, loop_line),
        (1, first_line),
        (1, second_line),
    ]
    addresses = [address for group in groups for address in group["addresses"]]
    assert sorted(addresses) == sorted(e["addr"] for e in taken)
    assert all(g["how"] == "await" and g["ended"] == "ended at await" for g in groups)

    # One of them, asked for by its address.
    one = json.loads(work.cli("exceptions", addresses[0], "--json").stdout)
    assert one["type"] == "StopIteration" and one["status"] == "ended at await"
    assert one["handled_at"] is None
    said = work.cli("exceptions", addresses[0]).stdout.splitlines()
    assert said[0] == f"{addresses[0]} StopIteration: ended at await"

    # Inside one call: only what happened there.
    twice = next(c for c in recording.calls if c["fn"] == "twice")
    inside = json.loads(work.cli("exceptions", twice["addr"], "--json").stdout)
    assert inside["taken_by_interpreter"]["total"] == 2
