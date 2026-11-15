"""The kitchen-sink schema, identical on every backend.

``current.backend`` supplies the models and the few backend-specific pieces
(scopes and hand-written resolvers); everything else is the library's.
"""

import datetime

import strawberry
from strawberry import relay
from strawberry.schema.config import StrawberryConfig

from benchmarks.kitchen_sink import current
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto

B = current.backend
orm = B.orm

# Leaf models first: a relation is offered under object: only once its target
# is registered.
filters, orders, groups = {}, {}, {}
# Every post and comment in the data has an author; ordering through one is
# allowed even though inactive users are scoped out.
order_options = {
    B.Comment: {"allow_scoped_ordering": ["author"]},
    B.Post: {"allow_scoped_ordering": ["author"], "exclude": ["comments"]},
}
for model in (B.Organization, B.Category, B.Tag, B.User, B.Comment, B.Post):
    filters[model] = orm.filter(model)
    orders[model] = orm.order(model, **order_options.get(model, {}))
    groups[model] = orm.group(model)


def _type(model):
    return orm.type(
        model, filters=filters[model], order=orders[model], group=groups[model]
    )


@_type(B.Organization)
class OrgNode:
    id: auto
    name: auto
    country: auto
    founded: auto


@orm.type(B.Category)
class ParentCategoryNode:
    id: auto
    name: auto


@_type(B.Category)
class CategoryNode:
    id: auto
    name: auto
    parent: ParentCategoryNode | None


@_type(B.Tag)
class TagNode:
    id: auto
    name: auto


# Connections must name their node class directly (a forward reference gets
# no generated arguments), so the user type comes last.


@_type(B.Comment)
class CommentNode(relay.Node):
    id: relay.NodeID[int]
    body: auto
    upvotes: auto
    is_flagged: auto
    created_at: auto
    author: "UserNode | None"
    post: "PostNode | None"

    @classmethod
    def scope_rows(cls, query, info):
        return B.visible_comments(query)


@_type(B.Post)
class PostNode(relay.Node):
    id: relay.NodeID[int]
    title: auto
    body: auto
    status: auto
    views: auto
    rating: auto
    # Django's and Tortoise's auto() type a nullable column as non-null.
    published_at: datetime.datetime | None
    author: "UserNode | None"
    category: CategoryNode | None
    tags: list[TagNode]
    comments: list[CommentNode]

    @orm.field.lazy
    def top_comments(self, info: strawberry.Info) -> list[CommentNode]:
        return B.top_comments(self)

    @classmethod
    def scope_rows(cls, query, info):
        return B.visible_posts(query)


@_type(B.User)
class UserNode(relay.Node):
    id: relay.NodeID[int]
    name: auto
    email: auto
    is_active: auto
    score: auto
    joined_at: auto
    organization: OrgNode | None
    posts: ORMListConnection[PostNode] = orm.connection.eager()
    if getattr(B, "EAGER_VIEWS", True):
        recent_posts: list[PostNode] = orm.field.eager(on="posts", scope=B.newest_first)
    else:
        # Tortoise can serve an on= view only lazily: the same rows, per parent.
        @orm.field.lazy
        def recent_posts(self, info: strawberry.Info) -> list[PostNode]:
            return B.newest_first(B.posts_by(self), info)

    @orm.connection.lazy(ORMListConnection[PostNode])
    def posts_lazy(self, info: strawberry.Info) -> list[PostNode]:
        return B.posts_by(self)

    @classmethod
    def scope_rows(cls, query, info):
        return B.active_users(query)


@strawberry.type
class Query:
    posts: ORMListConnection[PostNode] = orm.connection.eager()
    users: ORMListConnection[UserNode] = orm.connection.eager()
    comments: ORMListConnection[CommentNode] = orm.connection.eager()


schema = orm.schema(query=Query, config=StrawberryConfig(relay_max_results=1000))
