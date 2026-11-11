# AGENTS.md — ormaos-python

## What this repository is

Ormaos records what happened inside a Python execution and hands it to the developer and to their coding agent. This repository holds the recorder and the CLI, in one package named `ormaos`. It is being built for the Hack Brasil 2026 hackathon; the deadline is **Nov 30, 2026, 23:59 (America/Sao_Paulo)**.

Public, Apache 2.0. The demo app lives in the sibling repository `ormaos-examples`.

## The product, in one paragraph

`ormaos run -- <command>` runs any command that starts Python and records the Python processes it creates. The recording has: `context` (command, environment variable names, duration, CPU, memory, threads, processes), `calls` (the call tree of the user's code, with the shape of the arguments), `exceptions` (every exception, where it was raised and where it was handled, marked `swallowed` when handled without re-raise), `path` (line by line, only inside a scope the user asks for) and `io` (database, files, subprocesses, HTTP, each with the user-code line that caused it). The CLI reads recordings: a short summary, then verbs that navigate by address, all with `--json`. A `guide` verb teaches any coding agent how to use it.

## Rules you must never break

- **Fail open.** If Ormaos fails, the user's program runs unchanged. Every callback is guarded; on internal error, recording stops and the reason goes to `not_collected`. There is a test for it.
- **Never change the state of the observed program.** A test runs the demo app with and without Ormaos and compares stdout, stderr and the exit code.
- **Shape always, value never.** Never record argument values, environment variable values, or raw exception messages. Record type, length and structure. A test plants fake secrets and searches every file in `.ormaos/` for them.
- **Describe, never judge.** Output facts ("swallowed 14 times at sync.py:42"), never opinions ("bug", "slow", "wrong").
- **Everything not recorded appears in `not_collected`, with the reason.**
- **No AI, no network, no telemetry inside Ormaos.** Nothing leaves the user's machine.
- **Standard library only** inside the `ormaos` package. Optional libraries (`requests`, `httpx`) are hooked only if installed, imported lazily, never at module top level.
- **No secrets** in any file. `gitleaks` runs in CI.
- **No code from before Nov 4, 2026, 21:00** (hackathon rule 6.1). Write everything new.

## Technical choices (already decided — do not reopen)

- Python 3.12+ (`requires-python = ">=3.12"`), recording with `sys.monitoring` (PEP 669).
- The recorder is enabled in child processes through a `.pth` file or `sitecustomize`, driven by environment variables (`ORMAOS_DIR`, `ORMAOS_PATH`, `ORMAOS_ONLY`).
- The format is **provisional JSONL** in `.ormaos/`, with `"format": "provisional-0"` in every file header. It will be replaced after the hackathon.
- Addresses are short and stable inside one recording (for example `r3:c42` for call 42 of recording 3). Every CLI answer ends with the addresses for the next question.
- Matching across two recordings (`diff`) is by code origin (file, function, line) and order, never by address.
- The summary goes to stderr, at most 5 lines. `--json` output goes to stdout or to `--json-out`, never mixed with the program's output.

## Commands

- Environment: `uv sync` (uv is the tool; `uv run <command>` runs inside the environment)
- Tests: `uv run pytest`
- Lint and format: `uv run ruff format . && uv run ruff check .`
- Types: `uv run mypy --strict src`

## How we work

- `main` is always green. Before every push: `ruff format`, `ruff check`, `mypy --strict`, `pytest`.
- CI runs on Python 3.12, 3.13 and 3.14, plus `gitleaks`.
- Small commits in English, imperative mood ("Add swallowed exception detection").
- Never `push --force`, never rewrite `main`, never publish anywhere (PyPI, releases), never delete outside the repository.
- Keep `README.md` current.
