"""The name a connection type is published under.

Clients write it into fragments, ``__typename`` checks, persisted queries and
cache policies, and an operation that names a renamed type still validates,
so a rename breaks them without any error. This pins the name.
"""

import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection


@strawberry.type
class PostType(relay.Node):
    id: relay.NodeID[int]


def test_an_orm_list_connection_is_published_as_its_node_and_class_name():
    @strawberry.type
    class Query:
        posts: ORMListConnection[PostType]

    sdl = str(strawberry.Schema(query=Query))

    assert "type PostTypeORMListConnection {" in sdl
    assert "posts: PostTypeORMListConnection!" in sdl
