"""The command line as it is recorded: names, never values.

Kept as written: the executable, a Python interpreter further along the
command (``uv run python ...``), the script path or the module after ``-m``,
and the names of options (``--name`` of ``--name=value``). Every other
argument is a value, and only its shape is kept: ``<str[12]>`` for a string of
12 characters. So are the program text after ``-c`` and the value that follows
an option.
"""

import os
import re
from collections.abc import Sequence

_PYTHON = re.compile(r"(python|pypy)[0-9.]*(\.exe)?", re.IGNORECASE)
# Options of the Python interpreter that take the next word as their value.
_PYTHON_TAKES_VALUE = ("-W", "-X", "--check-hash-based-pycs")
_FLAGS = re.compile(r"-[A-Za-z]{1,3}")


def hidden(text: str) -> str:
    """The shape of a value: ``<str[12]>``."""
    return f"<str[{len(text)}]>"


def _is_python(word: str) -> bool:
    return _PYTHON.fullmatch(os.path.basename(word)) is not None


def _is_option(word: str) -> bool:
    return len(word) > 1 and word[0] == "-" and not word[1].isdigit()


def _option(word: str) -> str:
    """An option with its value hidden: ``--name=<str[5]>``, ``-p<str[6]>``."""
    name, equals, value = word.partition("=")
    if equals:
        return f"{name}={hidden(value)}"
    if word.startswith("--") or _FLAGS.fullmatch(word):
        return word  # `--name`, `--`, and short flags such as `-q` or `-vv`
    return word[:2] + hidden(word[2:])  # `-pVALUE`: the letter, not the value


def _arguments(words: Sequence[str]) -> list[str]:
    return [_option(word) if _is_option(word) else hidden(word) for word in words]


def _python(words: Sequence[str]) -> list[str]:
    """What follows a Python interpreter: its options, then the script or the
    module (kept), then the arguments of the program."""
    out: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if not _is_option(word):
            if word == "-":
                out.append(word)  # the program comes from stdin
                return out + _arguments(words[i + 1 :])
            out.append(word)  # the script
            return out + _arguments(words[i + 1 :])
        short = not word.startswith("--")
        if short and word.endswith("m") and "=" not in word:
            out.append(word)
            out.extend(words[i + 1 : i + 2])  # the module
            return out + _arguments(words[i + 2 :])
        if short and word.endswith("c") and "=" not in word:
            out.append(word)
            out.extend(hidden(text) for text in words[i + 1 : i + 2])  # program text
            return out + _arguments(words[i + 2 :])
        if word in _PYTHON_TAKES_VALUE:
            out.append(word)
            out.extend(hidden(value) for value in words[i + 1 : i + 2])
            i += 2
            continue
        out.append(_option(word))
        i += 1
    return out


def shape_command(command: Sequence[str]) -> list[str]:
    """The command with every value replaced by its shape."""
    words = [str(word) for word in command]
    if not words:
        return []
    out = [words[0]]
    if _is_python(words[0]):
        return out + _python(words[1:])
    for i, word in enumerate(words[1:], start=1):
        if _is_option(word):
            out.append(_option(word))
        elif _is_python(word):
            out.append(word)
            return out + _python(words[i + 1 :])
        elif word.endswith(".py"):
            out.append(word)  # a script path
        else:
            out.append(hidden(word))
    return out
