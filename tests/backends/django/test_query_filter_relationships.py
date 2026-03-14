"""Filter/order on relationship fields tests — inherits from abstract."""

from tests.abstract.query_filter_relationships import (
    AbstractTestQueryFilterAndOrderRelationships,
    AbstractTestQueryFilterRelationships,
    AbstractTestQueryOrderRelationships,
)


class TestQueryFilterRelationships(AbstractTestQueryFilterRelationships):
    pass


class TestQueryOrderRelationships(AbstractTestQueryOrderRelationships):
    pass


class TestQueryFilterAndOrderRelationships(AbstractTestQueryFilterAndOrderRelationships):
    pass
