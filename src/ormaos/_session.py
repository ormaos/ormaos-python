"""``with ormaos.record():`` records a block of a script or of a notebook.

It makes a recording in ``.ormaos/`` like ``ormaos run`` does, for the code
that runs inside the block in this process, and for the Python processes that
are started inside the block. Read it with the same verbs (``ormaos show``).

Fail open: if the recording cannot start, the block runs unrecorded, and
nothing is raised. An exception of the block always goes through unchanged.
"""

import os
import sys
import time
from types import TracebackType
from typing import Any, Literal

_ENV = ("ORMAOS_DIR", "ORMAOS_PATH", "ORMAOS_IO")
# The one thing a block changes in the program it records. It is said in
# every recording of a block.
ENVIRONMENT_NOTE = {
    "what": "the block as it runs without ormaos: its environment is changed",
    "reason": "`with ormaos.record():` sets ORMAOS_DIR, ORMAOS_PATH and ORMAOS_IO "
    "in os.environ while the block runs (the Python processes started inside "
    "find the recording through them); when the block ends, each is put back "
    "to the value the program had, or removed if it had none",
}


def _say(message: str) -> None:
    try:
        print("ormaos: " + message, file=sys.stderr, flush=True)
    except Exception:
        pass


class Session:
    """What ``ormaos.record()`` returns. After the block starts, ``name`` is
    the recording (``r3``) and ``directory`` its folder; both are ``None``
    when nothing is being recorded by this block."""

    def __init__(
        self,
        path: tuple[str, ...] | list[str] | str = (),
        io: bool = True,
        summary: bool = False,
        root: str | None = None,
    ) -> None:
        self._scopes = [path] if isinstance(path, str) else list(path)
        self._io = io
        self._summary = summary
        self._root = root
        self._recorder: Any = None
        self._env: dict[str, str | None] = {}
        self._started = 0
        self._times: Any = None
        self._cwd = ""
        self.name: str | None = None
        self.directory: str | None = None
        self.reason: str | None = None  # why nothing is recorded by this block

    def __enter__(self) -> "Session":
        try:
            self._start()
        except Exception as err:
            self.reason = f"cannot start ({type(err).__name__})"
            self._restore_env()
            self._recorder = None
            self.name = self.directory = None
        return self

    def _start(self) -> None:
        if not hasattr(sys, "monitoring"):
            self.reason = "this Python has no sys.monitoring"
            return
        from ormaos import _recorder

        active = _recorder.ACTIVE
        if active is not None:
            # Already recording (under `ormaos run`, or inside another block):
            # this block is part of that recording.
            self.reason = "already recording"
            self.directory = active._dir
            self.name = os.path.basename(active._dir)
            return
        from ormaos.recording import ROOT
        from ormaos.runner import append, new_recording, run_header

        self._cwd = os.getcwd()
        directory = os.path.abspath(new_recording(self._root or ROOT))
        command = [sys.executable]
        target = _recorder._argv_shape().get("target")
        if target:
            command.append(str(target))
        run_file = os.path.join(directory, "run.jsonl")
        append(run_file, run_header(command, self._cwd, sorted(os.environ), "record"))
        append(run_file, {"k": "not_collected", **ENVIRONMENT_NOTE})
        # The Python processes started inside the block find the recording
        # through the environment, as under `ormaos run`. It is put back when
        # the block ends.
        self._env = {name: os.environ.get(name) for name in _ENV}
        os.environ["ORMAOS_DIR"] = directory
        os.environ["ORMAOS_PATH"] = ",".join(self._scopes)
        os.environ["ORMAOS_IO"] = "1" if self._io else "0"
        self._started = time.perf_counter_ns()
        self._times = os.times()
        recorder = _recorder.Recorder(
            directory, scopes=self._scopes, io=self._io, started_by="record"
        )
        recorder.start()
        self._recorder = recorder
        self.directory = directory
        self.name = os.path.basename(directory)

    def _restore_env(self) -> None:
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._env = {}

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        recorder, self._recorder = self._recorder, None
        if recorder is None:
            return False
        try:
            if exc is not None:
                recorder.block_left(exc)
            recorder.finish()
            import atexit

            atexit.unregister(recorder.finish)
        except Exception:
            pass
        try:
            self._restore_env()
            from ormaos.runner import append

            after = os.times()
            end: dict[str, Any] = {
                "k": "end",
                "exit_code": None,
                "signal": None,
                "dur": time.perf_counter_ns() - self._started,
                "cpu_user": after.user - self._times.user,
                "cpu_system": after.system - self._times.system,
                "raised": exc_type is not None,
            }
            assert self.directory is not None
            append(os.path.join(self.directory, "run.jsonl"), end)
            if self._summary:
                from ormaos.recording import load
                from ormaos.summary import summarize

                for line in summarize(load(self.directory), self._cwd):
                    print(line, file=sys.stderr)
        except Exception as err:
            _say(f"could not finish the recording ({type(err).__name__})")
        return False
