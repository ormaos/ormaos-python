# ormaos

Ormaos records what happened inside a Python execution and hands it to the developer and to their coding agent.

> Status: being built for Hack Brasil 2026. `ormaos run` records calls, exceptions, I/O and, inside a scope you ask for, the lines that ran. `ormaos show`, `tree`, `exceptions`, `path` and `io` read a recording. `ormaos guide` tells a coding agent how to use it.

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

- `context`: the command, the names of the environment variables (never the values), duration, CPU, memory, threads, and one file per Python process.
- `calls`: the call tree of your code, with the shape of the arguments and of the return value.
- `exceptions`: every exception that passes through your code, where it was raised and where it was handled, marked `swallowed` when it was handled and not raised again.
- `io`: sqlite3 statements, files opened, read and written, subprocesses started and HTTP requests, each with the line of your code that caused it (see below).
- `path`: line by line, only inside a scope you ask for with `--path` (see below).
- `not_collected`: everything that was not recorded, with the reason.

## Reading a recording

Everything in a recording has a short address: `r1` is the recording, `r1:c7` a call, `r1:e1` an exception, `r1:i3` an I/O event. Each answer is short and ends with a `next:` line: the commands for the next question.

```bash
ormaos show                # the overview of the last recording (or: ormaos show r1)
ormaos exceptions r1       # its exceptions, swallowed first
ormaos exceptions r1:e1    # one exception: where raised, the calls it left, where handled
ormaos tree r1:c7          # the calls below a call: shapes, results, durations
ormaos path r1:c7          # the lines that call ran, in order
ormaos io r1:c7            # the I/O of that call and of the calls below it
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

Limits of `io`, also listed in `not_collected` of every recording: a file that is iterated (`for line in f`, `csv.reader(f)`) is read inside C code, so only its `open` is seen; an `open` is recorded when it is asked for, also when it then fails (the exception is recorded too); sockets, other databases, directory operations and `os.read`/`os.write` are not recorded; I/O that no line of your code is waiting for (the import system, library threads) is not recorded. A quoted name in SQL (`"stock"`) is kept only where a name is expected (after `FROM`, `INTO`, `UPDATE`, `JOIN`, ..., or next to a dot); anywhere else it is shown as `?`.

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
- The handler line is the `except` clause that matched. Two rare cases may show an earlier clause of the same `try`: `except*`, and a clause whose types span several lines.
- Recording costs time on every call, and the files are large for long runs. At most 200,000 calls, 50,000 exceptions and 200,000 lines are kept per process (`ORMAOS_MAX_CALLS`, `ORMAOS_MAX_EXCEPTIONS`, `ORMAOS_MAX_LINES`); the rest is listed in `not_collected`. The cost is not measured yet.
- `path` has the lines of the functions inside the scope, not of the functions they call (those appear as calls).
- At most 100,000 I/O events are kept per process (`ORMAOS_MAX_IO`). The same event repeated at the same line is one event with a count.
- A process that is killed, or that ends with `os._exit`, loses its last events. The recording says so.
- Tested on Linux only.

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
