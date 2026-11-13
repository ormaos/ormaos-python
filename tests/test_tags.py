"""Every event says which thread, which asyncio task and which process."""

import json

from conftest import Workdir

APP = """\
import asyncio, threading

def plain(n):
    return n

def in_thread():
    plain(1)
    try:
        int("x")
    except ValueError:
        pass
    with open(__file__) as handle:
        handle.read()

async def job(n):
    await asyncio.sleep(0)
    plain(n)
    try:
        int("x")
    except ValueError:
        pass
    with open(__file__) as handle:
        handle.read()
    return n

async def main():
    await job(0)
    return await asyncio.gather(job(1), job(2))

plain(0)
thread = threading.Thread(target=in_thread)
thread.start()
thread.join()
asyncio.run(main())
asyncio.run(main())
"""


def test_calls_exceptions_and_io_carry_thread_task_and_process(work: Workdir) -> None:
    work.write("app.py", APP)
    result = work.python("app.py")

    assert result.returncode == 0
    recording = work.last()
    events = [*recording.calls, *recording.exceptions, *recording.io]
    assert all(isinstance(event["thread"], int) for event in events)
    assert all(event["process"] == 1 for event in events)

    def of(fn: str) -> list[dict[str, object]]:
        return [call for call in recording.calls if call["fn"] == fn]

    # Outside asyncio: no task. The thread function is in thread 2.
    (module,) = of("<module>")
    assert module["thread"] == 1 and "task" not in module
    (threaded,) = of("in_thread")
    assert threaded["thread"] == 2 and "task" not in threaded
    assert [c["thread"] for c in of("plain")][:2] == [1, 2]

    # Each `asyncio.run` has a main task; `gather` adds one task per job.
    # `await job(0)` runs inside the task of `main`.
    mains = of("main")
    jobs = of("job")
    assert len(mains) == 2 and len(jobs) == 6
    tasks = [call["task"] for call in jobs]
    assert tasks[0] == mains[0]["task"] and tasks[3] == mains[1]["task"]
    assert len(set(tasks)) == 6 and all(isinstance(task, int) for task in tasks)
    # What a job does carries the job's task: its calls, exceptions and I/O.
    by_address = {call["addr"]: call for call in recording.calls}
    for call in of("plain")[2:]:
        assert call["task"] == by_address[call["parent_addr"]]["task"]
    # The program's own exceptions; asyncio raises others to end a coroutine.
    in_jobs = [
        exc
        for exc in recording.exceptions
        if exc["type"] == "ValueError" and "task" in exc
    ]
    for exc in in_jobs:
        assert exc.get("task") == by_address[exc["handled_addr"]].get("task")
    assert sum(1 for exc in in_jobs if "task" in exc) == 6
    opens = [event for event in recording.io if event["op"] == "open"]
    assert len(opens) == 7
    for event in opens:
        assert event.get("task") == by_address[event["call_addr"]].get("task")

    # The verbs show it: in JSON always, in text when it is not the first.
    job = jobs[1]
    node = json.loads(work.cli("tree", job["addr"], "--json").stdout)["nodes"][0]
    assert (node["thread"], node["process"], node["task"]) == (1, 1, job["task"])
    (text,) = [
        line
        for line in work.cli("tree", job["addr"]).stdout.splitlines()
        if line.startswith(job["addr"] + " ")
    ]
    assert f"task {job['task']}" in text and "thread" not in text
    assert "  thread 2" in work.cli("tree", threaded["addr"]).stdout
    listed = json.loads(work.cli("exceptions", "r1", "--json").stdout)["exceptions"]
    assert sorted(entry["thread"] for entry in listed).count(2) == 1
    in_tasks = [e for e in listed if "task" in e and e["type"] == "ValueError"]
    assert len(in_tasks) == 6
    event = json.loads(work.cli("io", opens[-1]["addr"], "--json").stdout)
    assert event["task"] == opens[-1]["task"] and event["thread"] == 1
    processes = json.loads(work.cli("show", "--json").stdout)["context"]["processes"]
    assert processes[0]["threads"] == 2 and processes[0]["tasks"] == 6
