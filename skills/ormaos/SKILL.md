---
name: ormaos
description: Record a Python run with Ormaos and read what happened inside it - the calls of the project's code, every exception including the ones caught and not raised again, the SQL statements, files, subprocesses and HTTP requests, and line-by-line paths. Use when a Python program runs but its output does not explain what it did (a job that prints OK, an error that never shows, a result that cannot be traced), or when asked to debug or investigate the runtime behaviour of Python code.
---

# Ormaos

Ormaos records what happened inside a Python execution, and a small CLI reads the recording. Use it to look at what a run did instead of guessing from the source.

It records shapes, never values: types, lengths and structure of arguments and results, and for I/O the names (a path, a table, a host), never the contents.

## Before you start

- `ormaos` must be installed in the environment of the Python that runs the program. Check with `ormaos --version`.
- `ormaos guide` prints the short version of this file. If this file and the guide differ, the guide is right for the installed version.

## Steps

1. **Record.** Put `ormaos run --` in front of the command you would run:

   ```bash
   ormaos run -- python job.py
   ```

   The command runs unchanged, with the same output and the same exit code. Ormaos then prints a summary on stderr: at most 5 lines, each starting with `ormaos:`. The recording is saved in `.ormaos/`.

2. **Read the summary.** It names the recording (`r1`), the counts, the swallowed and the uncaught exceptions, and ends with addresses. `ormaos show` prints the overview again, with more detail.

3. **Follow the `next:` line.** Every answer ends with `next:` and the commands for the next question. Run the one that fits your question, and repeat.

4. **Report what the recording says**, with the addresses and the file and line of each fact, then read the source at those lines.

## Addresses

| Address | What it is |
|---|---|
| `last` | the newest recording |
| `r3` | recording 3 |
| `r3:c42` | a call |
| `r3:e5` | an exception |
| `r3:i7` | an I/O event |

An address belongs to one recording: `r3:c42` and `r4:c42` are not the same call. `last:e5` works too. An exception address given to `tree`, `io` or `path` means the call that handled it.

## Verbs

| Command | Answer |
|---|---|
| `ormaos show [r3]` | Overview: the command, counts, swallowed and uncaught exceptions, I/O by kind, what was not collected. |
| `ormaos exceptions <addr>` | For a recording or a call: its exceptions, swallowed first. For one exception: where it was raised, the calls it left, where it was handled. |
| `ormaos tree <addr>` | The calls below a call, or the top of a recording: shape of arguments and results, file and line, duration, exceptions handled there, I/O count. |
| `ormaos io <addr>` | SQL statements, files, subprocesses and HTTP requests of a recording or a call, grouped by the line of code that caused them, with counts. For one I/O event: where it happened. |
| `ormaos path <addr>` | The lines a call ran, in order, with the calls it made and the exceptions it caught in between. |

Options on every verb: `--json` (one JSON document on stdout, also for errors, with `kind`, `version`, `next` and `next_commands`). On `tree`, `exceptions`, `path` and `io`: `--limit N` and `--offset N`. On `tree`: `--depth N`. On `io`: `--kind sqlite|file|subprocess|http`.

An exception is `swallowed` (it entered an `except` and was not raised again; "handled at" is the line of that `except`), `uncaught` (it ended the program), `replaced` (another one was raised from its handler) or `exit` (a `SystemExit`, as `sys.exit()` raises).

`--limit N` asks for more lines. `--offset N` skips entries already seen; for `tree` these are the calls directly below the address.

## Line by line

Lines are recorded only inside a scope given when recording. When `ormaos path` has no lines for a call, its error message gives the exact command to record them:

```bash
ormaos run --path job.py:sync_orders -- python job.py
ormaos path last
```

A scope is `file.py`, `file.py:function` or `function`. `--path` can be given more than once. Recording again runs the program again, with its side effects: check that this is acceptable before you do it.

## What is not recorded

- Values: arguments, results, exception messages, environment values, SQL parameters and literals, file contents, headers, bodies, URL query strings.
- Calls inside the standard library and installed packages.
- Lines outside a `--path` scope.
- Reads of a file that is iterated (`for line in f`, `csv.reader(f)`): only its `open` is seen.
- What one run could not record is listed as "not collected" in `ormaos show`. `ormaos show --json` has the full list, with the reason for each item.

Recordings stay in `.ormaos/` on the machine. Nothing is sent anywhere.
