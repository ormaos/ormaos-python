"""Ormaos records what happened inside a Python execution."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ormaos._session import Session

__version__ = "0.0.1"

__all__ = ["__version__", "record"]


def record(
    path: tuple[str, ...] | list[str] | str = (),
    io: bool = True,
    summary: bool = False,
    root: str | None = None,
) -> "Session":
    """Record a block of code: ``with ormaos.record(): ...``.

    For scripts and notebooks, where ``ormaos run -- <command>`` does not fit.
    The recording goes to ``.ormaos/`` in the current directory (or ``root``)
    and is read with the same commands (``ormaos show last``).

    ``path`` asks for line-by-line recording inside a scope (``"file.py"``,
    ``"file.py:function"`` or ``"function"``; one or several). ``io=False``
    leaves I/O out. ``summary=True`` prints the short summary on stderr when
    the block ends.
    """
    from ormaos._session import Session

    return Session(path=path, io=io, summary=summary, root=root)
