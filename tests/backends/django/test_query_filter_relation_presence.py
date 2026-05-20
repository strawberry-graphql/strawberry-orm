"""FK presence filter tests for Django — inherits from abstract."""

from tests.abstract.query_filter_relation_presence import (
    AbstractTestQueryFilterRelationPresence,
)
from tests.abstract.query_filter_relation_presence_custom_pk import (
    AbstractTestQueryFilterRelationPresenceCustomPk,
)
from tests.backends.django.custom_pk_fixtures import (  # noqa: F401
    Book,
    Publisher,
    custom_pk_execute,
    custom_pk_orm,
    custom_pk_seed,
)


class TestQueryFilterRelationPresence(AbstractTestQueryFilterRelationPresence):
    pass


class TestQueryFilterRelationPresenceCustomPk(
    AbstractTestQueryFilterRelationPresenceCustomPk
):
    pass
