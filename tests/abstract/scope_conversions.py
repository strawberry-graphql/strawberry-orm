"""A row scope means the same thing wherever the library applies it.

The root read runs the scope's own query, but everywhere else the library
turns a scope into something else first: the keys of the rows it allows, a
subquery a filter or a group joins, one load for many parents, a check of the
rows a resolver returned. Each conversion has to keep every row restriction
the scope wrote. A scope that allows no rows (``limit(0)``, the only way
Tortoise can say so, an empty ``IN``, a condition nothing meets, Django's
``.none()`` or ``[:0]``) hides every post on every surface below, and one that
narrows them by a condition, an annotation or a join hides the same posts on
each. One that keeps some number of rows, or skips some, is refused on every
surface: elsewhere its limit would count the rows of another statement, and
even on the root read a client's filter or order picks which rows it keeps.

``PostNode.scope_rows`` is the scope under test; every surface reads posts
through it. A backend provides ``orm`` and ``models`` (``User``, ``Post``,
``Tag``, ``Comment``) and, on the test class:

- ``DENY``: scopes allowing no rows, by name;
- ``NARROWING``: the controls, scopes keeping some posts by a condition, named
  as in ``NARROWED``;
- ``BOUNDED``: ``{"limit": first two posts by id, "offset": all but the first}``;
- ``run(schema, query)``, an awaitable result;
- ``read(obj, name)``, a relation a ``using=`` path loaded;
- ``rows(info, model)``, every row of *model*, as instances;
- ``nodes(info, model, ids)``, the rows with those keys, as instances;
- ``first_ten(model)``, a query for the first ten rows by key, which a scope
  narrows without refusing the limit, as it is the resolver's own.

Seed: Alice wrote "Hello World" and "GraphQL Guide", Bob "Draft Post" (the one
draft), Charlie "Rust Adventures". Bob and Alice commented on "Hello World",
Charlie on "GraphQL Guide".
"""

import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto

TITLES = ["Hello World", "GraphQL Guide", "Draft Post", "Rust Adventures"]
WORDS = dict(zip(TITLES, [100, 250, 40, 310], strict=True))
POSTS_BY = {
    "Alice": ["Hello World", "GraphQL Guide"],
    "Bob": ["Draft Post"],
    "Charlie": ["Rust Adventures"],
}
TAGGED = {
    "python": ["Hello World", "GraphQL Guide"],
    "graphql": ["GraphQL Guide"],
    "rust": ["Rust Adventures"],
}
COMMENTED = {
    "Nice post!": "Hello World",
    "Thanks!": "Hello World",
    "Great guide": "GraphQL Guide",
}
#: What each ``NARROWING`` scope lets through: the published posts, the
#: commented ones (by a count of their comments), the tagged ones (by a join to
#: their tags, each post once).
NARROWED = {
    "published": {"Hello World", "GraphQL Guide", "Rust Adventures"},
    "commented": {"Hello World", "GraphQL Guide"},
    "tagged": {"Hello World", "GraphQL Guide", "Rust Adventures"},
}
IN_TITLES = "{inList: [" + ", ".join(f'"{title}"' for title in TITLES) + "]}"


def _sorted(values):
    return sorted(values, key=lambda value: (value is None, value))


def _visible(titles, shown):
    return _sorted(title for title in titles if title in shown)


def _max_words(shown):
    return max((WORDS[title] for title in shown), default=None)


def _counted(keys):
    counts = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1
    return counts


#: ``name: (query, read the data, what it shows when the posts in *v* are visible)``.
SURFACES = {
    "root_list": (
        "{ posts { title } }",
        lambda data: _sorted(post["title"] for post in data["posts"]),
        lambda v: _sorted(v),
    ),
    "root_filtered": (
        "{ posts(filter: {field: {title: "
        '{inList: ["Draft Post", "Rust Adventures"]}}}) { title } }',
        lambda data: _sorted(post["title"] for post in data["posts"]),
        lambda v: _visible(["Draft Post", "Rust Adventures"], v),
    ),
    "root_ordered": (
        "{ posts(order: [{field: {title: DESC}}]) { title } }",
        lambda data: [post["title"] for post in data["posts"]],
        lambda v: sorted(v, reverse=True),
    ),
    "total_count": (
        "{ postConn { totalCount } }",
        lambda data: data["postConn"]["totalCount"],
        lambda v: len(v),
    ),
    "edges": (
        "{ postConn(first: 10) { edges { node { title } } } }",
        lambda data: _sorted(e["node"]["title"] for e in data["postConn"]["edges"]),
        lambda v: _sorted(v),
    ),
    "aggregates": (
        "{ postConn { aggregates { count max { wordCount } } } }",
        lambda data: (
            data["postConn"]["aggregates"]["count"],
            data["postConn"]["aggregates"]["max"]["wordCount"],
        ),
        lambda v: (len(v), _max_words(v)),
    ),
    "groups": (
        "{ postConn(groupBy: [{field: {title: true}}]) {"
        " groups { key { title } aggregates { count } } } }",
        lambda data: _sorted(g["key"]["title"] for g in data["postConn"]["groups"]),
        lambda v: _sorted(v),
    ),
    "group_items": (
        "{ postConn(groupBy: [{field: {isPublished: true}}]) {"
        " groups { items(first: 10) { edges { node { title } } } } } }",
        lambda data: _sorted(
            edge["node"]["title"]
            for group in data["postConn"]["groups"]
            for edge in group["items"]["edges"]
        ),
        lambda v: _sorted(v),
    ),
    "forward_to_one": (
        "{ comments { body post { title } } }",
        lambda data: {
            c["body"]: c["post"] and c["post"]["title"] for c in data["comments"]
        },
        lambda v: {
            body: title if title in v else None for body, title in COMMENTED.items()
        },
    ),
    "reverse_to_many": (
        "{ users { name posts { title } } }",
        lambda data: {
            u["name"]: _sorted(p["title"] for p in u["posts"]) for u in data["users"]
        },
        lambda v: {name: _visible(titles, v) for name, titles in POSTS_BY.items()},
    ),
    "many_to_many": (
        "{ tags { name posts { title } } }",
        lambda data: {
            t["name"]: _sorted(p["title"] for p in t["posts"]) for t in data["tags"]
        },
        lambda v: {name: _visible(titles, v) for name, titles in TAGGED.items()},
    ),
    "using_hop": (
        "{ users { name postTitles } }",
        lambda data: {u["name"]: u["postTitles"] for u in data["users"]},
        lambda v: {name: _visible(titles, v) for name, titles in POSTS_BY.items()},
    ),
    "relation_connection": (
        "{ userNodes { name posts(first: 10) {"
        " totalCount edges { node { title } } } } }",
        lambda data: {
            u["name"]: (
                u["posts"]["totalCount"],
                _sorted(e["node"]["title"] for e in u["posts"]["edges"]),
            )
            for u in data["userNodes"]
        },
        lambda v: {
            name: (len(_visible(titles, v)), _visible(titles, v))
            for name, titles in POSTS_BY.items()
        },
    ),
    "filter_to_many": (
        "{ users(filter: {object: {posts: {field: {title: "
        + IN_TITLES
        + "}}}}) { name } }",
        lambda data: _sorted(u["name"] for u in data["users"]),
        lambda v: _sorted(
            name for name, titles in POSTS_BY.items() if set(titles) & set(v)
        ),
    ),
    "filter_to_one": (
        "{ comments(filter: {object: {post: {field: {title: "
        + IN_TITLES
        + "}}}}) { body } }",
        lambda data: _sorted(c["body"] for c in data["comments"]),
        lambda v: _sorted(body for body, title in COMMENTED.items() if title in v),
    ),
    "filter_to_one_key": (
        "{ comments(filter: {object: {post: {field: {id: {inList: [1, 2, 3, 4]}}}}})"
        " { body } }",
        lambda data: _sorted(c["body"] for c in data["comments"]),
        lambda v: _sorted(body for body, title in COMMENTED.items() if title in v),
    ),
    "not_filter_to_many": (
        "{ users(filter: {not: {object: {posts: {field: {title: "
        + IN_TITLES
        + "}}}}}) { name } }",
        lambda data: _sorted(u["name"] for u in data["users"]),
        lambda v: _sorted(
            name for name, titles in POSTS_BY.items() if not set(titles) & set(v)
        ),
    ),
    "has_to_many": (
        "{ users(filter: {object: {posts: {isNull: false}}}) { name } }",
        lambda data: _sorted(u["name"] for u in data["users"]),
        lambda v: _sorted(
            name for name, titles in POSTS_BY.items() if set(titles) & set(v)
        ),
    ),
    "lacks_to_many": (
        "{ users(filter: {object: {posts: {isNull: true}}}) { name } }",
        lambda data: _sorted(u["name"] for u in data["users"]),
        lambda v: _sorted(
            name for name, titles in POSTS_BY.items() if not set(titles) & set(v)
        ),
    ),
    "has_to_one": (
        "{ comments(filter: {object: {post: {isNull: false}}}) { body } }",
        lambda data: _sorted(c["body"] for c in data["comments"]),
        lambda v: _sorted(body for body, title in COMMENTED.items() if title in v),
    ),
    "group_key_to_one": (
        "{ commentConn(groupBy: [{object: {post: {field: {title: true}}}}]) {"
        " groups { key { post { title } } aggregates { count } } } }",
        lambda data: {
            g["key"]["post"]["title"]: g["aggregates"]["count"]
            for g in data["commentConn"]["groups"]
        },
        lambda v: _counted(
            title if title in v else None for title in COMMENTED.values()
        ),
    ),
    "group_key_objects": (
        "{ userConn(groupBy: [{objects: {posts: {field: {title: true}}}}]) {"
        " groups { key { objects { posts { title } } } aggregates { count } } } }",
        lambda data: {
            tuple(_sorted(p["title"] for p in g["key"]["objects"]["posts"])): g[
                "aggregates"
            ]["count"]
            for g in data["userConn"]["groups"]
        },
        lambda v: _counted(tuple(_visible(t, v)) for t in POSTS_BY.values()),
    ),
    "resolver_rows": (
        "{ postRows { title } }",
        lambda data: _sorted(post["title"] for post in data["postRows"]),
        lambda v: _sorted(v),
    ),
    "resolver_limit": (
        "{ firstPosts { title } }",
        lambda data: _sorted(post["title"] for post in data["firstPosts"]),
        lambda v: _sorted(v),
    ),
    "resolver_limit_groups": (
        "{ firstPostConn(groupBy: [{field: {title: true}}]) {"
        " groups { key { title } aggregates { count } } } }",
        lambda data: _sorted(
            g["key"]["title"] for g in data["firstPostConn"]["groups"]
        ),
        lambda v: _sorted(v),
    ),
    "relay_node": (
        "{ nodes(ids: ["
        + ", ".join(f'"{relay.to_base64("PostNode", pk)}"' for pk in range(1, 5))
        + "]) { ... on PostNode { title } } }",
        lambda data: [node and node["title"] for node in data["nodes"]],
        lambda v: [title if title in v else None for title in TITLES],
    ),
}

#: A bounded scope's refusal, as a client reads it.
REFUSED = (
    "A row scope cannot limit or offset the rows it allows: the library applies "
    "it under a client's filter and order, to relations and within other "
    "statements, where the limit would count other rows. Narrow the rows with "
    "a condition, or limit them to none."
)


class AbstractTestScopeConversions:
    DENY: dict = {}
    NARROWING: dict = {}
    BOUNDED: dict = {}
    #: Surfaces the backend has no way to offer.
    UNSUPPORTED: frozenset = frozenset()

    def pytest_generate_tests(self, metafunc):
        if "surface" in metafunc.fixturenames:
            metafunc.parametrize("surface", sorted(set(SURFACES) - self.UNSUPPORTED))
        if "deny" in metafunc.fixturenames:
            metafunc.parametrize("deny", sorted(self.DENY))
        if "narrowing" in metafunc.fixturenames:
            metafunc.parametrize("narrowing", sorted(self.NARROWING))
        if "bound" in metafunc.fixturenames:
            metafunc.parametrize("bound", sorted(self.BOUNDED))

    def schema(self, orm, models, scope):
        User, Post, Tag, Comment = models.User, models.Post, models.Tag, models.Comment
        read, rows, nodes, first_ten = self.read, self.rows, self.nodes, self.first_ten
        post_filter = orm.filter(Post)
        post_group = orm.group(Post)

        @orm.type(
            Post,
            filters=post_filter,
            order=orm.order(Post),
            group=post_group,
            aggregate=orm.aggregate(Post),
        )
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto
            word_count: auto
            is_published: auto

            @classmethod
            def scope_rows(cls, query, info):
                return scope(query)

            @classmethod
            def resolve_nodes(cls, *, info, node_ids, required=False):
                return nodes(info, Post, [int(pk) for pk in node_ids])

        @orm.type(Comment, filters=orm.filter(Comment), group=orm.group(Comment))
        class CommentNode(relay.Node):
            id: relay.NodeID[int]
            body: auto
            post: PostNode | None

        @orm.type(User, filters=orm.filter(User), name="Author")
        class Author:
            id: auto
            name: auto
            posts: list[PostNode]

            @orm.field.lazy(using=["posts"])
            def post_titles(self, info: strawberry.Info) -> list[str]:
                return sorted(post.title for post in read(self, "posts"))

        @orm.type(User, group=orm.group(User))
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: ORMListConnection[PostNode] = orm.connection.eager()

        @orm.type(Tag)
        class TagType:
            id: auto
            name: auto
            posts: list[PostNode]

        @strawberry.type
        class Query:
            nodes: list[relay.Node | None] = relay.node()
            posts: list[PostNode] = orm.field.eager()
            post_conn: ORMListConnection[PostNode] = orm.connection.eager()
            comments: list[CommentNode] = orm.field.eager()
            comment_conn: ORMListConnection[CommentNode] = orm.connection.eager()
            users: list[Author] = orm.field.eager()
            user_nodes: list[UserNode] = orm.field.eager()
            user_conn: ORMListConnection[UserNode] = orm.connection.eager()
            tags: list[TagType] = orm.field.eager()

            @strawberry.field
            def post_rows(self, info: strawberry.Info) -> list[PostNode]:
                return rows(info, Post)

            @orm.field.lazy
            def first_posts(self, info: strawberry.Info) -> list[PostNode]:
                return first_ten(Post)

            @orm.connection.lazy(ORMListConnection[PostNode])
            def first_post_conn(self, info: strawberry.Info) -> list[PostNode]:
                return first_ten(Post)

        return orm.schema(query=Query)

    async def assert_shows(self, orm, models, scope, surface, visible):
        query, extract, expected = SURFACES[surface]
        result = await self.run(self.schema(orm, models, scope), query)
        assert result.errors is None, result.errors
        shown, wanted = extract(result.data), expected(visible)
        assert shown == wanted, f"shows {shown!r}, not {wanted!r}"

    async def test_no_scope_shows_every_post(self, orm, models, surface):
        await self.assert_shows(orm, models, lambda query: query, surface, TITLES)

    async def test_a_narrowing_scope_hides_the_same_posts_everywhere(
        self, orm, models, narrowing, surface
    ):
        scope = self.NARROWING[narrowing]
        await self.assert_shows(orm, models, scope, surface, NARROWED[narrowing])

    async def test_a_scope_allowing_no_rows_hides_every_post(
        self, orm, models, deny, surface
    ):
        await self.assert_shows(orm, models, self.DENY[deny], surface, set())

    async def test_a_bounded_scope_is_refused(self, orm, models, bound, surface):
        schema = self.schema(orm, models, self.BOUNDED[bound])
        result = await self.run(schema, SURFACES[surface][0])
        assert result.errors, f"shows {SURFACES[surface][1](result.data)!r}"
        assert {error.message for error in result.errors} == {REFUSED}, result.errors
