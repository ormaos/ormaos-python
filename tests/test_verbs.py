"""The verbs that go inside a recording: `tree`, `exceptions`, `path`."""

import json
import sys
from collections.abc import Callable
from typing import Any

import pytest

from conftest import Workdir

CODE = """\
import sys

class Rejected(Exception):
    pass

def check(n):
    if n % 3 == 0:
        raise Rejected(f"no {n}")  # raise
    return n

def step(n):
    return check(n) * 2

def work(items):
    done = 0
    for n in items:
        try:
            step(n)
            done += 1
        except KeyError:
            pass
        except Rejected:  # handler
            continue
    return done

def numbers():
    yield 1
    yield 2

def replace():
    try:
        {}["missing"]
    except KeyError as err:
        raise ValueError("other") from err

work(list(range(1, 13)))
sum(numbers())
try:
    replace()
except ValueError:
    pass
raise RuntimeError("end")  # end
"""

VERBS = ("show", "tree", "exceptions", "path")


def run(work: Workdir, *path: str) -> None:
    work.write("code.py", CODE)
    scopes = [arg for scope in path for arg in ("--path", scope)]
    result = work.cli("run", *scopes, "--", sys.executable, "code.py")
    assert result.returncode == 1 and "RuntimeError: end" in result.stderr


def ask(work: Workdir, *args: str) -> dict[str, Any]:
    answer = work.cli(*args, "--json")
    assert answer.stderr == ""
    doc: dict[str, Any] = json.loads(answer.stdout)
    assert doc["version"] == 1 and "kind" in doc
    assert isinstance(doc["next"], list) and isinstance(doc["next_commands"], list)
    return doc


def text(work: Workdir, *args: str) -> list[str]:
    answer = work.cli(*args)
    assert answer.returncode == 0, answer.stderr
    lines = answer.stdout.splitlines()
    assert lines[-1].startswith("next: ")
    return lines


def test_tree_of_a_recording_starts_at_the_top_and_is_bounded(work: Workdir) -> None:
    run(work)
    doc = ask(work, "tree", "r1")
    assert doc["kind"] == "tree" and doc["root"] is None
    top = doc["nodes"][0]
    assert (top["fn"], top["depth"], top["file"]) == ("<module>", 0, "code.py")
    names = [node["fn"] for node in doc["nodes"] if node["depth"] == 1]
    assert names == ["Rejected", "work", "numbers", "replace"]
    assert len(doc["nodes"]) <= doc["limit"] == 30

    small = ask(work, "tree", "r1", "--limit", "4")
    assert len(small["nodes"]) == 4
    assert sum(rest["not_shown"] for rest in small["not_shown"]) > 0


def test_tree_of_a_call_shows_shapes_results_and_what_was_handled(
    work: Workdir,
) -> None:
    run(work)
    top = ask(work, "tree", "r1")
    address = next(n["address"] for n in top["nodes"] if n["fn"] == "work")

    doc = ask(work, "tree", address, "--limit", "7")
    root = doc["nodes"][0]
    assert doc["root"] == address and root["calls_below"] == 12
    assert root["args"] == {"items": {"type": "list", "len": 12, "items": {"int": 12}}}
    assert root["ret"] == {"type": "int"} and root["out"] == "returned"
    assert root["handled_here"] == [
        {
            "type": "__main__.Rejected",
            "status": "swallowed",
            "count": 4,
            "address": "r1:e1",
        }
    ]
    assert [a["fn"] for a in doc["ancestors"]] == ["<module>"]
    # 7 lines: work, then step and check, three times.
    assert [n["fn"] for n in doc["nodes"]] == ["work", *["step", "check"] * 3]
    (rest,) = doc["not_shown"]
    assert rest == {
        "parent": address,
        "total": 12,
        "not_shown": 9,
        "by_fn": {"step": 9},
        "raised": 3,
        "offset": 3,
    }
    assert f"ormaos tree {address} --offset 3" in doc["next_commands"]

    more = ask(work, "tree", address, "--offset", "3", "--depth", "1")
    assert [n["out"] for n in more["nodes"][1:4]] == ["returned", "returned", "raised"]
    assert len(more["nodes"]) == 10 and more["not_shown"] == []

    lines = text(work, "tree", address, "--limit", "3")
    assert lines[0] == "inside: r1:c1 <module>"
    assert lines[1].startswith(
        f"{address} work(items: list[12 int]) -> int  code.py:14"
    )
    assert "[swallowed 4x __main__.Rejected: r1:e1]" in lines[1]
    assert lines[-2] == (
        f"... 11 of the 12 calls directly below {address} are not shown "
        "(11 step; 4 raised): --offset 1"
    )


def test_exceptions_lists_swallowed_first(
    work: Workdir, line_of: Callable[[str, str], int]
) -> None:
    run(work)
    doc = ask(work, "exceptions", "r1")
    assert doc["kind"] == "exceptions"
    assert (doc["total"], doc["swallowed"], doc["uncaught"]) == (7, 5, 1)
    statuses = [entry["status"] for entry in doc["exceptions"]]
    assert statuses == [*["swallowed"] * 5, "uncaught", "replaced"]
    first = doc["exceptions"][0]
    assert first["type"] == "__main__.Rejected"
    assert first["raised_at"] == {
        "file": "code.py",
        "line": line_of(CODE, "raise"),
        "fn": "check",
        "user_code": True,
    }
    assert first["handled_at"]["line"] == line_of(CODE, "handler")
    assert doc["next"][0] == first["address"]

    page = ask(work, "exceptions", "r1", "--limit", "2", "--offset", "1")
    assert len(page["exceptions"]) == 2 and page["not_shown"] == 4
    assert "ormaos exceptions r1 --offset 3" in page["next_commands"]

    lines = text(work, "exceptions", "r1", "--limit", "2")
    assert lines[0] == "r1: 7 exceptions (5 swallowed, 1 uncaught)"
    assert lines[1].startswith("r1:e1 swallowed __main__.Rejected  raised at code.py:8")
    assert lines[-2] == "... 5 more: --offset 2"


def test_exceptions_of_a_call_are_the_ones_below_it(work: Workdir) -> None:
    run(work)
    top = ask(work, "tree", "r1")
    replace = next(n["address"] for n in top["nodes"] if n["fn"] == "replace")
    doc = ask(work, "exceptions", replace)
    assert doc["scope"]["fn"] == "replace"
    assert [(e["type"], e["status"]) for e in doc["exceptions"]] == [
        ("ValueError", "swallowed"),
        ("KeyError", "replaced"),
    ]


def test_one_exception_in_full(
    work: Workdir, line_of: Callable[[str, str], int]
) -> None:
    run(work)
    doc = ask(work, "exceptions", "r1:e1")
    assert doc["kind"] == "exception" and doc["status"] == "swallowed"
    assert doc["message"].startswith("not recorded")
    assert doc["args"] == {"type": "tuple", "len": 1, "items": {"str": 1}}
    assert [call["fn"] for call in doc["left_calls"]] == ["check", "step"]
    assert doc["handled_at"]["fn"] == "work"
    assert doc["handled_at"]["line"] == line_of(CODE, "handler")
    assert doc["same_type_and_handler"]["count"] == 4
    assert doc["next"][0] == doc["handled_in"]
    assert f"ormaos tree {doc['handled_in']}" in doc["next_commands"]

    replaced = next(
        e
        for e in ask(work, "exceptions", "r1")["exceptions"]
        if e["type"] == "KeyError"
    )
    detail = ask(work, "exceptions", replaced["address"])
    assert detail["status"] == "replaced"
    other = ask(work, "exceptions", detail["replaced_by"])
    assert other["type"] == "ValueError" and other["during"] == replaced["address"]

    lines = text(work, "exceptions", "r1:e1")
    assert lines[0] == "r1:e1 __main__.Rejected: swallowed"
    assert any(
        line.strip().startswith(f"handled at  code.py:{line_of(CODE, 'handler')}")
        and line.endswith("and not raised again")
        for line in lines
    )


def test_path_shows_the_lines_the_calls_and_the_handled_exceptions(
    work: Workdir, line_of: Callable[[str, str], int]
) -> None:
    run(work, "code.py:work", "numbers")
    overview = ask(work, "path", "r1")
    assert overview["kind"] == "paths" and overview["scope"] == [
        "code.py:work",
        "numbers",
    ]
    assert [(f["fn"], f["calls"]) for f in overview["functions"]] == [
        ("work", 1),
        ("numbers", 1),
    ]
    address = overview["functions"][0]["addresses"][0]

    doc = ask(work, "path", address, "--limit", "30")
    assert doc["kind"] == "path" and doc["call"]["fn"] == "work"
    assert doc["source"]["changed_after_recording"] is False
    first = line_of(CODE, "handler") - 7  # `done = 0`
    steps = doc["steps"]
    assert [s.get("line") for s in steps[:5]] == [
        first,
        first + 1,
        first + 2,
        first + 3,
        None,
    ]
    assert steps[0]["text"] == "    done = 0"
    assert steps[4]["fn"] == "step" and steps[4]["out"] == "returned"
    # The third item is rejected: the call raises, the exception enters the
    # handlers, the first clause does not match, the second one does.
    raised = next(i for i, s in enumerate(steps) if s.get("out") == "raised")
    after = steps[raised + 1 : raised + 5]
    assert after[0] == {
        "step": raised + 2,
        "exception": "r1:e1",
        "type": "__main__.Rejected",
        "status": "swallowed",
    }
    assert [s["line"] for s in after[1:]] == [
        line_of(CODE, "handler") - 2,
        line_of(CODE, "handler"),
        line_of(CODE, "handler") + 1,
    ]
    assert doc["total"] > 30 and doc["not_shown_after"] == doc["total"] - 30
    assert f"ormaos path {address} --offset 30" in doc["next_commands"]

    # Asked by the exception: the answer is its handler, around the moment.
    by_exception = ask(work, "path", "r1:e2", "--limit", "6")
    assert by_exception["call"]["address"] == address
    assert any(s.get("exception") == "r1:e2" for s in by_exception["steps"])

    # The generator's lines, across its suspensions.
    generator = overview["functions"][1]["addresses"][0]
    lines = [s["line"] for s in ask(work, "path", generator)["steps"]]
    assert lines == [line_of(CODE, "handler") + 5, line_of(CODE, "handler") + 6]

    shown = text(work, "path", "r1:e1", "--limit", "8")
    assert shown[0].startswith(f"{address} work code.py:14: ")
    assert any(
        "exception __main__.Rejected [r1:e1] enters a handler here" in s for s in shown
    )
    assert any(s.endswith("except Rejected:  # handler") for s in shown)


def test_path_recording_does_not_change_which_clause_is_reported(
    work: Workdir, line_of: Callable[[str, str], int]
) -> None:
    run(work, "code.py")
    doc = ask(work, "exceptions", "r1:e1")
    assert doc["handled_at"]["line"] == line_of(CODE, "handler")
    paths = ask(work, "path", "r1")
    assert {f["fn"] for f in paths["functions"]} >= {
        "<module>",
        "work",
        "check",
        "step",
    }


def test_path_that_was_not_recorded_says_how_to_record_it(work: Workdir) -> None:
    run(work)
    for address in ("r1", "r1:e1"):
        answer = work.cli("path", address, "--json")
        assert answer.returncode == 1
        doc = json.loads(answer.stdout)
        assert doc["kind"] == "error" and doc["version"] == 1
        assert "no path" in doc["error"]
        assert doc["next_commands"][0] == (
            f"ormaos run --path code.py:work -- {sys.executable} code.py"
        )
    plain = work.cli("path", "r1:e1")
    assert plain.returncode == 1 and plain.stdout == ""
    assert plain.stderr.startswith("ormaos: no path for ")
    assert plain.stderr.splitlines()[-1].startswith("ormaos: next: ormaos run --path")


def test_a_scope_that_matches_nothing_is_in_not_collected(work: Workdir) -> None:
    run(work, "nothing.py:nowhere")
    doc = ask(work, "show")
    assert {
        "what": "path",
        "reason": "no call matched the scope nothing.py:nowhere",
    } in doc["not_collected"]
    assert any("no call matched the scope" in line for line in text(work, "show"))


def test_without_a_scope_the_overview_says_path_is_off(work: Workdir) -> None:
    run(work)
    notes = ask(work, "show")["not_collected"]
    (note,) = [n for n in notes if n["what"] == "path"]
    assert "ormaos run --path" in note["reason"] and note["always"] is True


@pytest.mark.parametrize("verb", VERBS)
def test_errors_are_json_with_a_version_on_every_verb(work: Workdir, verb: str) -> None:
    nothing = work.cli(verb, "--json")
    assert nothing.returncode == 1
    doc = json.loads(nothing.stdout)
    assert doc["kind"] == "error" and doc["version"] == 1
    assert "no recordings" in doc["error"]
    assert doc["next_commands"] == ["ormaos run -- <command>"]

    run(work)
    for address, word in (("r1:c999", "no call"), ("r1:x1", "not an address")):
        answer = work.cli(verb, address, "--json")
        if verb == "show" and address == "r1:c999":
            continue
        assert answer.returncode == 1 and answer.stderr == ""
        assert word in json.loads(answer.stdout)["error"]

    plain = work.cli(verb, "r7")
    assert plain.returncode == 1 and plain.stdout == ""
    assert plain.stderr.startswith("ormaos: no recording 'r7'")


def test_a_usage_error_is_json_too(work: Workdir) -> None:
    answer = work.cli("tree", "--json", "--limit", "many")
    assert answer.returncode == 2
    doc = json.loads(answer.stdout)
    assert doc["kind"] == "error" and "--limit" in doc["error"]


def test_show_text_is_the_overview_and_ends_with_next(work: Workdir) -> None:
    run(work)
    lines = text(work, "show")
    assert lines[0].startswith("r1 exit 1 in ")
    assert lines[0].endswith(" calls, 7 exceptions (5 swallowed, 1 uncaught)")
    assert ": 1 Python process, " in lines[0]
    assert lines[1] == f"command: {sys.executable} code.py"
    assert lines[2] == (
        "swallowed 4x __main__.Rejected at code.py:22 in work, "
        "raised at code.py:8  [r1:e1]"
    )
    assert any(
        line.startswith("uncaught RuntimeError raised at code.py:") for line in lines
    )
    assert lines[-1].startswith("next: ormaos exceptions r1:e1 | ")
    assert text(work, "show", "last") == text(work, "show", "r1") == lines


def test_every_answer_is_bounded_by_its_limit(work: Workdir) -> None:
    source = (
        "def f(n):\n    try:\n        raise KeyError(n)\n"
        "    except KeyError:\n        pass\n"
    )
    work.write("many.py", source + "for n in range(500):\n    f(n)\n")
    work.cli("run", "--path", "many.py", "--", sys.executable, "many.py")
    assert len(ask(work, "tree", "r1")["nodes"]) == 30
    assert len(ask(work, "exceptions", "r1")["exceptions"]) == 20
    assert len(ask(work, "path", "r1:c1")["steps"]) == 40
    for verb in ("tree", "exceptions"):
        assert len(text(work, verb, "r1")) <= 34
    assert len(text(work, "path", "r1:c1")) <= 44
    assert len(text(work, "show")) <= 12
    assert len(ask(work, "exceptions", "r1", "--limit", "200")["exceptions"]) == 200
