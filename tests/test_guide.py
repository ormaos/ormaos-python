"""`ormaos guide`, the AGENTS.md line and the skill file."""

import json
import re
from pathlib import Path

from conftest import Workdir
from ormaos.cli import build_parser
from ormaos.guide import GUIDE, MARK, SNIPPET

SKILL = Path(__file__).resolve().parent.parent / "skills" / "ormaos" / "SKILL.md"
READERS = ("show", "exceptions", "tree", "io", "path")


def verbs() -> set[str]:
    parser = build_parser()
    (action,) = [a for a in parser._actions if isinstance(a.choices, dict)]
    return set(action.choices)


_VERB = re.compile(r"ormaos (run|guide|diff|" + "|".join(READERS) + r")\b([^\n`|]*)")
_ARGUMENT = re.compile(r"<addr>|\[r3\]|last|r\d+(:[cei]\d+)?|--[a-z-]+")


def commands_in(text: str) -> list[list[str]]:
    """Every `ormaos <verb> <address or option>...` written in a text."""
    found = []
    for match in _VERB.finditer(text):
        words = [match.group(1)]
        for word in re.split(r"\s{2,}", match.group(2).strip())[0].split():
            if not _ARGUMENT.fullmatch(word):
                break
            words.append(word.strip("[]").replace("<addr>", "r3"))
        found.append(words)
    return found


def test_the_guide_is_short_and_has_the_four_parts(work: Workdir) -> None:
    result = work.cli("guide")
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout == GUIDE
    lines = GUIDE.splitlines()
    assert len(lines) <= 50 and max(len(line) for line in lines) <= 80
    for part in (
        "WHEN TO USE IT",
        "WHERE TO START",
        "HOW TO FOLLOW ADDRESSES",
        "WHAT IS NOT RECORDED",
    ):
        assert part in lines
    # It works before any recording exists, and writes nothing.
    assert list(work.path.iterdir()) == []


def test_the_guide_names_every_verb_and_only_verbs_that_exist() -> None:
    assert verbs() == {"run", "guide", "diff", *READERS}
    for verb in ("run", "diff", *READERS):
        assert f"ormaos {verb} " in GUIDE
    parser = build_parser()
    for text in (GUIDE, SKILL.read_text(encoding="utf-8")):
        written = commands_in(text)
        assert len(written) >= 8
        for words in written:
            if words[0] == "run":
                continue  # `run` takes a command of the user after `--`
            if words[-1] in ("--limit", "--offset", "--depth", "--kind", "--write"):
                words = words[:-1]  # the value is written as N or as a choice
            assert parser.parse_args(words).verb == words[0], words
    for option in ("--json", "--limit", "--offset", "--depth", "--kind", "--path"):
        assert option in GUIDE


def test_the_guide_as_json(work: Workdir) -> None:
    doc = json.loads(work.cli("guide", "--json").stdout)
    assert doc["kind"] == "guide" and doc["version"] == 1
    assert doc["text"] == GUIDE
    assert doc["next_commands"] == ["ormaos run -- <command>", "ormaos show"]


def test_agents_md_prints_one_line_and_touches_no_file(work: Workdir) -> None:
    (work.path / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    result = work.cli("guide", "--agents-md")
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout == SNIPPET + "\n"
    assert "\n" not in SNIPPET and MARK in SNIPPET
    assert (work.path / "AGENTS.md").read_text(encoding="utf-8") == "# Rules\n"
    assert [p.name for p in work.path.iterdir()] == ["AGENTS.md"]


def test_agents_md_write_shows_the_change_then_makes_it(work: Workdir) -> None:
    file = work.path / "AGENTS.md"
    file.write_text("# Rules\n\n- Use uv.", encoding="utf-8")

    result = work.cli("guide", "--agents-md", "--write")
    assert result.returncode == 0
    assert result.stdout.startswith("--- AGENTS.md\n+++ AGENTS.md\n@@ ")
    assert f"+{SNIPPET}\n" in result.stdout
    assert result.stderr == "ormaos: added the line to AGENTS.md\n"
    assert file.read_text(encoding="utf-8") == f"# Rules\n\n- Use uv.\n\n{SNIPPET}\n"

    again = work.cli("guide", "--agents-md", "--write")
    assert again.returncode == 0 and again.stdout == ""
    assert "already mentions `ormaos guide`: nothing to change" in again.stderr
    assert file.read_text(encoding="utf-8").count(SNIPPET) == 1


def test_agents_md_write_to_a_new_file_and_as_json(work: Workdir) -> None:
    result = work.cli("guide", "--agents-md", "--write", "docs.md", "--json")
    doc = json.loads(result.stdout)
    assert doc["kind"] == "agents_md" and doc["version"] == 1
    assert doc["changed"] is True and doc["file"] == "docs.md"
    assert doc["diff"].startswith("--- /dev/null\n+++ docs.md\n")
    assert (work.path / "docs.md").read_text(encoding="utf-8") == SNIPPET + "\n"

    missing = work.cli("guide", "--agents-md", "--write", "no/such/dir/A.md")
    assert missing.returncode == 1
    assert missing.stderr.startswith("ormaos: cannot change no/such/dir/A.md")
    wrong = work.cli("guide", "--write", "AGENTS.md")
    assert wrong.returncode == 2 and not (work.path / "AGENTS.md").exists()


def test_the_skill_file_has_the_standard_header() -> None:
    text = SKILL.read_text(encoding="utf-8")
    head, _, body = text.removeprefix("---\n").partition("\n---\n")
    fields = dict(line.split(": ", 1) for line in head.splitlines())
    assert set(fields) == {"name", "description"}
    assert fields["name"] == SKILL.parent.name == "ormaos"
    assert 50 < len(fields["description"]) <= 1024
    assert body.lstrip().startswith("# Ormaos")
    for verb in ("run", "guide", "diff", *READERS):
        assert f"ormaos {verb}" in body
    for address in ("last", "r3:c42", "r3:e5", "r3:i7"):
        assert address in body and address in GUIDE
