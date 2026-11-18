from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from strawberry_orm.backends._base import signed_value


class Shown(int):
    """An int whose repr is not its value."""

    def __repr__(self) -> str:
        return "1"


@pytest.mark.parametrize(
    ("one", "other"),
    [
        (True, 1),
        (1, 1.0),
        (Shown(2), 1),
        ("1", b"1"),
        (Decimal("1.0"), Decimal("1.00")),
        (date(2026, 1, 1), datetime(2026, 1, 1)),
        (UUID(int=1), UUID(int=2)),
        (memoryview(b"a"), memoryview(b"b")),
        (None, "None"),
        ([1, 2], (1, 2)),
    ],
)
def test_values_read_apart_are_signed_apart(one, other):
    assert signed_value(one) != signed_value(other)


@pytest.mark.parametrize(
    "value", [None, 1, "a", UUID(int=1), memoryview(b"a"), (1, [None, "a"])]
)
def test_equal_values_are_signed_alike(value):
    assert signed_value(value) == signed_value(value)


@pytest.mark.parametrize("value", [object(), [1, object()], {"a": 1}])
def test_a_value_it_cannot_read_has_no_signature(value):
    assert signed_value(value) is None
