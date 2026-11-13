"""Describe a value by its shape: type, length and structure. Never the value.

Nothing here may run code of the observed program. So we only look inside
objects whose type is exactly a builtin container, and we read type names
through the descriptors of ``type`` itself, which a metaclass cannot override.
"""

from itertools import islice
from typing import Any

# The types of the items of a container are taken from its first items only.
SAMPLE = 50

_get_module = type.__dict__["__module__"].__get__
_get_qualname = type.__dict__["__qualname__"].__get__

# Compared by identity (`id`), never with `==` or `hash`: both would run the
# code of a metaclass of the program.
_SIZED = frozenset(map(id, (str, bytes, bytearray)))
_SEQUENCES = frozenset(map(id, (list, tuple, set, frozenset)))
_get_name = type.__dict__["__name__"].__get__


def type_name(tp: type) -> str:
    """``str`` for builtins, ``module.QualName`` for everything else."""
    module = _get_module(tp)
    name = _get_qualname(tp)
    if type(module) is not str or type(name) is not str:
        return "?"
    if module == "builtins":
        return name
    return f"{module}.{name}"


def _kinds(items: Any) -> dict[str, int]:
    """Count the types of the first items of a builtin container."""
    counts: dict[str, int] = {}
    for item in islice(items, SAMPLE):
        name = type_name(type(item))
        counts[name] = counts.get(name, 0) + 1
    return counts


def shape(value: object) -> dict[str, Any]:
    """The shape of ``value``: its type, and for builtin containers the length
    and the types of the first items. Never any content."""
    tp = type(value)
    key = id(tp)
    if key in _SIZED:
        return {"type": _get_name(tp), "len": len(value)}  # type: ignore[arg-type]
    if key in _SEQUENCES:
        size = len(value)  # type: ignore[arg-type]
        out: dict[str, Any] = {"type": _get_name(tp), "len": size}
        if size > SAMPLE:
            out["sampled"] = SAMPLE  # `items` is about the first ones only
        if size:
            try:
                out["items"] = _kinds(value)
            except RuntimeError:  # changed by another thread while we looked
                pass
        return out
    if tp is dict:
        size = len(value)  # type: ignore[arg-type]
        out = {"type": "dict", "len": size}
        if size > SAMPLE:
            out["sampled"] = SAMPLE
        if size:
            try:
                out["keys"] = _kinds(value)
                out["values"] = _kinds(value.values())  # type: ignore[attr-defined]
            except RuntimeError:
                pass
        return out
    return {"type": type_name(tp)}
