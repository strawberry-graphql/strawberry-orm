"""Type generation tests — inherits from abstract."""

from tests.abstract.query_type_generation import (
    AbstractTestQueryCustomName,
    AbstractTestQueryIncludeExclude,
    AbstractTestQueryInputGeneration,
    AbstractTestQueryPartialGeneration,
    AbstractTestQueryTypeGeneration,
)


class TestQueryTypeGeneration(AbstractTestQueryTypeGeneration):
    pass


class TestQueryInputGeneration(AbstractTestQueryInputGeneration):
    pass


class TestQueryPartialGeneration(AbstractTestQueryPartialGeneration):
    pass


class TestQueryIncludeExclude(AbstractTestQueryIncludeExclude):
    pass


class TestQueryCustomName(AbstractTestQueryCustomName):
    pass
