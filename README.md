# ormaos

Ormaos records what happened inside a Python execution and hands it to the developer and to their coding agent.

> Status: being built for Hack Brasil 2026. `ormaos run` records calls and exceptions. The verbs that navigate a recording are not built yet.

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
- `not_collected`: everything that was not recorded, with the reason.

Shape always, value never: Ormaos records types, lengths and structure (`{"type": "str", "len": 14}`), never the content of an argument, an environment variable or an exception message.

## Limits today

- The Python that the command starts must have `ormaos` installed in its environment. If not, the command still runs and the summary says that no Python process was recorded.
- "Your code" is every file outside the standard library and outside `site-packages`. Set `ORMAOS_ONLY` to a list of paths to narrow it.
- For a `try` with several `except` clauses, the handler line is the first clause.
- Recording costs time on every call, and the files are large for long runs. At most 200,000 calls and 50,000 exceptions are kept per process (`ORMAOS_MAX_CALLS`, `ORMAOS_MAX_EXCEPTIONS`); the rest is listed in `not_collected`. The cost is not measured yet.
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
