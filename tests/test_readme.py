"""The example outputs of the README are the real output of the demo.

The demo `order-sync` is run the way the README shows: a virtual environment
in `.venv` with ormaos in it, `./reset.sh`, then `.venv/bin/ormaos run`. Every
example block of the README that starts with `$ ormaos ...` and reads one of
the demo's recordings is run, and its output must be the one in the README,
except for durations, which the README writes as `…`, and the time of day of
`ormaos list`.

The examples are the output on Python 3.14 (each version builds the demo's
dataclass with its own number of calls), so the test runs only there. The demo
is found like in `test_demo.py`.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import Result, clean_env

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
DEMO_DIR = Path(
    os.environ.get("ORMAOS_DEMO_DIR") or REPO.parent / "ormaos-examples" / "order-sync"
)
DEMO_PYTHON = os.environ.get("ORMAOS_DEMO_PYTHON") or sys.executable

# The three commands of "The demo in 3 commands"; the test runs the last one.
RUN = "./reset.sh && .venv/bin/ormaos run -- .venv/bin/python job.py"
# The other recordings the examples read: r2 with lines, r3 after a fix.
RUN_WITH_PATH = "ormaos run --path job.py:sync_orders -- python job.py"
FIX = ('sku=row["sku"],', 'sku=row["sku"].strip().upper(),')

# The example blocks, by the recordings that must exist when each one is run.
AFTER_R1 = [
    ".venv/bin/ormaos exceptions r1:e1",
    "ormaos list",
    "ormaos tree r1:c130 --depth 1 --limit 4",
    "ormaos io r1:e1",
]
AFTER_R2 = ["ormaos path r2:e1 --limit 12"]
AFTER_R3 = ["ormaos diff r1 r3"]
# Example blocks that show a program other than the demo.
OTHER_PROGRAMS = {
    "ormaos run -- python job.py --token abc --mode=fast",
    "ormaos exceptions r1",
}

DURATION = re.escape("…")
WHEN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def blocks() -> list[list[str]]:
    """The fenced blocks of the README that have no language, as lines."""
    found: list[list[str]] = []
    block: list[str] | None = None
    fence = ""
    for line in README.read_text("utf-8").splitlines():
        if fence:
            if line == "```":
                if block is not None:
                    found.append(block)
                block, fence = None, ""
            elif block is not None:
                block.append(line)
        elif line.startswith("```"):
            fence = line
            block = [] if line == "```" else None
    return found


def examples() -> dict[str, list[str]]:
    """Each `$ command` block: the command, and the output under it."""
    starts = ("$ ormaos ", "$ .venv/bin/ormaos ")
    return {b[0][2:]: b[1:] for b in blocks() if b and b[0].startswith(starts)}


def matches(expected: list[str], real: list[str]) -> bool:
    if len(expected) != len(real):
        return False
    for want, got in zip(expected, real, strict=True):
        pattern = re.escape(WHEN.sub("WHEN", want)).replace(DURATION, "[0-9.]+")
        if not re.fullmatch(pattern.replace("WHEN", WHEN.pattern), got):
            return False
    return True


def same(expected: list[str], real: list[str], what: str) -> None:
    assert matches(expected, real), (
        f"the README's output of {what!r} is not the real output\n"
        + "--- README\n"
        + "\n".join(expected)
        + "\n--- real\n"
        + "\n".join(real)
    )


class Demo:
    def __init__(self, path: Path) -> None:
        self.path = path
        venv = path / ".venv" / "bin"
        self.env = clean_env({"PATH": f"{venv}{os.pathsep}{os.environ['PATH']}"})

    def shell(self, command: str) -> Result:
        done = subprocess.run(
            ["sh", "-c", command],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        return Result(done.returncode, done.stdout, done.stderr)

    def example(self, command: str, expected: list[str]) -> None:
        done = self.shell(command)
        assert done.returncode == 0, done.stdout + done.stderr
        same(expected, done.stdout.splitlines(), command)


def make_venv(target: Path) -> None:
    """A `.venv` whose Python imports the ormaos of DEMO_PYTHON, and its script."""
    subprocess.run(
        [DEMO_PYTHON, "-m", "venv", "--without-pip", str(target)], check=True
    )
    python = target / "bin" / "python"
    ask = "import site, sysconfig; print(sysconfig.get_path('purelib'))"
    own = subprocess.run(
        [python, "-I", "-c", ask], capture_output=True, text=True, check=True
    )
    ask = "import site; print('\\n'.join(site.getsitepackages()))"
    theirs = subprocess.run(
        [DEMO_PYTHON, "-I", "-c", ask], capture_output=True, text=True, check=True
    )
    lines = [
        f"import site; site.addsitedir({folder!r})" for folder in theirs.stdout.split()
    ]
    pth = Path(own.stdout.strip()) / "zz_ormaos_from_test.pth"
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script = target / "bin" / "ormaos"
    script.write_text(
        f"#!{python}\nimport sys\nfrom ormaos.cli import main\nsys.exit(main())\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def demo_python_version() -> tuple[int, int]:
    ask = "import sys; print(*sys.version_info[:2])"
    done = subprocess.run(
        [DEMO_PYTHON, "-c", ask], capture_output=True, text=True, check=True
    )
    major, minor = done.stdout.split()
    return int(major), int(minor)


@pytest.fixture
def demo(tmp_path: Path) -> Demo:
    if not (DEMO_DIR / "job.py").is_file():
        message = f"the demo app is not at {DEMO_DIR}"
        if os.environ.get("ORMAOS_DEMO_REQUIRED"):
            pytest.fail(message)
        pytest.skip(message)
    if demo_python_version() != (3, 14):
        pytest.skip("the README's examples are the output on Python 3.14")
    target = tmp_path / "order-sync"
    ignore = shutil.ignore_patterns(".venv", "data", ".ormaos", "__pycache__")
    shutil.copytree(DEMO_DIR, target, ignore=ignore)
    make_venv(target / ".venv")
    return Demo(target)


def test_the_readme_lists_the_examples_this_test_runs() -> None:
    """A new or renamed example block fails here instead of going unchecked."""
    text = README.read_text("utf-8")
    assert RUN in text and RUN_WITH_PATH in text and FIX[1] in text
    demo_examples = {c for c in examples() if c not in OTHER_PROGRAMS}
    assert demo_examples == {*AFTER_R1, *AFTER_R2, *AFTER_R3}


def test_the_example_outputs_of_the_readme_are_the_real_output(demo: Demo) -> None:
    shown = examples()
    # The summary after the third command: the job's output, then the summary.
    (summary,) = [b for b in blocks() if b and b[0] == "sync finished: OK"]
    # The block shows what `ormaos run` prints, after what `./reset.sh` printed.
    reset, run = RUN.split(" && ")
    assert demo.shell(reset).returncode == 0
    done = demo.shell(run)
    assert done.returncode == 0, done.stderr
    real = done.stdout.splitlines() + done.stderr.splitlines()
    same(summary, real, run)
    for command in AFTER_R1:
        demo.example(command, shown[command])

    assert demo.shell("./reset.sh").returncode == 0
    path_run = RUN_WITH_PATH.replace("python", ".venv/bin/python")
    assert demo.shell(path_run).returncode == 0
    for command in AFTER_R2:
        demo.example(command, shown[command])

    parse = demo.path / "parse.py"
    source = parse.read_text("utf-8")
    assert source.count(FIX[0]) == 1
    parse.write_text(source.replace(*FIX), encoding="utf-8")
    assert demo.shell(RUN).returncode == 0
    for command in AFTER_R3:
        demo.example(command, shown[command])
