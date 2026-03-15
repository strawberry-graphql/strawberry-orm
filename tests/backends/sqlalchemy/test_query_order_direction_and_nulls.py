"""Ordering direction, nulls handling, and tie-breaking tests — inherits from abstract."""

from tests.abstract.query_order_direction_and_nulls import (
    AbstractTestQueryOrderDirection,
    AbstractTestQueryOrderNulls,
    AbstractTestQueryOrderTieBreaking,
)


class TestQueryOrderDirection(AbstractTestQueryOrderDirection):
    pass


class TestQueryOrderTieBreaking(AbstractTestQueryOrderTieBreaking):
    pass


class TestQueryOrderNulls(AbstractTestQueryOrderNulls):
    pass
