import shutil
import subprocess
import sys

import pytest

import ormaos
from ormaos.cli import main


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"ormaos {ormaos.__version__}"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: ormaos" in capsys.readouterr().out


def test_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ormaos", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"ormaos {ormaos.__version__}"


def test_installed_script() -> None:
    script = shutil.which("ormaos")
    assert script is not None, "the `ormaos` script is not installed"
    result = subprocess.run(
        [script, "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"ormaos {ormaos.__version__}"
