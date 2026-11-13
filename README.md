# ormaos

Ormaos records and shows what happened inside your Python execution — the runtime context for your agent and for you.

You put `ormaos run --` in front of a command. The program runs unchanged, and a recording of what it did is saved next to it: the calls of your code, every exception (also the ones that were caught and never seen), the I/O, and what could not be recorded. A small command line reads the recording, one short answer at a time.

It records shapes, never values: types, lengths and structure, not the content of an argument, an environment variable or an exception message. Nothing leaves your machine: no AI, no network, no telemetry. The package uses only the Python standard library.

## Install

Python 3.12 or newer. Ormaos must be installed in the environment of the Python that runs your program: the Python that runs it must be able to `import ormaos`.

```bash
pip install git+https://github.com/ormaos/ormaos-python
```

## The demo in 3 commands

[`order-sync`](https://github.com/ormaos/ormaos-examples/tree/main/order-sync) is a job that applies orders to an inventory. It prints `sync finished: OK`.

```bash
git clone https://github.com/ormaos/ormaos-examples && cd ormaos-examples/order-sync
python3 -m venv .venv && .venv/bin/pip install git+https://github.com/ormaos/ormaos-python
./reset.sh && .venv/bin/ormaos run -- .venv/bin/python job.py
```

The job's own output comes first, unchanged. Then, on stderr, at most 5 lines, each starting with `ormaos:` (`…` stands for the duration of the run):

```
sync finished: OK
ormaos: r1 exit 0 in …s: 1 Python process, 322 calls, 7 exceptions (6 swallowed), 182 io
ormaos: swallowed 6x validate.SkuFormatError at job.py:57, raised at validate.py:24 [r1:e1]
ormaos: not collected: io: reads of the 1 file opened at fetch.py:26: opened for reading, and no `read` call by Python code was seen on a file of that name: if it was read, that was inside C code (a file that is iterated, `for line in f`, `csv.reader`)
ormaos: saved in .ormaos/r1, next: ormaos exceptions r1:e1 | ormaos tree r1:c1
```

The last line gives the next commands. The first one:

```
$ .venv/bin/ormaos exceptions r1:e1
r1:e1 validate.SkuFormatError: swallowed
  raised at   validate.py:24 in check_sku  [r1:c168]
  left calls   check_sku [r1:c168], check_order [r1:c167]
  handled at  job.py:57 in sync_orders  [r1:c130]  and not raised again
  message     not recorded; args: tuple[1 str]
  same type, same handler: 6 in all: r1:e1 r1:e2 r1:e3 r1:e4 r1:e5 ...
next: ormaos tree r1:c130 | ormaos io r1:c130 | ormaos tree r1:c168 | ormaos exceptions r1:e2 | ormaos path r1:e1
```

## What is recorded

The recording is a folder, `.ormaos/r1/`, in the directory where you ran the command. It holds JSON lines (see "The files").

- **context**: the command line (names only, see below), the names of the environment variables, the exit code, duration, CPU and memory, and every Python process the command started. Python subprocesses and `multiprocessing` children are in the same recording, each linked to the process that started it.
- **calls**: the call tree of your code, with the shape of the arguments and of the result (`orders: list[36 parse.Order]`), the file and line, and how long each call took.
- **exceptions**: every exception that passes through your code, where it was raised and where it was handled. One that entered an `except` and was not raised again is marked `swallowed`.
- **io**: sqlite3 statements, files opened, read and written, subprocesses started and HTTP requests, each with the line of your code that caused it.
- **path**: line by line, only inside a scope you ask for with `--path`.
- **not_collected**: what this run could not record, with the reason, said when it happens.

Every call, exception and I/O event says which process, which thread and, inside asyncio, which task it ran in. They are small numbers that belong to one recording (`process 2`, `thread 3`, `task 5`).

**The command line** is recorded by name: the executable, a Python interpreter further along the command, the script path or the module after `-m`, and the names of options. Every other word is a value and is kept as its shape, `<str[N]>` for a string of N characters. So is the program text after `-c`.

```
$ ormaos run -- python job.py --token abc --mode=fast
$ ormaos show
...
command: python job.py --token <str[3]> --mode=<str[4]>
```

## What is not recorded

- **Values.** Arguments, results, exception messages, the values of environment variables, the values on the command line, SQL parameters and literals, file contents, request headers and bodies, URL query strings.
- **Calls inside the standard library and installed packages.** "Your code" is every file outside the standard library and outside `site-packages` (a path that contains `site-packages/` counts as a library). Set `ORMAOS_ONLY` to a list of paths, separated like `PATH`, to narrow it.
- **Code compiled from a string under a made-up name.** Code from `compile` or `exec` is yours when its file name is `<string>`, `<stdin>` or a notebook cell.
- **Code of your program that runs in the middle of the recorder.** A signal handler or a `__del__` that Python runs while Ormaos is noting an event is not recorded.
- **Exceptions that never pass through your code.**
- **Lines**, outside a `--path` scope.

`ormaos show --json` lists every kind of thing that is never recorded, with the reason, next to the notes of that run.

Names are recorded: file paths, the names of functions, classes and exception types, table names, host names, the names of programs. A name that your program builds from data (a class made with `type(name, ...)`, a file named after a customer) is recorded as it is.

## Reading a recording

Everything in a recording has a short address: `r1` is the recording, `r1:c7` a call, `r1:e1` an exception, `r1:i3` an I/O event. `last` is the newest recording, and `last:e1` works too. An address belongs to one recording. Every answer is short and ends with `next:`, the commands for the next question.

```bash
ormaos show [r1]            # the overview: counts, swallowed and uncaught exceptions, I/O by kind
ormaos exceptions r1        # the exceptions of a recording or a call, swallowed first
ormaos exceptions r1:e1     # one exception: where it was raised, the calls it left, where it was handled
ormaos tree r1:c130         # the calls below a call, or the top of a recording
ormaos io r1:c130           # the I/O of a call and of the calls below it, by line of code
ormaos path r1:e1           # the lines a call ran, in order (needs --path, see below)
ormaos diff r1 r3           # what r3 did differently from r1
ormaos list                 # the recordings, newest first
ormaos status               # what works here, and what is being recorded now
```

- `tree`, `exceptions`, `path` and `io` take a recording, a call or an exception. An exception stands for the call that handled it. `show` takes a recording.
- Every answer is bounded. `--limit N` asks for more lines and `--offset N` skips entries you have seen, on `tree`, `exceptions`, `path`, `io` and `list` (`diff` has `--limit`). `tree` also takes `--depth N`, `io` takes `--kind sqlite|file|subprocess|http`.
- `--json` on every reading verb and on `guide` prints one JSON document on stdout, also for errors (`"kind": "error"`). Every document has `"kind"`, `"version"`, `"next"` (addresses) and `"next_commands"`. `ormaos run` has `--json-out FILE` instead, which writes the overview to a file: the JSON is never mixed with your program's output.
- An option is written in full: `--lim` is not `--limit`.

**The status of an exception:** `swallowed` (it entered an `except` and was not raised again; "handled at" is the line of that `except`), `uncaught` (it ended the program), `replaced` (another one was raised from its handler), `exit` (a `SystemExit` that ended the program; one that your program catches is `swallowed` like any other), `ended at await` (see below), or `no handler seen`.

### The calls: `ormaos tree`

```
$ ormaos tree r1:c130 --depth 1 --limit 4
inside: r1:c1 <module> > r1:c16 main
r1:c130 sync_orders(conn: sqlite3.Connection, orders: list[36 parse.Order]) -> int  job.py:49  …ms  io 180  [swallowed 6x validate.SkuFormatError: r1:e1]  calls below: 66
  r1:c131 check_order(order: parse.Order) -> NoneType  validate.py:47  …us  calls below: 3
  r1:c135 apply_order(conn: sqlite3.Connection, order: parse.Order) -> bool  inventory.py:78  …us  io 6  calls below: 1
  r1:c137 check_order(order: parse.Order) -> NoneType  validate.py:47  …us  calls below: 3
... 63 of the 66 calls directly below r1:c130 are not shown (34 check_order, 29 apply_order; 6 raised): --offset 3
next: ormaos exceptions r1:e1 | ormaos tree r1:c130 --offset 3 | ormaos tree r1:c131 | ormaos tree r1:c16 | ormaos exceptions r1:c130
```

When a list, tuple, set or dict has more than 50 items, its length is exact and the types are those of its first 50 items; the tree says `list[1000 int in the first 50]`, and `not_collected` says that a container was sampled.

### Line by line: `--path`

Lines are recorded only where you ask:

```bash
ormaos run --path job.py:sync_orders -- python job.py   # also: --path job.py, --path sync_orders
ormaos path last                                        # the functions that have lines
ormaos path r2:e1                                       # the handler of r2:e1, around the moment it caught it
```

```
$ ormaos path r2:e1 --limit 12
r2:c130 sync_orders job.py:49: 261 steps, showing 42-53
  42        call apply_order [r2:c165] returned
  43    56                  applied += 1
  44    52      for order in orders:
  45    53          try:
  46    54              validate.check_order(order)
  47        call check_order [r2:c167] raised
  48        exception validate.SkuFormatError [r2:e1] enters a handler here (swallowed)
  49    57          except Exception as exc:
  50    58              log.warning("order %s skipped: %s", order.order_id, exc)
  51    59              continue
  52    52      for order in orders:
  53    53          try:
... 208 more steps: --offset 53
next: ormaos exceptions r2:e1 | ormaos path r2:c130 --offset 53 | ormaos tree r2:c167 | ormaos tree r2:c130
```

The text of each line is read from your file when you ask; it is not in the recording. `path` has the lines of the functions inside the scope, not of the functions they call (those appear as calls).

### I/O: `ormaos io`

```
$ ormaos io r1:e1
r1:e1 sync_orders and the calls below it: 180 io events (sqlite 180)
  30x sqlite SELECT on_hand, pack_size FROM stock WHERE sku = ?  at inventory.py:87 in apply_order  [r1:i3]
  30x sqlite SELECT ? FROM applied_orders WHERE order_id = ?  at inventory.py:92 in apply_order  [r1:i4]
  30x sqlite BEGIN  at inventory.py:101 in apply_order  [r1:i5]
  30x sqlite UPDATE stock SET on_hand = on_hand - ? WHERE sku = ?  at inventory.py:101 in apply_order  [r1:i6]
  30x sqlite INSERT INTO applied_orders (order_id, sku, quantity, applied_at) VALU...  at inventory.py:105 in apply_order  [r1:i7]
  30x sqlite COMMIT  at inventory.py:100 in apply_order  [r1:i8]
calls directly below: 36 check_order (6 raised, no io), 30 apply_order (180 io)
next: ormaos io r1:i3 | ormaos io r1:i4 | ormaos tree r1:c130 | ormaos io r1:c16
```

The I/O is grouped by what was done and by the line of your code that caused it, with a count. `ormaos io r1:i7` shows one event. `ormaos run --no-io` records without I/O.

| Kind | Recorded | Never recorded |
|---|---|---|
| `sqlite` | the connection (the path of the database), each statement with every literal replaced by `?`, the table | parameters, literals, rows |
| `file` | `open` with the path and the mode; `read` and `write` when Python code calls them on the file | contents, sizes |
| `subprocess` | a program started with `subprocess` or `os.exec*`/`os.posix_spawn`: its name and how many arguments. `os.system` is recorded as `system shell` and `os.fork` as `fork`, without a program name | arguments, environment, output, the shell command |
| `http` | method, scheme, host, port, path, status and duration, for `http.client` (so also `urllib`, `requests`, `urllib3`) and for `httpx` | query string, user name and password of the URL, `;parameters`, fragment, headers, bodies |

How it is observed. Ormaos replaces and wraps nothing: `open` is still `open`, a connection is still a `sqlite3.Connection`.

- Files, subprocesses, `sqlite3.connect` and the request line of `http.client` are seen through Python's audit events (`sys.addaudithook`). An audit hook cannot be removed; when recording stops it stays and does nothing.
- `read` and `write` on a file, and the status of an HTTP response, are seen through `sys.monitoring`. `requests` and `httpx` are never imported by Ormaos: their code is recognised when your program runs it.
- sqlite3 has no event for statements, so Ormaos sets the trace callback of each connection. See "Limitations".

### Two recordings: `ormaos diff`

```
$ ormaos diff r1 r3
r1 -> r3, matched by place in the code and order, not by address
  r1: exit 0, 322 calls, 7 exceptions, 182 io events
  r3: exit 0, 346 calls, 1 exception, 218 io events
calls: 6 of 31 places differ
  check_order at validate.py:47: 36 calls in both; 6 raised in r1, none in r3; call 7 raised in r1 and returned in r3  [r1:c167 r3:c167]
  apply_order at inventory.py:78: 30 calls in r1, 36 in r3  [r1:c135 r3:c135]
  ...
exceptions: 1 of 2 groups differ
  swallowed validate.SkuFormatError, raised at validate.py:24, handled at job.py:57: 6 in r1, none in r3  [r1:e1]
io: 6 of 8 groups differ
  sqlite UPDATE stock SET on_hand = on_hand - ? WHERE sku = ?  at inventory.py:101 in apply_order: 30 in r1, 36 in r3  [r1:i6 r3:i6]
  ...
not compared: values, shapes of arguments and results, durations, cpu and memory, the lines of `path`, which process or thread an event was in
next: ormaos exceptions r1:e1 | ormaos tree r1:c167 | ormaos io r1:i3 | ormaos tree r3:c167 | ormaos io r3:i3
```

An address belongs to one recording, so `diff` never matches by address. It matches a call, an exception or an I/O event by where it is in the code (file, function, line), and events at the same place by order. The answer says what each recording has, with the counts; it does not say which one is right. Two recordings of the same code and the same input give `the same in both` three times.

### The `StopIteration` of an `await`

When a coroutine awaits a future and the future finishes (`await asyncio.gather(...)`, `await future`), Python raises a `StopIteration` inside the coroutine to hand over the result, and takes it itself: no `except` ever sees it. Ormaos records each one, counts them apart and groups them by the line of the `await`:

```
$ ormaos exceptions r1
r1: 3 exceptions (0 swallowed, 0 uncaught, 3 StopIteration ended at await)
3x StopIteration ended at await, at aw.py:11 in main  [r1:e1]
```

An `await` of another coroutine, or of `asyncio.sleep`, produces none. With `--json`, `taken_by_interpreter` has the total, the counts and one group per origin with the address of every occurrence.

### What is there: `ormaos list` and `ormaos status`

```
$ ormaos list
1 recording in .ormaos, newest first
r1  2026-11-13 14:38:30  exit 0  …ms  1 process, 322 calls, 7 exceptions (6 swallowed), 182 io  .venv/bin/python job.py
next: ormaos show r1
```

`ormaos status` says the version, whether this Python can record, whether the command itself runs inside a recording, how many recordings there are, the last one, the ones still running and the ones left incomplete.

## In a script or a notebook: `with ormaos.record():`

```python
import ormaos

with ormaos.record():
    main()
```

The block is recorded into `.ormaos/` like a run, with the Python processes started inside it, and is read with the same commands. Only what runs inside the block is recorded.

- Options: `path="job.py:sync_orders"` (one scope, or a list), `io=False`, `summary=True` (the short summary on stderr when the block ends; nothing is printed without it), `root="some/folder"` (the recordings go to that folder instead of `./.ormaos`; the reading commands only read `./.ormaos`).
- The object it gives has `name` (`r3`), `directory` and `reason` (why nothing is recorded, when that is the case).
- If the recording cannot start, the block runs unrecorded and nothing is raised or printed. An exception of the block goes through unchanged.
- Under `ormaos run`, the block is part of that recording.
- In a Jupyter notebook the cells are your code. It runs in a real kernel in the compatibility matrix.

## For coding agents: `ormaos guide`

```bash
ormaos guide                               # the guide, as text (or --json)
ormaos guide --agents-md                   # one line to put in the AGENTS.md of your project
ormaos guide --agents-md --write           # shows the change as a diff, then adds the line to AGENTS.md
ormaos guide --agents-md --write FILE      # the same, for another file
```

The guide is short and written for a coding agent: when to use Ormaos, where to start, how to follow addresses, what is not recorded. Without `--write`, no file is touched. [skills/ormaos/SKILL.md](skills/ormaos/SKILL.md) is the same guidance as a skill: copy the folder `skills/ormaos` into the skills folder of your agent.

## A process that ends early

The recorder writes while the program runs: after every 256 records, when the oldest waiting record is 0.2 seconds old (checked when a record arrives), and when a call that has no call of your code above it starts or ends. Each write ends with a checkpoint: the calls that have not returned, the exceptions whose end is not known, and whether anything is still unwritten.

So a recording can be read while the program is still running, and after `kill -9`. `ormaos show` then marks it `(incomplete)`, the calls that had not returned are shown as `-> open`, and `not_collected` says which process may miss its last events.

A recording is incomplete when a process ended without writing all its events, whoever ended it. A pool worker that its pool ends while it waits for a task lost nothing, and the recording is complete; a worker ended in the middle of a task did. For every process that a signal ended, `ormaos show` says which signal and who sent it:

- `was ended by its pool with signal 15`
- `was ended by the program (process 1) with signal 15` (`terminate()` or `kill()` of `multiprocessing` or `subprocess`, or `os.kill`)
- `was ended by signal 9, which the program was not seen sending (from outside, or by a way that is not watched)`

followed by `nothing left to write` or `events not written`. When nothing saw the signal, it says that no signal was seen.

## The files

A recording is `.ormaos/r<N>/`, with `run.jsonl` (written by `ormaos run` or by the block) and one `p<pid>-<time>.jsonl` per Python process. `.ormaos/` has its own `.gitignore`. The format is provisional and will change: every file starts with a line that says `"format": "provisional-0"`.

- The first line of a process file has two parts. `core` is what a recorder of any language would write: the process (`pid`, `ppid`, `cwd`), the `capabilities` that were recorded (`context`, `calls`, `exceptions`, `path`, `io`), what is never recorded, and `anchor`. `python` is what only this recorder knows: the version, the executable, how Python was started, whether the process was forked, the path scope.
- The first line of `run.jsonl` has only `core`: the command (by name and shape), the working directory, the names of the environment variables, the platform, and `anchor`.
- `anchor` ties the times of the events to the wall clock: `{"t": 0, "unix": ..., "clock": "monotonic, nanoseconds"}`. Event times (`t`) are nanoseconds on a monotonic clock, and the anchor says which moment `t = 0` was.

## Compatibility

[docs/compatibility.md](docs/compatibility.md) has one small example per row, run in CI: pytest, Flask, FastAPI, Django, a Click command line, pandas, `with ormaos.record():` in a script, a Jupyter notebook, and coverage.py running at the same time. Each row says "works" or "works, with these limits".

## Limitations

What Ormaos does to your program:

- **It takes one `sys.monitoring` tool id.** Ormaos uses tool id 4, or 3 when 4 is taken. A program that asks for that same id gets `ValueError: tool 4 is already in use`, and a program that takes all six ids cannot run under Ormaos. The ids of debuggers, coverage tools and profilers (0, 1, 2, 5) are free, and `sys.settrace` and `sys.setprofile` work as always. If your program turns Ormaos's tool off, recording stops there and `not_collected` says so.
- **It sets the sqlite3 trace callback.** sqlite3 has no event for statements, so Ormaos sets the trace callback of each connection as soon as the connection is made: this is the one place where it sets something on an object of your program. If your program sets or clears the trace callback itself, your choice stays and works as always; the statements of that connection are not recorded after that, and `not_collected` names the connection and the line (when the call is made by Python code). If your program had already set a callback when Ormaos comes to set its own, yours is kept and called after Ormaos's, with the same statement; in that one case your callback has one more frame above it on the stack.
- **`with ormaos.record():` changes the environment during the block.** It sets `ORMAOS_DIR`, `ORMAOS_PATH` and `ORMAOS_IO` in `os.environ` (that is how child processes find the recording), and when the block ends it puts each back to the value your program had, or removes it if it had none. Every recording of a block says so in `not_collected`.
- **Your program can see Ormaos.** `.ormaos/` is created in the current directory. Under `ormaos run`, `ORMAOS_DIR` is in the environment, and a few modules (`ormaos` and some of the standard library) are already imported when your program starts.
- **`ormaos run` waits a moment at the end.** After the command has ended, it waits up to one second for Python processes of the recording that are still alive, before it prints the summary.
- **A signal by exit code.** When the command is ended by a signal, `ormaos run` exits with 128 plus the signal number, as a shell reports it.

The cost:

- **Recording adds work to every call of your code.** A program that makes very many small calls runs many times slower while it is recorded, and its recording is large. A program that spends its time in I/O or inside libraries is affected much less. See "Overhead".
- **Limits per process.** At most 200,000 calls, 50,000 exceptions, 200,000 lines and 100,000 I/O events are kept per process (`ORMAOS_MAX_CALLS`, `ORMAOS_MAX_EXCEPTIONS`, `ORMAOS_MAX_LINES`, `ORMAOS_MAX_IO`). What comes after is listed in `not_collected`. The work of noting an event is still done after a limit is reached.
- **Reading a large recording.** `show`, `list`, `status` and the summary read a recording line by line and keep no call in memory. `tree`, `exceptions`, `io`, `path` and `diff` load the whole recording.

What a recording can miss (each is said in `not_collected` when it happens, unless noted):

- A Python that the command starts without `ormaos` installed, without its site packages (`-S`), or without `ORMAOS_DIR` in its environment is not recorded. When the recorded program started it with `subprocess`, the recording says how many are missing. When no Python at all was recorded, it says that.
- A process that is killed loses the events it had not written yet: at most 256 records, or what arrived in its last 0.2 seconds.
- A thread that was already running when a `with ormaos.record():` block started: only the calls it starts inside the block are recorded, with nothing above them. The function that contains the block is not a call of the recording.
- At the recursion limit, the recorder has no room to note an event: calls and exceptions around that point can be missing or lack their end. The recording says how often.
- A file that is iterated (`for line in f`, `csv.reader(f)`) or written by `print(..., file=f)` is read or written inside C code, so only its `open` is seen. A file that was opened and that no call was seen reading (or writing) is listed, with the line that opened it; this goes by file name, so a file read once through `read()` and once by iteration is not listed.
- Sockets, directory and file-system operations (`os.listdir`, `os.mkdir`, `os.remove`, ...) and `os.read`/`os.write` are not recorded; each line of your code that does one is listed, with a count.
- I/O that no line of your code is waiting for is not recorded; it is counted in one line.
- A path or a program name longer than 300 characters is cut.
- Other databases, and anything a C library does by itself, are never seen (not listed).

What a recording can say imprecisely:

- For `except*` the handler line is the first clause of the `try`; which clauses took a part of the group is not recorded.
- An exception that is kept and raised again later (`raise saved`) is reported at the line where it was first raised.
- Exceptions are told apart by their place in memory while they are in flight. Two exceptions of the same type, the second created exactly where the first one just left, can be read as one.
- Who sent a signal is known only for signals sent through `multiprocessing`, `subprocess` or `os.kill`, and the signal itself only for the command of `ormaos run` and for children that their parent waited for.
- In SQL, a double-quoted word after `ON`, `AS`, `FROM`, `JOIN` and similar, or next to a dot, is kept as a name. SQLite reads such a word as a string when no column has that name, so a string written that way is recorded.
- A secret that is part of a URL path (`/reset/<token>`) is recorded with the path.
- On the command line, every word that is not an option, a script or a module is hidden, also a harmless one such as a subcommand.

Tested on Linux only.

## Overhead

Measurements are coming.

## Not yet

- A package on PyPI. Install from GitHub.
- A stable file format. Today's is `provisional-0`.
- Windows and macOS: not tested.
- Reading commands for a folder other than `./.ormaos`.
- Line-by-line reading for `tree`, `exceptions`, `io`, `path` and `diff`.
- A way to know which signal ended a process that nothing waited for.
- Web servers that start worker processes (uvicorn, gunicorn) in the compatibility matrix.

## Development

```bash
uv sync
uv run ormaos --version
uv run ruff format . && uv run ruff check .
uv run mypy --strict src
uv run pytest
uv sync --group compat && uv run --group compat pytest tests/compat   # the compatibility matrix
```

## License

Apache 2.0. See [LICENSE](LICENSE).
