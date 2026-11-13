"""The compatibility matrix: one small example per row.

`ROWS` is the single place that says what each row is and how it ends.
`tests/compat/test_compat.py` runs every row and checks it against this, and
`docs/compatibility.md` is this table, written by `python tests/compat/matrix.py`.

A limit is something the recording of that example does not have. Each one
names the `not_collected` note that says so in that recording; the test
checks that the note is there, and that there is no other one.
"""

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Limit:
    text: str  # for the table
    note: str  # the start of the `what` of the not_collected note


@dataclass(frozen=True)
class Row:
    name: str
    needs: tuple[str, ...]  # modules that must be installed
    example: str  # the file or folder in tests/compat/cases
    command: str  # how it is recorded
    recorded: str  # what the recording has
    limits: tuple[Limit, ...] = ()

    @property
    def outcome(self) -> str:
        return "works, with these limits" if self.limits else "works"


LIBRARY_FILES = Limit(
    "file-system operations the library does on behalf of a line of your code "
    "(listing a directory, making or removing a file) are counted by line, not "
    "recorded one by one",
    "io: directory and file-system operations",
)
NO_USER_LINE = Limit(
    "I/O the library does by itself, with no line of your code waiting for it, "
    "is counted, not recorded",
    "io with no line of user code",
)
C_READS = Limit(
    "what is read from a file inside C code is not seen: the file is recorded "
    "as opened, not as read",
    "io: reads of the 1 file opened",
)
C_WRITES = Limit(
    "what is written to a file inside C code is not seen: the file is recorded "
    "as opened for writing, not as written",
    "io: writes to the 1 file opened",
)
ENVIRONMENT = Limit(
    "while the block runs, `ORMAOS_DIR`, `ORMAOS_PATH` and `ORMAOS_IO` are set "
    "in the environment of the program; they are put back after",
    "the block as it runs without ormaos",
)
CELL_NAMES = Limit(
    "calls and exceptions are recorded under the temporary file name the "
    "kernel gives each cell, not under the cell's number",
    "which notebook cell a call is in",
)

ROWS = (
    Row(
        "pytest",
        ("pytest",),
        "pytest_project/",
        "`ormaos run -- python -m pytest`",
        "the tests, the fixtures and the code they call; an exception caught "
        "by `pytest.raises` is shown as swallowed at that `with` line",
        (NO_USER_LINE,),
    ),
    Row(
        "Flask",
        ("flask",),
        "flask_app.py",
        "`ormaos run -- python flask_app.py` (requests through Flask's test client)",
        "the view functions and what they call, one call tree per request",
        (LIBRARY_FILES,),
    ),
    Row(
        "FastAPI",
        ("fastapi", "httpx"),
        "fastapi_app.py",
        "`ormaos run -- python fastapi_app.py` (requests through its test client)",
        "sync and async routes and what they call, each with its thread and "
        "its asyncio task",
        (NO_USER_LINE,),
    ),
    Row(
        "Django",
        ("django",),
        "django_app.py",
        "`ormaos run -- python django_app.py` (a model, a view, the test client)",
        "the view and what it calls; the SQL statements of the ORM, each with "
        "the line of your code that caused it",
        (LIBRARY_FILES,),
    ),
    Row(
        "Click CLI",
        ("click",),
        "click_cli.py",
        "`ormaos run -- python click_cli.py add 1 x 3`",
        "the command function and what it calls",
    ),
    Row(
        "pandas",
        ("pandas",),
        "pandas_job.py",
        "`ormaos run -- python pandas_job.py in.csv out.csv`",
        "your functions, also when pandas calls them (`Series.apply`); the "
        "files it opened",
        (C_READS, C_WRITES),
    ),
    Row(
        "`with ormaos.record()` in a script",
        (),
        "record_script.py",
        "`python record_script.py`",
        "only what runs inside the block",
        (ENVIRONMENT,),
    ),
    Row(
        "Jupyter notebook",
        ("nbclient", "nbformat", "ipykernel"),
        "notebook.ipynb",
        "`python run_notebook.py notebook.ipynb` (nbclient starts an ipykernel "
        "kernel; one cell has `with ormaos.record():`)",
        "the functions defined in earlier cells, called from the block",
        (ENVIRONMENT, CELL_NAMES),
    ),
    Row(
        "coverage.py at the same time",
        ("coverage",),
        "coverage_script.py",
        "`ormaos run -- python -m coverage run coverage_script.py`, with "
        "`COVERAGE_CORE=ctrace` and with `COVERAGE_CORE=sysmon`",
        "the same calls and exceptions as without coverage; coverage reports "
        "the same lines as without Ormaos",
        (NO_USER_LINE,),
    ),
)

HEAD = """\
# Compatibility

One small example per row, in [`tests/compat/cases/`](../tests/compat/cases/).
Every row is run in CI on one Python version, with the library versions of
`uv.lock`: the example runs with and without Ormaos and must print the same
and exit the same, and the recording must have what the row says.

A row ends "works" when the recording has no `not_collected` note of its own,
and "works, with these limits" when it has: the limits below are those notes,
in plain words. What is never recorded, with or without these libraries
(values, the calls inside the libraries themselves), is in the README.

This file is what `python tests/compat/matrix.py` prints; a test fails when it
is out of date.
"""


def table() -> str:
    lines = [
        HEAD,
        "| What | Example | Recorded with | In the recording | Result |",
        "|---|---|---|---|---|",
    ]
    for row in ROWS:
        result = row.outcome
        if row.limits:
            result += ": " + "; ".join(limit.text for limit in row.limits)
        example = f"[`{row.example}`](../tests/compat/cases/{row.example})"
        lines.append(
            f"| {row.name} | {example} | {row.command} | {row.recorded} | {result} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.write(table())
