"""``ormaos run -- <command>``: run a command and record its Python processes.

The command always runs, with its own stdin, stdout and stderr, and its exit
code is always returned. If anything on our side fails, we say so in one line
on stderr and get out of the way.
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from types import FrameType
from typing import Any

from ormaos import __version__
from ormaos.recording import FORMAT, ROOT, load, recording_numbers
from ormaos.show import document
from ormaos.summary import PREFIX, summarize


def _say(message: str) -> None:
    try:
        print(PREFIX + message, file=sys.stderr, flush=True)
    except Exception:
        pass


def _append(path: str, record: dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def _new_recording(root: str) -> str:
    """Create ``<root>/r<N>`` with the next free number and return its path."""
    os.makedirs(root, exist_ok=True)
    ignore = os.path.join(root, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as handle:
            handle.write("# Recordings stay on this machine.\n*\n")
    number = max(recording_numbers(root), default=0) + 1
    while True:
        path = os.path.join(root, f"r{number}")
        try:
            os.mkdir(path)
            return path
        except FileExistsError:
            number += 1


def _exit_code(returncode: int) -> tuple[int, int | None]:
    """The exit code a shell would report, and the signal if one ended it."""
    if returncode < 0:
        return 128 - returncode, -returncode
    return returncode, None


def _wait(proc: subprocess.Popen[bytes]) -> int:
    def forward(signum: int, _frame: FrameType | None) -> None:
        try:
            proc.send_signal(signum)
        except OSError:
            pass

    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                previous[signum] = signal.signal(signum, forward)
            except (OSError, ValueError):
                pass
    try:
        while True:
            try:
                return proc.wait()
            except KeyboardInterrupt:
                continue  # the command got the same Ctrl-C; wait for it
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run(command: Sequence[str], json_out: str | None = None) -> int:
    cwd = os.getcwd()
    env = dict(os.environ)
    env.pop("ORMAOS_DIR", None)
    directory: str | None = None
    run_file = ""
    try:
        directory = os.path.abspath(_new_recording(ROOT))
        run_file = os.path.join(directory, "run.jsonl")
        _append(
            run_file,
            {
                "format": FORMAT,
                "kind": "run",
                "ormaos": __version__,
                "command": list(command),
                "cwd": cwd,
                "env_names": sorted(env),
                "platform": sys.platform,
                "started": time.time(),
            },
        )
        env["ORMAOS_DIR"] = directory
    except Exception as err:
        directory = None
        env.pop("ORMAOS_DIR", None)
        _say(f"not recording: cannot write to {ROOT} ({type(err).__name__})")

    before = os.times()
    started = time.perf_counter_ns()
    try:
        proc = subprocess.Popen(list(command), env=env)
    except OSError as err:
        code = 127 if isinstance(err, FileNotFoundError) else 126
        _say(f"cannot start {command[0]!r} ({type(err).__name__})")
        if directory:
            try:
                _append(
                    run_file,
                    {
                        "k": "not_collected",
                        "what": "everything",
                        "reason": f"the command could not be started "
                        f"({type(err).__name__})",
                    },
                )
                _append(run_file, {"k": "end", "exit_code": code, "signal": None})
            except Exception:
                pass
        return code

    code, signum = _exit_code(_wait(proc))

    if directory:
        try:
            after = os.times()
            end: dict[str, Any] = {
                "k": "end",
                "exit_code": code,
                "signal": signum,
                "dur": time.perf_counter_ns() - started,
                "cpu_user": after.children_user - before.children_user,
                "cpu_system": after.children_system - before.children_system,
            }
            try:
                import resource

                rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
                end["max_rss_kb"] = rss // 1024 if sys.platform == "darwin" else rss
            except Exception:
                pass
            _append(run_file, end)
            recording = load(directory)
            for line in summarize(recording, cwd):
                print(line, file=sys.stderr)
            sys.stderr.flush()
        except Exception as err:
            _say(f"could not summarize the recording ({type(err).__name__})")
            recording = None
        if json_out and recording is not None:
            try:
                with open(json_out, "w", encoding="utf-8") as handle:
                    json.dump(document(recording, cwd), handle, indent=2)
                    handle.write("\n")
            except Exception as err:
                _say(f"could not write {json_out} ({type(err).__name__})")
    elif json_out:
        _say(f"{json_out} not written: there is no recording")
    return code
