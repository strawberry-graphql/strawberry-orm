"""Filter nested condition tests — inherits from abstract."""

from tests.abstract.query_filter_nested_conditions import (
    AbstractTestQueryFilterComplexScenarios,
    AbstractTestQueryFilterDeeplyNested,
    AbstractTestQueryFilterMultiField,
    AbstractTestQueryFilterMultiOperatorLookup,
)


class TestQueryFilterMultiField(AbstractTestQueryFilterMultiField):
    pass


class TestQueryFilterMultiOperatorLookup(AbstractTestQueryFilterMultiOperatorLookup):
    pass


class TestQueryFilterDeeplyNested(AbstractTestQueryFilterDeeplyNested):
    pass


class TestQueryFilterComplexScenarios(AbstractTestQueryFilterComplexScenarios):
    pass
