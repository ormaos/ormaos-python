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

| What | Example | Recorded with | In the recording | Result |
|---|---|---|---|---|
| pytest | [`pytest_project/`](../tests/compat/cases/pytest_project/) | `ormaos run -- python -m pytest` | the tests, the fixtures and the code they call; an exception caught by `pytest.raises` is shown as swallowed at that `with` line | works, with these limits: I/O the library does by itself, with no line of your code waiting for it, is counted, not recorded |
| Flask | [`flask_app.py`](../tests/compat/cases/flask_app.py) | `ormaos run -- python flask_app.py` (requests through Flask's test client) | the view functions and what they call, one call tree per request | works, with these limits: file-system operations the library does on behalf of a line of your code (listing a directory, making or removing a file) are counted by line, not recorded one by one |
| FastAPI | [`fastapi_app.py`](../tests/compat/cases/fastapi_app.py) | `ormaos run -- python fastapi_app.py` (requests through its test client) | sync and async routes and what they call, each with its thread and its asyncio task | works, with these limits: I/O the library does by itself, with no line of your code waiting for it, is counted, not recorded |
| Django | [`django_app.py`](../tests/compat/cases/django_app.py) | `ormaos run -- python django_app.py` (a model, a view, the test client) | the view and what it calls; the SQL statements of the ORM, each with the line of your code that caused it | works, with these limits: file-system operations the library does on behalf of a line of your code (listing a directory, making or removing a file) are counted by line, not recorded one by one |
| Click CLI | [`click_cli.py`](../tests/compat/cases/click_cli.py) | `ormaos run -- python click_cli.py add 1 x 3` | the command function and what it calls | works |
| pandas | [`pandas_job.py`](../tests/compat/cases/pandas_job.py) | `ormaos run -- python pandas_job.py in.csv out.csv` | your functions, also when pandas calls them (`Series.apply`); the files it opened | works, with these limits: what is read from a file inside C code is not seen: the file is recorded as opened, not as read; what is written to a file inside C code is not seen: the file is recorded as opened for writing, not as written |
| `with ormaos.record()` in a script | [`record_script.py`](../tests/compat/cases/record_script.py) | `python record_script.py` | only what runs inside the block | works, with these limits: while the block runs, `ORMAOS_DIR`, `ORMAOS_PATH` and `ORMAOS_IO` are set in the environment of the program; they are put back after |
| Jupyter notebook | [`notebook.ipynb`](../tests/compat/cases/notebook.ipynb) | `python run_notebook.py notebook.ipynb` (nbclient starts an ipykernel kernel; one cell has `with ormaos.record():`) | the functions defined in earlier cells, called from the block | works, with these limits: while the block runs, `ORMAOS_DIR`, `ORMAOS_PATH` and `ORMAOS_IO` are set in the environment of the program; they are put back after; calls and exceptions are recorded under the temporary file name the kernel gives each cell, not under the cell's number |
| coverage.py at the same time | [`coverage_script.py`](../tests/compat/cases/coverage_script.py) | `ormaos run -- python -m coverage run coverage_script.py`, with `COVERAGE_CORE=ctrace` and with `COVERAGE_CORE=sysmon` | the same calls and exceptions as without coverage; coverage reports the same lines as without Ormaos | works, with these limits: I/O the library does by itself, with no line of your code waiting for it, is counted, not recorded |
