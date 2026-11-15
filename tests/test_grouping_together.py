"""How ``objects:`` is read: the paths it makes, and the values JSON hands back."""

import dataclasses
import datetime
import uuid
from decimal import Decimal
from typing import Any

import pytest
import strawberry

from strawberry_orm.grouping import (
    GroupHop,
    SetOrder,
    from_json,
    group_paths,
    order_terms,
)
from strawberry_orm.types import Ordering


class _Tag:
    pass


class _Backend:
    def __init__(self, many: bool = True) -> None:
        self.many = many

    def relation_hop(self, model: type, relation: str) -> GroupHop:
        return GroupHop(relation=relation, model=model, target=_Tag, many=self.many)

    def together_reader(self, model: type, column: str, interval: Any) -> Any:
        return str


@dataclasses.dataclass
class _Field:
    name: Any = strawberry.UNSET


@dataclasses.dataclass
class _Objects:
    tags: Any = strawberry.UNSET
    comments: Any = strawberry.UNSET


@dataclasses.dataclass
class _Object:
    author: Any = strawberry.UNSET


@dataclasses.dataclass
class _GroupBy:
    field: Any = strawberry.UNSET
    object: Any = strawberry.UNSET
    objects: Any = strawberry.UNSET


def _by_tag_names() -> _GroupBy:
    return _GroupBy(objects=_Objects(tags=_GroupBy(field=_Field(name=True))))


def test_objects_makes_one_path_per_relation():
    (path,) = group_paths(_Backend(), _Tag, [_by_tag_names(), _by_tag_names()])
    assert (path.alias, path.name, path.columns) == ("_g0", "tags", ("name",))


def test_objects_needs_a_to_many_relation():
    with pytest.raises(ValueError, match="rows of one of its own to-many"):
        group_paths(_Backend(many=False), _Tag, _by_tag_names())


def test_objects_cannot_be_reached_through_object():
    entry = _GroupBy(object=_Object(author=_by_tag_names()))
    with pytest.raises(ValueError, match="rows of one of its own to-many"):
        group_paths(_Backend(), _Tag, entry)


def test_the_related_rows_are_grouped_by_their_own_columns():
    nested = _GroupBy(object=_Object(author=_GroupBy(field=_Field(name=True))))
    entry = _GroupBy(objects=_Objects(tags=nested))
    with pytest.raises(ValueError, match="through object"):
        group_paths(_Backend(), _Tag, entry)


@dataclasses.dataclass
class _Columns:
    id: Any = strawberry.UNSET
    name: Any = strawberry.UNSET


def _order_by_tags(**columns: Ordering) -> _GroupBy:
    return _GroupBy(objects=_Objects(tags=_GroupBy(field=_Columns(**columns))))


def test_a_relation_ordered_by_twice_is_one_set_where_it_is_first_named():
    title = _GroupBy(field=_Field(name=Ordering.ASC))
    by_id = _order_by_tags(id=Ordering.DESC)
    by_name = _order_by_tags(name=Ordering.DESC_NULLS_LAST)
    first, by_set, *entries = order_terms(_Backend(), _Tag, [title, by_id, by_name])
    assert first is title
    assert isinstance(by_set, SetOrder)
    assert by_set.descending
    path = by_set.path
    assert (path.alias, path.name, path.columns) == ("_s0", "tags", ("id", "name"))
    assert entries == [by_id, by_name]


def test_an_order_without_objects_is_left_as_it_is():
    entry = _GroupBy(field=_Field(name=Ordering.ASC))
    assert order_terms(_Backend(), _Tag, entry) == [entry]


def test_a_set_is_ordered_one_way():
    entries = [_order_by_tags(id=Ordering.ASC), _order_by_tags(name=Ordering.DESC)]
    with pytest.raises(ValueError, match="both ascending and descending"):
        order_terms(_Backend(), _Tag, entries)


def test_ordering_by_objects_needs_a_to_many_relation():
    with pytest.raises(ValueError, match="rows of one of its own to-many"):
        order_terms(_Backend(many=False), _Tag, _order_by_tags(name=Ordering.ASC))


def test_ordering_by_objects_cannot_be_reached_through_object():
    entry = _GroupBy(object=_Object(author=_order_by_tags(name=Ordering.ASC)))
    with pytest.raises(ValueError, match=r"objects: \{tags\} through author"):
        order_terms(_Backend(), _Tag, [entry])


def test_the_related_rows_are_ordered_by_their_own_columns():
    nested = _GroupBy(object=_Object(author=_GroupBy(field=_Field(name=Ordering.ASC))))
    with pytest.raises(ValueError, match="through object"):
        order_terms(_Backend(), _Tag, _GroupBy(objects=_Objects(tags=nested)))


@pytest.mark.parametrize(
    ("value", "kind", "expected"),
    [
        (None, int, None),
        (1, None, 1),
        (1, bool, True),
        (2, float, 2.0),
        (Decimal("1.50"), Decimal, Decimal("1.50")),
        (str(uuid.UUID(int=1)), uuid.UUID, uuid.UUID(int=1)),
        (
            "2026-01-15T00:00:00-08:00",
            datetime.datetime,
            datetime.datetime(2026, 1, 15, 8, tzinfo=datetime.UTC),
        ),
        ("2026-01-01", datetime.date, datetime.date(2026, 1, 1)),
        ("2026-01-01T00:00:00+00:00", datetime.date, datetime.date(2026, 1, 1)),
        ("10:30:00", datetime.time, datetime.time(10, 30)),
        ("python", str, "python"),
    ],
)
def test_a_value_reads_as_its_column_does(value, kind, expected):
    assert from_json(value, kind) == expected
