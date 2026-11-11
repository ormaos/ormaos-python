# ormaos

Ormaos records what happened inside a Python execution and hands it to the developer and to their coding agent.

> Status: being built for Hack Brasil 2026. `ormaos run` records calls, exceptions and, inside a scope you ask for, the lines that ran. `ormaos show`, `tree`, `exceptions` and `path` read a recording.

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
ormaos: saved in .ormaos/r1, next: r1:e1 r1:c1
```

The recording is in `.ormaos/r1/`, as JSON lines (`"format": "provisional-0"`, it will change):

- `context`: the command, the names of the environment variables (never the values), duration, CPU, memory, threads, and one file per Python process.
- `calls`: the call tree of your code, with the shape of the arguments and of the return value.
- `exceptions`: every exception that passes through your code, where it was raised and where it was handled, marked `swallowed` when it was handled and not raised again.
- `path`: line by line, only inside a scope you ask for with `--path` (see below).
- `not_collected`: everything that was not recorded, with the reason.

## Reading a recording

Everything in a recording has a short address: `r1` is the recording, `r1:c7` a call, `r1:e1` an exception. Each answer is short and ends with a `next:` line: the commands for the next question.

```bash
ormaos show                # the overview of the last recording (or: ormaos show r1)
ormaos exceptions r1       # its exceptions, swallowed first
ormaos exceptions r1:e1    # one exception: where raised, the calls it left, where handled
ormaos tree r1:c7          # the calls below a call: shapes, results, durations
ormaos path r1:c7          # the lines that call ran, in order
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

- `tree`, `exceptions` and `path` take a recording, a call or an exception. An exception stands for the call that handled it.
- Every answer is bounded. `--limit N` asks for more, `--offset N` skips what you have seen; `tree` also takes `--depth N`.
- `--json` on every verb prints one JSON document on stdout, also for errors (`"kind": "error"`). Every document has `"kind"`, `"version"`, `"next"` (addresses) and `"next_commands"`.

### Line by line: `--path`

Lines are recorded only where you ask, because recording every line of a program costs too much:

```bash
ormaos run --path job.py:sync_orders -- python job.py   # also: --path job.py, --path sync_orders
ormaos path last                                        # the functions that have lines
ormaos path r2:e1                                       # the handler of r2:e1, around the moment it caught it
```

`path` shows the lines in the order they ran, with the calls made and the exceptions caught in between. The text of each line is read from your file when you ask, it is not in the recording.

### JSON overview

```bash
ormaos run --json-out overview.json -- python job.py   # written next to the run
ormaos show --json last                                # or r1, r2, ...
```

The JSON goes to the file or to stdout, never mixed with the program's output. It has the context, the counts, the swallowed exceptions grouped by handler (with where they were raised and how many times), the uncaught ones, `not_collected`, and `next`: the addresses to ask about next.

Shape always, value never: Ormaos records types, lengths and structure (`{"type": "str", "len": 14}`), never the content of an argument, an environment variable or an exception message.

## Limits today

- The Python that the command starts must have `ormaos` installed in its environment. If not, the command still runs and the summary says that no Python process was recorded.
- "Your code" is every file outside the standard library and outside `site-packages`. Set `ORMAOS_ONLY` to a list of paths to narrow it.
- The handler line is the `except` clause that matched. Two rare cases may show an earlier clause of the same `try`: `except*`, and a clause whose types span several lines.
- Recording costs time on every call, and the files are large for long runs. At most 200,000 calls, 50,000 exceptions and 200,000 lines are kept per process (`ORMAOS_MAX_CALLS`, `ORMAOS_MAX_EXCEPTIONS`, `ORMAOS_MAX_LINES`); the rest is listed in `not_collected`. The cost is not measured yet.
- `path` has the lines of the functions inside the scope, not of the functions they call (those appear as calls).
- Database, file, subprocess and HTTP activity is not recorded yet.
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
