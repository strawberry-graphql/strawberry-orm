"""The pieces of the per-schema input cut that no backend reaches on its own."""

import pytest
from strawberry.types.base import StrawberryList, StrawberryOptional
from strawberry.types.lazy_type import LazyType

from strawberry_orm.cover import build_covered, named_type


class Named:
    pass


def test_named_type_unwraps_lazy_types_lists_and_optionals():
    lazy = LazyType("Named", __name__, None)
    assert named_type(StrawberryOptional(StrawberryList(lazy))) is Named


def test_a_type_with_an_of_type_attribute_is_not_taken_for_a_list():
    class Shipment:
        of_type = "parcel"

    assert named_type(Shipment) is Shipment


def test_a_schema_failing_for_another_reason_reports_its_own_error():
    class Broken:
        def __init__(self, **kwargs):
            raise TypeError("not the cut")

    with pytest.raises(TypeError, match="not the cut"):
        build_covered(Broken, {})
