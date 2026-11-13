"""``ormaos guide``: how a coding agent uses Ormaos, short enough to read in
one call. Only what works today is described here.
"""

import os
from typing import Any

from ormaos.verbs import VERSION

GUIDE = """\
ormaos: what happened inside a Python run, to read instead of guessing.

WHEN TO USE IT
- A Python program runs, and its output does not explain what it did: a job
  that prints OK, an error that never shows, a result you cannot trace.
- You want to see what ran: the project's calls with the shape of their
  arguments, every exception (also the caught ones), the SQL statements,
  files, subprocesses and HTTP requests.

WHERE TO START
1. Record:  ormaos run -- <command>
   The command runs unchanged (same output, same exit code); its Python must
   have ormaos installed. In a script or notebook: `with ormaos.record():`.
2. Read the summary printed on stderr: at most 5 lines, each starting with
   "ormaos:". To see it again, with more: ormaos show
3. Run the commands of the `next:` line. Reading never runs the program.

HOW TO FOLLOW ADDRESSES
Everything has an address: r3 is a recording, r3:c42 a call, r3:e5 an
exception, r3:i7 an I/O event. `last` is the newest recording (`last:e5`
works too). An address belongs to one recording. Every answer is short and
ends with `next:`, the commands for the next question.
  ormaos show [r3]          overview: counts, swallowed and uncaught exceptions
  ormaos exceptions <addr>  the exceptions of a recording or a call, swallowed
                            first; for one exception: where it was raised, the
                            calls it left, where it was handled
  ormaos tree <addr>        the calls below a call, or the top of a recording
  ormaos io <addr>          SQL, files, subprocesses and HTTP of a recording or
                            a call, by line of code, with counts
  ormaos path <addr>        the lines a call ran, in order; needs a recording
                            made with `ormaos run --path file.py:function --
                            <command>` (the program runs again; see the error)
  ormaos diff r3 r4         what r4 did differently from r3, by place in code
  ormaos list               the recordings, newest first; killed: "incomplete"
  ormaos status             what works here, and what is being recorded now
An exception address given to tree, io or path means the call that handled it.
Exceptions are: "swallowed" (entered an `except`, not raised again; "handled
at" is the line of that `except`), "uncaught" (ended the program), "replaced"
(another was raised from its handler), "exit" (SystemExit ended the program).
Options: --json (one JSON document, also for errors), --limit N, --offset N
(skip entries); tree: --depth N; io: --kind sqlite|file|subprocess|http.

WHAT IS NOT RECORDED
- Values: arguments, results, exception messages, environment values, SQL
  parameters, file contents, headers, bodies, URL query strings.
- Calls inside the standard library and installed packages; lines outside a
  --path scope; reads of a file that is iterated (only its `open` is seen).
- What one run could not record is listed as "not collected" in `ormaos show`;
  `ormaos show --json` has the full list, with the reasons.
Recordings stay in .ormaos/ on this machine. Nothing is sent anywhere.
"""

SNIPPET = (
    "- When a Python program runs and its behaviour is unclear, record it with "
    "`ormaos run -- <command>` and read what happened inside; run `ormaos guide` "
    "first to learn how."
)
MARK = "`ormaos guide`"


def guide_document() -> dict[str, Any]:
    commands = ["ormaos run -- <command>", "ormaos show"]
    return {
        "kind": "guide",
        "version": VERSION,
        "text": GUIDE,
        "next": [],
        "next_commands": commands,
    }


def agents_md(file: str | None) -> dict[str, Any]:
    """The line for a project's AGENTS.md. With ``file``, also the change that
    adds it to that file, as a diff, and the new content (not written here)."""
    doc: dict[str, Any] = {
        "kind": "agents_md",
        "version": VERSION,
        "snippet": SNIPPET,
        "file": file,
        "changed": False,
        "diff": "",
        "next": [],
        "next_commands": [],
    }
    if file is None:
        doc["next_commands"] = ["ormaos guide --agents-md --write AGENTS.md"]
        return doc
    import difflib

    old = ""
    if os.path.exists(file):
        with open(file, encoding="utf-8") as handle:
            old = handle.read()
    if MARK in old:
        doc["note"] = f"{file} already mentions `ormaos guide`: nothing to change"
        return doc
    new = old
    if new and not new.endswith("\n"):
        new += "\n"
    if new:
        new += "\n"
    new += SNIPPET + "\n"
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=file if old else "/dev/null",
        tofile=file,
    )
    doc["diff"] = "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in diff
    )
    doc["changed"] = True
    doc["content"] = new
    return doc
