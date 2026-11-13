# ormaos

Ormaos records what happened inside a Python execution and hands it to the developer and to their coding agent.

> Status: being built for Hack Brasil 2026. `ormaos run` records calls, exceptions, I/O and, inside a scope you ask for, the lines that ran; `with ormaos.record():` does the same for a block of a script or a notebook. `ormaos show`, `tree`, `exceptions`, `path`, `io` and `diff` read a recording, `ormaos list` and `ormaos status` say what is there. `ormaos guide` tells a coding agent how to use it.

## What works today

```bash
ormaos run -- python -c "
def load(key):
    return {}[key]
try:
    load('sku')
except KeyError:
    pass
"
```

The program runs unchanged, with its own output and exit code. Then, on stderr, at most 5 lines:

```
ormaos: r1 exit 0 in 0.03s: 1 Python process, 2 calls, 1 exception (1 swallowed)
ormaos: swallowed 1x KeyError at <string>:6, raised at <string>:3 [r1:e1]
ormaos: saved in .ormaos/r1, next: ormaos exceptions r1:e1 | ormaos tree r1:c1
```

The recording is in `.ormaos/r1/`, as JSON lines (`"format": "provisional-0"`, it will change):

- `context`: the command, the names of the environment variables (never the values), duration, CPU, memory, threads, and one file per Python process: the Python subprocesses and the `multiprocessing` children are in the same recording, each linked to the process that started it.
- `calls`: the call tree of your code, with the shape of the arguments and of the return value.
- `exceptions`: every exception that passes through your code, where it was raised and where it was handled, marked `swallowed` when it was handled and not raised again.
- `io`: sqlite3 statements, files opened, read and written, subprocesses started and HTTP requests, each with the line of your code that caused it (see below).
- `path`: line by line, only inside a scope you ask for with `--path` (see below).
- `not_collected`: everything that was not recorded, with the reason.

Every call, exception and I/O event says which process, which thread and, inside asyncio, which task it ran in. The numbers are small and belong to one recording (`thread 2`, `task 5`); names are not recorded.

### In a script or a notebook: `with ormaos.record():`

```python
import ormaos

with (
    ormaos.record()
):  # or: ormaos.record(path="job.py:sync_orders", io=False, summary=True)
    main()
```

The block is recorded into `.ormaos/` like a run, with the Python processes started inside it, and is read with the same commands. Only what runs inside the block is recorded. If the recording cannot start, the block runs unrecorded and nothing is raised; an exception of the block goes through unchanged. Under `ormaos run`, the block is part of that recording. While the block runs, `ORMAOS_DIR`, `ORMAOS_PATH` and `ORMAOS_IO` are set in the environment (that is how child processes find the recording) and put back after.

### The `StopIteration` of an `await`

When an `await` of a future finishes (`await asyncio.gather(...)`, `await asyncio.sleep(1)`), Python raises a `StopIteration` inside the coroutine to hand over the result, and takes it itself: no `except` ever sees it. Ormaos records each one, counts them apart and groups them by the line of the `await`:

```
r1: 16 exceptions (11 swallowed, 0 uncaught, 5 StopIteration ended at await)
r1:e1 swallowed ValueError  raised at app.py:6  handled at app.py:7 in job  task 2
...
3x StopIteration ended at await, at app.py:18 in main  [r1:e4]
```

With `--json`, `taken_by_interpreter` has the total, the counts and one group per origin with the address of every occurrence.

### What is there: `ormaos list` and `ormaos status`

```bash
ormaos list      # the recordings, newest first: when, how it ended, how long, the counts, the command
ormaos status    # the version, whether this Python can record, what is running now, the last recording
```

```
3 recordings in .ormaos, newest first
r3  2026-11-13 11:03:34  exit 137 (signal 9), incomplete  759.1ms  1 process, 62 calls, 59 exceptions (59 swallowed)  python job.py
r2  2026-11-13 11:03:33  exit 0  343.2ms  1 process, 240 calls, 6 exceptions (5 swallowed), 123 io  python job.py --source api
```

### A process that is killed

The recorder writes in chunks while the program runs: every 256 records, every 0.2 seconds (checked when a record arrives), and when a call that has no call of your code above it starts or ends. Each chunk ends with a checkpoint: the calls that have not returned and the exceptions whose end is not known yet. So after `kill -9`, or while the program is still running, `ormaos show` reads what was written: the recording is marked `(incomplete)`, the calls that had not returned are shown as `-> open`, and `not_collected` says that the last events of that process may be missing. `multiprocessing` children end with `os._exit`; their recording is closed when their work returns. A pool ends the workers that are still alive with a signal, and `Process.terminate()` does the same: the parent writes down that it ended that child itself, so the recording is not marked incomplete for it. `ormaos show` says `ended by its pool (signal 15)` for that process, and `not_collected` says that its end record (duration, CPU, memory) is missing and whether a call of your code was running in it. A process killed in any other way makes the recording incomplete.

### The file header

Every file starts with one line: `"format": "provisional-0"`, `"kind"`, and two parts. `core` is what a recorder of any language would write: the process (`pid`, `ppid`, `cwd`) or the command, the `capabilities` that were recorded (`context`, `calls`, `exceptions`, `path`, `io`), what is never recorded, and `anchor`: `{"t": 0, "unix": 1794578386.2, "clock": "monotonic, nanoseconds"}`. Event times (`t`) are nanoseconds on a monotonic clock; the anchor says which moment of the wall clock `t = 0` was. `python` is what only this recorder knows: the version, the executable, how Python was started, whether the process was forked, the path scope.

## Reading a recording

Everything in a recording has a short address: `r1` is the recording, `r1:c7` a call, `r1:e1` an exception, `r1:i3` an I/O event. Each answer is short and ends with a `next:` line: the commands for the next question.

```bash
ormaos show                # the overview of the last recording (or: ormaos show r1)
ormaos exceptions r1       # its exceptions, swallowed first
ormaos exceptions r1:e1    # one exception: where raised, the calls it left, where handled
ormaos tree r1:c7          # the calls below a call: shapes, results, durations
ormaos path r1:c7          # the lines that call ran, in order
ormaos io r1:c7            # the I/O of that call and of the calls below it
ormaos diff r1 r2          # what r2 did differently from r1
```

```
$ ormaos exceptions r1:e1
r1:e1 KeyError: swallowed
  raised at   <string>:3 in load  [r1:c2]
  left calls   load [r1:c2]
  handled at  <string>:6 in <module>  [r1:c1]  and not raised again
  message     not recorded; args: tuple[1 str]
next: ormaos tree r1:c1 | ormaos tree r1:c2 | ormaos path r1:e1
```

Start with `ormaos guide`: it explains in about 50 lines when to use Ormaos, where to start, how to follow addresses and what is not recorded. It is written for a coding agent.

- `tree`, `exceptions`, `path` and `io` take a recording, a call or an exception. An exception stands for the call that handled it.
- Every answer is bounded. `--limit N` asks for more lines, `--offset N` skips entries you have seen (for `tree`: calls directly below the address); `tree` also takes `--depth N`.
- An exception is `swallowed` (it entered an `except` and was not raised again), `uncaught` (it ended the program), `replaced` (another one was raised from its handler) or `exit` (a `SystemExit`, as `sys.exit()` raises).
- `--json` on every verb prints one JSON document on stdout, also for errors (`"kind": "error"`). Every document has `"kind"`, `"version"`, `"next"` (addresses) and `"next_commands"`.

### Line by line: `--path`

Lines are recorded only where you ask, because recording every line of a program costs too much:

```bash
ormaos run --path job.py:sync_orders -- python job.py   # also: --path job.py, --path sync_orders
ormaos path last                                        # the functions that have lines
ormaos path r2:e1                                       # the handler of r2:e1, around the moment it caught it
```

`path` shows the lines in the order they ran, with the calls made and the exceptions caught in between. The text of each line is read from your file when you ask, it is not in the recording.

### I/O: `ormaos io`

```
$ ormaos io r1:c128
r1:c128 sync_orders and the calls below it: 180 io events (sqlite 180)
  30x sqlite SELECT on_hand FROM stock WHERE sku = ?  at inventory.py:65 in apply_order  [r1:i3]
  30x sqlite SELECT ? FROM applied_orders WHERE order_id = ?  at inventory.py:70 in apply_order  [r1:i4]
  30x sqlite BEGIN  at inventory.py:76 in apply_order  [r1:i5]
  30x sqlite UPDATE stock SET on_hand = on_hand - ? WHERE sku = ?  at inventory.py:76 in apply_order  [r1:i6]
  30x sqlite INSERT INTO applied_orders (order_id, sku, quantity, applied_at) VALU...  at inventory.py:80 in apply_order  [r1:i7]
  30x sqlite COMMIT  at inventory.py:75 in apply_order  [r1:i8]
calls directly below: 36 check_order (6 raised, no io), 30 apply_order (180 io)
next: ormaos io r1:i3 | ormaos io r1:i4 | ormaos tree r1:c128 | ormaos io r1:c14
```

The I/O is grouped by what was done and by the line of your code that caused it, with a count. `--kind sqlite|file|subprocess|http` shows one kind. `ormaos io r1:i7` shows one event. `ormaos run --no-io` records without I/O.

What is written for each kind, and what is not:

| Kind | Recorded | Never recorded |
|---|---|---|
| `sqlite` | the connection (the path of the database), each statement with every literal replaced by `?`, the table | parameters, literals, rows |
| `file` | `open` with the path and the mode; `read` and `write` when Python code calls them on the file | contents, sizes |
| `subprocess` | the name of the program started (`subprocess`, `os.system`, `os.exec*`, `os.posix_spawn`, `os.fork`) and how many arguments | arguments, environment, output, exit status |
| `http` | method, scheme, host, port, path, status and duration, for `http.client` (so also `urllib`, `requests`, `urllib3`) and for `httpx` | query string, headers, bodies |

How it is observed. Ormaos replaces and wraps nothing: `open` is still `open`, a connection is still a `sqlite3.Connection`.

- Files, subprocesses, `sqlite3.connect` and the request line of `http.client` are seen through Python's audit events (`sys.addaudithook`). An audit hook cannot be removed; when recording stops it stays and does nothing.
- `read` and `write` on a file, and the status of an HTTP response, are seen through `sys.monitoring`. `requests` and `httpx` are never imported by Ormaos: their code is recognised when your program runs it.
- sqlite3 has no event for statements. So Ormaos sets the trace callback of each connection (`Connection.set_trace_callback`), as soon as the connection is made: this is the one place where it sets something on an object of your program. If your program sets or clears the trace callback itself, your choice stays and works as always; the statements of that connection are not recorded after that, and `not_collected` names the connection and the line (when the call is made by Python code; a call made from inside C code, such as `operator.methodcaller`, is not seen). If your program had already set a callback when Ormaos comes to set its own (this happens only when no Python call was seen in between), yours is kept: it is called after Ormaos's, with the same statement, in the same order, and what it raises is reported by sqlite3 as before. In that one case your callback has one more frame above it on the stack.

Limits of `io`. The ones that depend on the run are listed in `not_collected` of the recording when they happen, with the line of your code:

- A file that is iterated (`for line in f`, `csv.reader(f)`) or written by `print(..., file=f)` is read or written inside C code, so only its `open` is seen. A file that was opened and never seen read (or written) by a call is listed, with the line that opened it.
- An `open` is recorded when it is asked for. When it fails, the event is marked `-> raised` (the exception is recorded too).
- Sockets, directory and file-system operations (`os.listdir`, `os.mkdir`, `os.remove`, ...) and `os.read`/`os.write` are not recorded; each line of your code that does one is listed, with a count.
- I/O that no line of your code is waiting for is not recorded. What the import system does is never counted; the rest (library threads, the interpreter printing a traceback) is counted in one line.
- Other databases, and anything a C library does by itself, are never seen.
- A quoted name in SQL (`"stock"`) is kept only where a name is expected (after `FROM`, `INTO`, `UPDATE`, `JOIN`, ..., or next to a dot); anywhere else it is shown as `?`.

### Two recordings: `ormaos diff`

```
$ ormaos diff r2 r3
r2 -> r3, matched by place in the code and order, not by address
  r2: exit 0, 260 calls, 7 exceptions, 182 io events
  r3: exit 0, 272 calls, 1 exception, 218 io events
calls: 4 of 27 places differ
  check_sku at validate.py:22: 36 calls in both; 6 raised in r2, none in r3; call 7 raised in r2 and returned in r3  [r2:c154 r3:c154]
  apply_order at inventory.py:59: 30 calls in r2, 36 in r3  [r2:c132 r3:c132]
  ...
exceptions: 1 of 2 groups differ
  swallowed validate.SkuFormatError, raised at validate.py:24, handled at job.py:57: 6 in r2, none in r3  [r2:e1]
io: 6 of 8 groups differ
  sqlite UPDATE stock SET on_hand = on_hand - ? WHERE sku = ?  at inventory.py:76 in apply_order: 30 in r2, 36 in r3  [r2:i6 r3:i6]
  ...
not compared: values, shapes of arguments and results, durations, cpu and memory, the lines of `path`, which process or thread an event was in
next: ormaos exceptions r2:e1 | ormaos tree r2:c153 | ormaos io r2:i3 | ormaos tree r3:c153 | ormaos io r3:i3
```

An address belongs to one recording, so `diff` never matches by address. It matches a call, an exception or an I/O event by where it is in the code (file, function, line), and events at the same place by order: the first with the first. When a line moved between the two recordings and the file and function leave no doubt, the place is still matched and both lines are shown. The answer says what each recording has, with the counts; it does not say which one is right. Two recordings of the same code and the same input give `the same in both` three times. A kind of event that one recording did not record (`--no-io`) is not compared, and the answer says so. `--limit N` shows more differences, `--json` prints the document.

### For coding agents: `ormaos guide`

```bash
ormaos guide                               # the guide, as text (or --json)
ormaos guide --agents-md                   # one line to put in the AGENTS.md of your project
ormaos guide --agents-md --write           # shows the change as a diff, then adds the line to AGENTS.md
ormaos guide --agents-md --write FILE      # the same, for another file
```

Without `--write`, no file is touched. With it, the diff is printed first, and a file that already mentions `ormaos guide` is left as it is.

[skills/ormaos/SKILL.md](skills/ormaos/SKILL.md) is the same guidance as a skill, in the `SKILL.md` format (a header with `name` and `description`, then instructions). Copy the folder `skills/ormaos` into the skills folder of your agent.

### JSON overview

```bash
ormaos run --json-out overview.json -- python job.py   # written next to the run
ormaos show --json last                                # or r1, r2, ...
```

The JSON goes to the file or to stdout, never mixed with the program's output. It has the context, the counts, the swallowed exceptions grouped by handler (with where they were raised and how many times), the uncaught ones, the I/O counted by kind, `not_collected`, and `next`: the addresses to ask about next.

Shape always, value never: Ormaos records types, lengths and structure (`{"type": "str", "len": 14}`), never the content of an argument, an environment variable or an exception message. For I/O it records names (a path, a table, a host, a program) and never contents.

## Limits today

- The Python that the command starts must have `ormaos` installed in its environment. If not, the command still runs and the summary says that no Python process was recorded.
- "Your code" is every file outside the standard library and outside `site-packages`. Set `ORMAOS_ONLY` to a list of paths to narrow it.
- The handler line is the `except` clause that matched, also when its types are written on several lines. For `except*` it is the first clause of the `try` (several clauses can each take a part of the group); the place is listed in `not_collected` when it happens.
- Recording costs time on every call, and the files are large for long runs. At most 200,000 calls, 50,000 exceptions and 200,000 lines are kept per process (`ORMAOS_MAX_CALLS`, `ORMAOS_MAX_EXCEPTIONS`, `ORMAOS_MAX_LINES`); the rest is listed in `not_collected`. `bench/` measures the cost (see "Measuring the cost").
- `path` has the lines of the functions inside the scope, not of the functions they call (those appear as calls).
- At most 100,000 I/O events are kept per process (`ORMAOS_MAX_IO`). The same event repeated at the same line is one event with a count.
- A process that is killed, or that ends with `os._exit`, loses the events that were not written yet: at most 256 records or what arrived in the last 0.2 seconds (see "A process that is killed"). The recording says so.
- Thread numbers come from the thread identifier: a thread that starts after another one has ended can get the same number. Task numbers are per process.
- `with ormaos.record():` changes the environment of the program while the block runs: it sets `ORMAOS_DIR`, `ORMAOS_PATH` and `ORMAOS_IO` in `os.environ`, and when the block ends it puts each back to the value the program had, or removes it if it had none. A program that reads those names inside the block sees Ormaos's values. Every recording of a block says so in `not_collected`.
- `ormaos run` waits up to one second, after the command has ended, for Python processes of the recording that are still alive (a `multiprocessing` fork server), before it prints the summary.
- `with ormaos.record():` records the calls made inside the block; the function that contains the block is not a call of the recording. In a notebook, the cells are your code when their name is `<ipython-input-...>` or a file outside `site-packages`; this was tested with code compiled under such a name, not inside Jupyter.
- Tested on Linux only.

## Measuring the cost

```bash
uv run python bench/run.py --runs 20
```

`bench/run.py` runs two fixed workloads (`bench/workloads/cpu_bound.py`: calls of small pure-Python functions; `bench/workloads/io_bound.py`: a SQLite file and small files), each N times without Ormaos and N times with each capability set: context only, calls and exceptions, the same with `--path`, the same with I/O (the default of `ormaos run`). It times the whole command and writes `bench/RESULTS.md`: one table per workload with the median, the spread (p25-p75 and min-max), what one run recorded, and the machine, the OS and the Python version. `--out FILE` writes somewhere else. Linux and macOS.

## Requirements

Python 3.12 or newer. The `ormaos` package uses only the standard library. Nothing leaves your machine: no AI, no network, no telemetry.

## Development

```bash
uv sync
uv run ormaos --version
uv run ormaos run -- python -c "print(1)"
uv run ruff format . && uv run ruff check .
uv run mypy --strict src
uv run pytest
```

## License

Apache 2.0. See [LICENSE](LICENSE).
