"""Auto-resolution tests — inherits from abstract."""

from tests.abstract.query_auto_resolution import (
    AbstractTestQueryRelationshipAutoResolution,
    AbstractTestQueryScalarAutoResolution,
)


class TestQueryScalarAutoResolution(AbstractTestQueryScalarAutoResolution):
    pass


class TestQueryRelationshipAutoResolution(AbstractTestQueryRelationshipAutoResolution):
    pass
