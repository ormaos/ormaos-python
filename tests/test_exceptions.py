"""Exceptions: where raised, where handled, and the `swallowed` mark."""

import sys
from collections.abc import Callable

from conftest import Workdir

SOURCE = """\
import contextlib

def boom():
    raise KeyError("k")  # raise

def swallow():
    try:
        boom()
    except KeyError:  # swallow
        pass

def through_finally():
    try:
        boom()
    finally:
        pass

def no_match():
    try:
        boom()
    except ValueError:
        pass

def bare_raise():
    try:
        boom()
    except KeyError:
        raise

def raise_same():
    try:
        boom()
    except KeyError as err:
        raise err

def replace():
    try:
        boom()
    except KeyError as err:  # replace
        raise ValueError("v") from err

def suppressed():
    with contextlib.suppress(KeyError):  # with
        boom()

def outer(func):
    try:
        func()
    except Exception:  # outer
        pass

swallow()
for func in (through_finally, no_match, bare_raise, raise_same, replace):
    outer(func)
suppressed()
for _ in range(5):
    swallow()
print("before the end")
1 / 0  # uncaught
"""


def test_exceptions(work: Workdir, line_of: Callable[[str, str], int]) -> None:
    work.write("exc.py", SOURCE)
    plain = work.plain(sys.executable, "exc.py")
    result = work.python("exc.py")
    assert result.returncode == plain.returncode == 1
    assert result.stdout == plain.stdout
    assert result.program_stderr == plain.stderr

    recording = work.last()
    assert recording.not_collected == []
    exceptions = recording.exceptions
    assert [e["addr"] for e in exceptions] == [f"r1:e{n}" for n in range(1, 15)]

    def handled(exc: dict[str, object]) -> tuple[object, object, object]:
        where = exc["handled"]
        assert isinstance(where, dict)
        return where["fn"], where["line"], exc["swallowed"]

    first = exceptions[0]
    assert first["type"] == "KeyError"
    assert first["args"] == {"type": "tuple", "len": 1, "items": {"str": 1}}
    assert first["origin"]["line"] == line_of(SOURCE, "raise")
    assert first["origin"]["fn"] == "boom" and first["origin"]["user"] is True
    assert handled(first) == ("swallow", line_of(SOURCE, "swallow"), True)
    assert first["end"] == "handled"
    call = next(c for c in recording.calls if c["addr"] == first["call_addr"])
    assert call["fn"] == "boom" and call["out"] == "raised"

    # Passing through `finally`, a clause that does not match, a bare `raise`
    # and `raise err` is not swallowing: the outer handler is where it ends.
    outer = ("outer", line_of(SOURCE, "outer"), True)
    for exc in exceptions[1:5]:
        assert exc["type"] == "KeyError"
        assert handled(exc) == outer

    # `raise Other from err`: the first one is handled but not swallowed.
    replaced, replacement = exceptions[5], exceptions[6]
    assert handled(replaced) == ("replace", line_of(SOURCE, "replace"), False)
    assert replaced["replaced_by_addr"] == replacement["addr"]
    assert replacement["type"] == "ValueError"
    assert replacement["during_addr"] == replaced["addr"]
    assert handled(replacement) == outer

    assert handled(exceptions[7]) == ("suppressed", line_of(SOURCE, "with"), True)

    # The same handler, five more times: five records, not one.
    for exc in exceptions[8:13]:
        assert handled(exc) == ("swallow", line_of(SOURCE, "swallow"), True)

    last = exceptions[13]
    assert last["type"] == "ZeroDivisionError"
    assert last["end"] == "uncaught" and "handled" not in last
    assert last["origin"]["line"] == line_of(SOURCE, "uncaught")


def test_summary_shows_the_swallowed_exception(
    work: Workdir, line_of: Callable[[str, str], int]
) -> None:
    work.write("exc.py", SOURCE)
    result = work.python("exc.py")
    summary = result.summary
    assert len(summary) <= 5
    assert "14 exceptions (12 swallowed)" in summary[0]
    assert summary[1] == (
        f"ormaos: swallowed 6x KeyError at exc.py:{line_of(SOURCE, 'swallow')}, "
        f"raised at exc.py:{line_of(SOURCE, 'raise')} [r1:e1]"
    )
    assert summary[2].startswith("ormaos: uncaught ZeroDivisionError raised at exc.py:")
    assert summary[-1] == (
        "ormaos: saved in .ormaos/r1, next: ormaos exceptions r1:e1 | "
        "ormaos exceptions r1:e14 | ormaos tree r1:c1"
    )


def test_swallowed_exception_in_a_command_string(work: Workdir) -> None:
    code = (
        "def f():\n    raise ValueError('x')\n"
        "try:\n    f()\nexcept ValueError:\n    pass\n"
    )
    result = work.python("-c", code)
    assert result.returncode == 0
    assert result.summary[1] == (
        "ormaos: swallowed 1x ValueError at <string>:5, raised at <string>:2 [r1:e1]"
    )


def test_exception_raised_in_a_library_and_caught_by_the_user(work: Workdir) -> None:
    code = "import json\ntry:\n    json.loads('{')\nexcept ValueError:\n    pass\n"
    work.python("-c", code)
    (exc,) = work.last().exceptions
    assert exc["type"] == "json.decoder.JSONDecodeError"
    assert exc["origin"]["user"] is False and exc["origin"]["fn"].endswith("raw_decode")
    assert exc["at"]["file"] == "<string>" and exc["at"]["line"] == 3
    assert exc["handled"]["line"] == 4 and exc["swallowed"] is True


def test_exception_in_a_thread_is_handled_by_threading(work: Workdir) -> None:
    code = (
        "import threading\n"
        "def run():\n    raise RuntimeError('in thread')\n"
        "t = threading.Thread(target=run)\nt.start()\nt.join()\n"
    )
    plain = work.plain(sys.executable, "-c", code)
    result = work.python("-c", code)
    assert result.program_stderr == plain.stderr
    (exc,) = work.last().exceptions
    assert exc["handled"]["user"] is False
    assert exc["handled"]["file"].endswith("threading.py")


def test_generators_and_coroutines(work: Workdir) -> None:
    code = """\
import asyncio

def gen():
    try:
        yield 1
    except KeyError:
        pass
    yield 2

async def fails():
    await asyncio.sleep(0)
    raise OSError("no")

async def main():
    try:
        await fails()
    except OSError:
        return "handled"

g = gen()
next(g)
g.throw(KeyError("k"))
g.close()
print(asyncio.run(main()))
"""
    work.write("gen.py", code)
    plain = work.plain(sys.executable, "gen.py")
    result = work.python("gen.py")
    assert (result.returncode, result.stdout) == (plain.returncode, plain.stdout)
    assert result.program_stderr == plain.stderr
    recording = work.last()
    assert recording.not_collected == []
    swallowed = {
        (e["type"], e["handled"]["fn"])
        for e in recording.exceptions
        if e.get("swallowed")
    }
    assert ("KeyError", "gen") in swallowed
    assert ("OSError", "main") in swallowed
    outcomes = {c["fn"]: c["out"] for c in recording.calls}
    assert outcomes["main"] == "returned" and outcomes["fails"] == "raised"


CLAUSES = """\
def boom():
    raise KeyError("k")

def second():
    try:
        boom()
    except ValueError:
        return 1
    except KeyError as err:  # second
        return 2

def bare_last():
    try:
        boom()
    except ValueError:
        return 1
    except:  # bare_last
        return 2

def third_on_one_line():
    try:
        boom()
    except ValueError: pass
    except OSError: pass
    except KeyError: pass  # third_on_one_line
    return 3

def first_of_two():
    try:
        boom()
    except (KeyError, OSError):  # first_of_two
        return 1
    except ValueError:
        return 2

def none_matches():
    try:
        boom()
    except ValueError:
        return 1
    except OSError:
        return 2

def outer():
    try:
        none_matches()
    except Exception:  # outer
        pass

def with_finally():
    try:
        try:
            boom()
        finally:
            done = True
    except OSError:
        return 1
    except KeyError:  # with_finally
        return 2

def recursive(depth):
    try:
        if depth:
            recursive(depth - 1)
        boom()
    except ValueError:
        return 1
    except LookupError:  # recursive
        return 2

second()
bare_last()
third_on_one_line()
first_of_two()
outer()
with_finally()
recursive(2)
for _ in range(3):
    second()
"""


def test_handler_line_is_the_clause_that_matched(
    work: Workdir, line_of: Callable[[str, str], int]
) -> None:
    work.write("clauses.py", CLAUSES)
    plain = work.plain(sys.executable, "clauses.py")
    result = work.python("clauses.py")
    assert (result.returncode, result.stdout) == (plain.returncode, plain.stdout)
    assert result.program_stderr == plain.stderr

    recording = work.last()
    assert recording.not_collected == []
    got = [(e["handled"]["fn"], e["handled"]["line"]) for e in recording.exceptions]
    names = [
        "second",
        "bare_last",
        "third_on_one_line",
        "first_of_two",
        "outer",
        "with_finally",
        "recursive",
        "recursive",
        "recursive",
        "second",
        "second",
        "second",
    ]
    assert got == [(name, line_of(CLAUSES, name)) for name in names]
    assert all(e["swallowed"] for e in recording.exceptions)
