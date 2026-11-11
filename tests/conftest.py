import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from ormaos.recording import Recording, load, recording_numbers
from ormaos.summary import PREFIX


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORMAOS_")}
    env.update(extra or {})
    return env


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def summary(self) -> list[str]:
        return [ln for ln in self.stderr.splitlines() if ln.startswith(PREFIX)]

    @property
    def program_stderr(self) -> str:
        lines = self.stderr.splitlines(keepends=True)
        return "".join(ln for ln in lines if not ln.startswith(PREFIX))


@dataclass
class Workdir:
    path: Path

    def write(self, name: str, source: str) -> Path:
        file = self.path / name
        file.write_text(source, encoding="utf-8")
        return file

    def _run(self, argv: list[str], env: dict[str, str] | None) -> Result:
        done = subprocess.run(
            argv,
            cwd=self.path,
            env=clean_env(env),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        return Result(done.returncode, done.stdout, done.stderr)

    def plain(self, *command: str, env: dict[str, str] | None = None) -> Result:
        """Run the command without Ormaos."""
        return self._run(list(command), env)

    def ormaos(self, *command: str, env: dict[str, str] | None = None) -> Result:
        """Run the command through ``ormaos run --``."""
        return self._run([sys.executable, "-m", "ormaos", "run", "--", *command], env)

    def cli(self, *args: str, env: dict[str, str] | None = None) -> Result:
        """Run ``ormaos <args>``."""
        return self._run([sys.executable, "-m", "ormaos", *args], env)

    def python(self, *args: str, env: dict[str, str] | None = None) -> Result:
        return self.ormaos(sys.executable, *args, env=env)

    def last(self) -> Recording:
        root = self.path / ".ormaos"
        number = recording_numbers(str(root))[-1]
        return load(str(root / f"r{number}"))


@pytest.fixture
def work(tmp_path: Path) -> Workdir:
    return Workdir(tmp_path)


@pytest.fixture
def line_of() -> Callable[[str, str], int]:
    """The line number of the source line that carries ``# <mark>``."""

    def find(source: str, mark: str) -> int:
        for number, line in enumerate(source.splitlines(), start=1):
            if line.rstrip().endswith(f"# {mark}"):
                return number
        raise AssertionError(f"no line marked {mark}")

    return find
