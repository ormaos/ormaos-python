from typing import Any

from ormaos._shape import shape, type_name


class Loud:
    """Any of these running would mean we executed the observed program's code."""

    def __len__(self) -> int:
        raise AssertionError("__len__ was called")

    def __repr__(self) -> str:
        raise AssertionError("__repr__ was called")

    def __iter__(self) -> Any:
        raise AssertionError("__iter__ was called")


class LoudList(list[int]):
    def __len__(self) -> int:
        raise AssertionError("__len__ was called")


def test_scalars_have_only_a_type() -> None:
    assert shape(42) == {"type": "int"}
    assert shape(None) == {"type": "NoneType"}
    assert shape(True) == {"type": "bool"}
    assert shape(1.5) == {"type": "float"}


def test_text_has_length_and_no_content() -> None:
    assert shape("hunter2") == {"type": "str", "len": 7}
    assert shape(b"abc") == {"type": "bytes", "len": 3}


def test_containers_have_length_and_item_types() -> None:
    assert shape([1, 2, "x"]) == {
        "type": "list",
        "len": 3,
        "items": {"int": 2, "str": 1},
    }
    assert shape(()) == {"type": "tuple", "len": 0}
    assert shape({"user": "ana", "age": 3}) == {
        "type": "dict",
        "len": 2,
        "keys": {"str": 2},
        "values": {"str": 1, "int": 1},
    }


def test_big_containers_are_sampled() -> None:
    assert shape(list(range(1000))) == {
        "type": "list",
        "len": 1000,
        "sampled": 50,  # `items` is about the first 50 only
        "items": {"int": 50},
    }
    assert "sampled" not in shape(list(range(50)))


def test_objects_are_named_by_type_and_never_run() -> None:
    assert shape(Loud()) == {"type": f"{__name__}.Loud"}
    assert shape(LoudList([1])) == {"type": f"{__name__}.LoudList"}
    assert shape([Loud()])["items"] == {f"{__name__}.Loud": 1}


def test_type_name() -> None:
    assert type_name(int) == "int"
    assert type_name(Loud) == f"{__name__}.Loud"
