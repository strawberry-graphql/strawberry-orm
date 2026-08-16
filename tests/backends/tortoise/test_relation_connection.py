"""A connection over a relation is refused on Tortoise.

Taking each parent's page in one query needs a window function to number rows
within each parent. Tortoise's ``batch_group_items`` loops per group instead,
so every parent would cost its own query and the field could not be eager.
"""

import pytest
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.backends.tortoise.models import Post as TPost
from tests.backends.tortoise.models import User as TUser


def _orm():
    return StrawberryORM.for_tortoise(warn_missing_scope=False, lazy_resolution="off")


class TestRelationConnectionIsRefused:
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        """Tortoise has to be initialised before relations are introspectable."""

    def _post_node(self, orm):
        @orm.type(TPost, filters=orm.filter(TPost), order=orm.order(TPost))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        return PostNode

    def test_it_is_refused_when_the_type_is_defined(self):
        orm = _orm()
        PostNode = self._post_node(orm)

        with pytest.raises(ValueError, match="needs a window function"):

            @orm.type(TUser)
            class UserNode(relay.Node):
                id: relay.NodeID[int]
                posts: ORMListConnection[PostNode] = orm.connection.eager()

    def test_the_message_points_at_lazy(self):
        orm = _orm()
        PostNode = self._post_node(orm)

        with pytest.raises(ValueError, match="orm.connection.lazy"):

            @orm.type(TUser)
            class UserNode(relay.Node):
                id: relay.NodeID[int]
                posts: ORMListConnection[PostNode] = orm.connection.eager()
