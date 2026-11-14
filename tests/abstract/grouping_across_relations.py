"""Grouping by a related row, by a period of a date, and by a ``@group_field``.

A group is keyed by what the client asked for: a column of the model, a column
of a related model reached through ``object:``, a date cut to the start of its
interval, or what a ``@group_field`` method returns. Groups come in the order
the connection reads its rows in, so each one's edges sit together, and each
group's ``edgeIndices`` points at exactly its own edges.

A related row the related type's ``scope_rows`` hides reads as ``None``, as it
does on the type, so its rows fall in the null group. Across a to-many relation
a row is grouped once for each related row it has, and once under ``None`` if it
has none the caller may see.

Backends supply:

``orm``, ``models``                 the ORM and ``User``, ``Post``, ``Tag``
``run(schema, query)``              awaitable result of running *query*
``hide(model, name, query)``        *query* without the rows named *name*
``by_title(Post)``                  every post, ordered by title
``author_column(Post)``             what a ``@group_field`` returns to group
                                    posts by their author's key
``date_posts(dates)``               awaitable; sets each post's ``created_at``
                                    from ``{post id: datetime}``

The seed: Alice wrote posts 1 and 2, Bob post 3 (a draft), Charlie post 4.
Post 1 is tagged python; post 2 python and graphql; post 3 nothing; post 4
rust.
"""

import base64
import datetime

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import group_field
from strawberry_orm.grouping import GroupPath
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import DateGroupByInterval, auto


def cursor(offset: int) -> str:
    return base64.b64encode(f"arrayconnection:{offset}".encode()).decode()


def fields(schema, type_name):
    graphql_type = schema._schema.type_map.get(type_name)
    return set(graphql_type.fields) if graphql_type is not None else set()


def _post_node(orm, models, hide, group=None, scoped=True):
    """``PostNode`` showing its author and tags; Bob and ``graphql`` are hidden."""
    User, Post, Tag = models.User, models.Post, models.Tag
    orm.group(User)
    orm.group(Tag)
    orm.order(User)
    orm.order(Tag)

    @orm.type(User)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

        @classmethod
        def scope_rows(cls, qs, info):
            return hide(User, "Bob", qs) if scoped else qs

    @orm.type(Tag)
    class TagNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

        @classmethod
        def scope_rows(cls, qs, info):
            return hide(Tag, "graphql", qs) if scoped else qs

    @orm.type(
        Post,
        filters=orm.filter(Post),
        order=orm.order(Post, allow_scoped_ordering=["author", "tags"]),
        group=group or orm.group(Post),
    )
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        is_published: auto
        created_at: auto
        author: UserNode | None
        tags: list[TagNode]

    return PostNode


def _posts_schema(orm, models, hide, **kwargs):
    PostNode = _post_node(orm, models, hide, **kwargs)

    @strawberry.type
    class Query:
        posts: ORMListConnection[PostNode] = orm.connection.eager()

    return orm.schema(query=Query)


def _posts(group_by, order="[{field: {id: ASC}}]", groups="key { author { id } }"):
    return f"""{{ posts(groupBy: {group_by}, order: {order}) {{
        edges {{ node {{ title }} }}
        groups {{ {groups} aggregates {{ count sum {{ id }} }} edgeIndices }} }} }}"""


def _summary(result, key=lambda key: key):
    assert result.errors is None, result.errors
    data = result.data["posts"]
    return [
        (key(group["key"]), group["aggregates"]["count"], group["edgeIndices"])
        for group in data["groups"]
    ]


def _tag_names(key):
    return [tag["name"] for tag in key["objects"]["tags"]]


def _titles(result):
    return [edge["node"]["title"] for edge in result.data["posts"]["edges"]]


@pytest.mark.asyncio
class AbstractTestGroupingAcrossRelations:
    async def test_the_input_offers_the_relations_the_type_shows(
        self, orm, models, hide
    ):
        schema = _posts_schema(orm, models, hide)
        assert {"author", "tags"} <= fields(schema, "PostGroupByObject")
        assert {"author", "tags"} <= fields(schema, "PostGroupKey")
        assert fields(schema, "UserGroupKey") == {"id", "name"}

    async def test_a_related_key_is_the_one_its_own_connection_reports(
        self, orm, models
    ):
        """One ``UserGroupKey``, though users carry aggregates of their own."""
        User, Post = models.User, models.Post

        @orm.aggregate_type(User)
        class UserAggregates:
            id: auto

        @orm.type(User, group=orm.group(User), aggregate=UserAggregates)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

        @orm.type(Post, group=orm.group(Post))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            author: UserNode | None

        @strawberry.type
        class Query:
            users: ORMListConnection[UserNode] = orm.connection.eager()
            posts: ORMListConnection[PostNode] = orm.connection.eager()
            drafts: ORMListConnection[PostNode] = orm.connection.eager()

        schema = orm.schema(query=Query)
        assert fields(schema, "PostGroupKey") >= {"author"}
        assert fields(schema, "UserGroupKey") == {"id", "name"}

    async def test_groups_by_a_foreign_key(self, orm, models, hide, run):
        schema = _posts_schema(orm, models, hide)
        result = await run(schema, _posts("[{object: {author: {field: {id: true}}}}]"))
        assert _summary(result) == [
            ({"author": {"id": "1"}}, 2, [0, 1]),
            # Bob is hidden, so his post reads as having no author.
            ({"author": {"id": None}}, 1, [2]),
            ({"author": {"id": "3"}}, 1, [3]),
        ]
        sums = [
            group["aggregates"]["sum"]["id"] for group in result.data["posts"]["groups"]
        ]
        assert sums == [3, 3, 4]

    async def test_groups_by_a_column_of_the_related_row(self, orm, models, hide, run):
        schema = _posts_schema(orm, models, hide)
        query = _posts(
            "[{object: {author: {field: {id: true}}}}, "
            "{object: {author: {field: {name: true}}}}]",
            groups="key { author { id name } }",
        )
        assert _summary(await run(schema, query)) == [
            ({"author": {"id": "1", "name": "Alice"}}, 2, [0, 1]),
            ({"author": {"id": None, "name": None}}, 1, [2]),
            ({"author": {"id": "3", "name": "Charlie"}}, 1, [3]),
        ]

    @pytest.mark.parametrize(
        ("direction", "titles"),
        [
            ("ASC", ["Hello World", "GraphQL Guide", "Draft Post", "Rust Adventures"]),
            ("DESC", ["Rust Adventures", "Draft Post", "Hello World", "GraphQL Guide"]),
        ],
    )
    async def test_groups_come_in_the_order_of_the_edges(
        self, orm, models, hide, run, direction, titles
    ):
        schema = _posts_schema(orm, models, hide, scoped=False)
        query = _posts(
            "[{object: {author: {field: {name: true}}}}]",
            order=f"[{{object: {{author: {{field: {{name: {direction}}}}}}}}}, "
            "{field: {id: ASC}}]",
            groups="key { author { name } } items(first: 1) { edges { node { title } } }",
        )
        result = await run(schema, query)
        assert _titles(result) == titles
        firsts = {
            group["key"]["author"]["name"]: group["items"]["edges"][0]["node"]["title"]
            for group in result.data["posts"]["groups"]
        }
        assert firsts == {
            "Alice": "Hello World",
            "Bob": "Draft Post",
            "Charlie": "Rust Adventures",
        }
        groups = _summary(result, key=lambda key: key["author"]["name"])
        names = ["Alice", "Bob", "Charlie"]
        assert [name for name, _, _ in groups] == (
            names if direction == "ASC" else names[::-1]
        )
        # Each group's edges sit together, one group after another.
        assert [i for _, _, indices in groups for i in indices] == [0, 1, 2, 3]

    async def test_groups_by_each_row_of_a_many_to_many(self, orm, models, hide, run):
        schema = _posts_schema(orm, models, hide)
        query = _posts(
            "[{object: {tags: {field: {name: true}}}}]",
            groups="key { tags { name } }",
        )
        assert _summary(await run(schema, query), lambda k: k["tags"]["name"]) == [
            # Post 2's graphql tag is hidden, so it is grouped under python alone.
            ("python", 2, [0, 1]),
            # The untagged post has a group of its own, and claims no other edge.
            (None, 1, [2]),
            ("rust", 1, [3]),
        ]

    async def test_groups_by_each_key_of_a_reverse_foreign_key(self, orm, models, run):
        """Alice's two published posts put her in the published group once."""
        schema = _users_schema(orm, models)
        result = await run(
            schema,
            """{ users(order: [{field: {id: ASC}}],
                groupBy: [{object: {posts: {field: {isPublished: true}}}}]) {
                edges { node { name } }
                groups { key { posts { isPublished } } aggregates { count sum { id } }
                  edgeIndices items { edges { node { name } } } } } }""",
        )
        assert result.errors is None, result.errors
        assert [
            (
                g["key"]["posts"]["isPublished"],
                g["aggregates"]["count"],
                g["aggregates"]["sum"]["id"],
                g["edgeIndices"],
                [e["node"]["name"] for e in g["items"]["edges"]],
            )
            for g in result.data["users"]["groups"]
        ] == [
            ("True", 2, 4, [0, 2], ["Alice", "Charlie"]),
            ("False", 1, 2, [1], ["Bob"]),
        ]

    async def test_a_null_group_holds_only_the_rows_without_a_value(
        self, orm, models, hide, run
    ):
        schema = _posts_schema(orm, models, hide)
        query = _posts(
            "[{object: {author: {field: {name: true}}}}]",
            groups="key { author { name } }",
        )
        groups = _summary(await run(schema, query), lambda key: key["author"]["name"])
        null_groups = [indices for name, _, indices in groups if name is None]
        assert null_groups == [[2]]

    async def test_a_page_of_items_within_a_relation_group(
        self, orm, models, hide, run
    ):
        schema = _posts_schema(orm, models, hide)
        query = f"""{{ posts(groupBy: [{{object: {{author: {{field: {{id: true}}}}}}}}],
            order: [{{field: {{id: ASC}}}}]) {{
            groups {{ key {{ author {{ id }} }}
              first: items(first: 1) {{ edges {{ node {{ title }} }} pageInfo {{ hasNextPage }} }}
              next: items(first: 1, after: "{cursor(0)}") {{ edges {{ node {{ title }} }} }} }} }} }}"""
        result = await run(schema, query)
        assert result.errors is None, result.errors
        alice = result.data["posts"]["groups"][0]
        assert alice["key"] == {"author": {"id": "1"}}
        assert [e["node"]["title"] for e in alice["first"]["edges"]] == ["Hello World"]
        assert alice["first"]["pageInfo"]["hasNextPage"] is True
        assert [e["node"]["title"] for e in alice["next"]["edges"]] == ["GraphQL Guide"]

    async def test_a_group_field_groups_by_what_it_returns(
        self, orm, models, hide, run, author_column
    ):
        Post = models.Post

        @orm.group_type(Post)
        class PostGroupBy:
            is_published: auto

            @group_field
            def by_author(self, value: bool, query):
                return author_column(Post)

        schema = _posts_schema(orm, models, hide, group=PostGroupBy)
        result = await run(
            schema, _posts("[{byAuthor: true}]", groups="key { byAuthor }")
        )
        assert _summary(result, lambda key: key["byAuthor"]) == [
            ("1", 2, [0, 1]),
            ("2", 1, [2]),
            ("3", 1, [3]),
        ]

    async def test_a_group_field_may_return_its_label_too(
        self, orm, models, hide, run, author_column
    ):
        Post = models.Post

        @orm.group_type(Post)
        class PostGroupBy:
            @group_field
            def by_author(self, value: bool, query):
                return author_column(Post), "author"

        schema = _posts_schema(orm, models, hide, group=PostGroupBy)
        result = await run(
            schema, _posts("[{byAuthor: true}]", groups="key { byAuthor }")
        )
        assert [key for key, _, _ in _summary(result, lambda k: k["byAuthor"])] == [
            "1",
            "2",
            "3",
        ]

    async def test_a_group_type_names_the_relations_it_groups_through(
        self, orm, models, hide, run
    ):
        Post = models.Post
        orm.group(models.User)

        @orm.group_type(Post)
        class PostGroupBy:
            is_published: auto
            author: auto

        schema = _posts_schema(orm, models, hide, group=PostGroupBy)
        assert fields(schema, "PostGroupByObject") == {"author"}
        result = await run(schema, _posts("[{object: {author: {field: {id: true}}}}]"))
        assert [key for key, _, _ in _summary(result)] == [
            {"author": {"id": "1"}},
            {"author": {"id": None}},
            {"author": {"id": "3"}},
        ]

    async def test_groups_by_two_columns_of_one_many_to_many(
        self, orm, models, hide, run
    ):
        schema = _posts_schema(orm, models, hide)
        query = _posts(
            "[{object: {tags: {field: {id: true}}}}, "
            "{object: {tags: {field: {name: true}}}}]",
            groups="key { tags { id name } }",
        )
        assert _summary(await run(schema, query), lambda k: k["tags"]) == [
            ({"id": "1", "name": "python"}, 2, [0, 1]),
            ({"id": None, "name": None}, 1, [2]),
            ({"id": "3", "name": "rust"}, 1, [3]),
        ]

    async def test_objects_offers_the_to_many_relations_the_type_shows(
        self, orm, models, hide
    ):
        schema = _posts_schema(orm, models, hide)
        assert fields(schema, "PostGroupByObjects") == {"tags"}
        assert fields(schema, "PostGroupKeyObjects") == {"tags"}
        assert "objects" in fields(schema, "PostGroupKey")
        tags = schema._schema.type_map["PostGroupKeyObjects"].fields["tags"]
        assert str(tags.type) == "[TagGroupKey!]"

    async def test_groups_by_the_rows_of_a_many_to_many_together(
        self, orm, models, hide, run
    ):
        """Post 2, tagged graphql and python, is in one group of its own."""
        schema = _posts_schema(orm, models, hide, scoped=False)
        query = _posts(
            "[{objects: {tags: {field: {name: true}}}}]",
            groups="key { objects { tags { name } } } items { edges { node { title } } }",
        )
        result = await run(schema, query)
        assert _summary(result, _tag_names) == [
            (["python"], 1, [0]),
            (["graphql", "python"], 1, [1]),
            ([], 1, [2]),
            (["rust"], 1, [3]),
        ]
        items = [
            [edge["node"]["title"] for edge in group["items"]["edges"]]
            for group in result.data["posts"]["groups"]
        ]
        assert items == [
            ["Hello World"],
            ["GraphQL Guide"],
            ["Draft Post"],
            ["Rust Adventures"],
        ]

    async def test_rows_the_type_hides_are_left_out_of_the_set(
        self, orm, models, hide, run
    ):
        """Without its hidden graphql tag, post 2 shares post 1's group."""
        schema = _posts_schema(orm, models, hide)
        query = _posts(
            "[{objects: {tags: {field: {name: true}}}}]",
            groups="key { objects { tags { name } } }",
        )
        assert _summary(await run(schema, query), _tag_names) == [
            (["python"], 2, [0, 1]),
            ([], 1, [2]),
            (["rust"], 1, [3]),
        ]

    async def test_a_set_of_rows_beside_a_column_and_with_two_columns(
        self, orm, models, hide, run
    ):
        schema = _posts_schema(orm, models, hide, scoped=False)
        query = _posts(
            "[{field: {isPublished: true}}, {objects: {tags: {field: {id: true}}}}, "
            "{objects: {tags: {field: {name: true}}}}]",
            groups="key { isPublished objects { tags { id name } } }",
        )
        assert _summary(
            await run(schema, query),
            lambda key: (key["isPublished"], key["objects"]["tags"]),
        ) == [
            (("True", [{"id": "1", "name": "python"}]), 1, [0]),
            (
                (
                    "True",
                    [{"id": "1", "name": "python"}, {"id": "2", "name": "graphql"}],
                ),
                1,
                [1],
            ),
            (("False", []), 1, [2]),
            (("True", [{"id": "3", "name": "rust"}]), 1, [3]),
        ]

    async def test_groups_by_the_rows_of_a_reverse_foreign_key_together(
        self, orm, models, run
    ):
        """Alice's two published posts read as one published post."""
        schema = _users_schema(orm, models)
        result = await run(
            schema,
            """{ users(order: [{field: {id: ASC}}],
                groupBy: [{objects: {posts: {field: {isPublished: true}}}}]) {
                edges { node { name } }
                groups { key { objects { posts { isPublished } } }
                  aggregates { count } edgeIndices } } }""",
        )
        assert result.errors is None, result.errors
        groups = [
            (
                [p["isPublished"] for p in g["key"]["objects"]["posts"]],
                g["aggregates"]["count"],
                g["edgeIndices"],
            )
            for g in result.data["users"]["groups"]
        ]
        assert groups == [(["True"], 2, [0, 2]), (["False"], 1, [1])], groups

    async def test_a_group_type_offers_objects_for_its_to_many_relations(
        self, orm, models, hide, run
    ):
        Post = models.Post
        orm.group(models.User)
        orm.group(models.Tag)

        @orm.group_type(Post)
        class PostGroupBy:
            is_published: auto
            author: auto
            tags: auto

        schema = _posts_schema(orm, models, hide, group=PostGroupBy)
        assert fields(schema, "PostGroupByObjects") == {"tags"}
        query = _posts(
            "[{objects: {tags: {field: {name: true}}}}]",
            groups="key { objects { tags { name } } }",
        )
        assert [
            key for key, _, _ in _summary(await run(schema, query), _tag_names)
        ] == [
            ["python"],
            [],
            ["rust"],
        ]

    async def test_objects_naming_no_column_groups_by_nothing(
        self, orm, models, hide, run
    ):
        schema = _posts_schema(orm, models, hide)
        query = _posts(
            "[{objects: {tags: {field: {name: false}}}}]", groups="edgeIndices"
        )
        result = await run(schema, query)
        assert result.errors is None, result.errors
        assert result.data["posts"]["groups"] == []

    async def test_an_order_through_a_to_many_relation_does_not_rank_groups(
        self, orm, models, hide, run
    ):
        schema = _posts_schema(orm, models, hide)
        query = _posts(
            "[{object: {author: {field: {id: true}}}}]",
            order="[{object: {tags: {field: {name: DESC}}}}, {field: {id: ASC}}]",
        )
        result = await run(schema, query)
        assert result.errors is None, result.errors
        keys = [group["key"] for group in result.data["posts"]["groups"]]
        assert keys == [
            {"author": {"id": "1"}},
            {"author": {"id": None}},
            {"author": {"id": "3"}},
        ]

    async def test_groups_and_their_items_follow_where_nulls_sort(
        self, orm, models, hide, run
    ):
        schema = _posts_schema(orm, models, hide)
        query = """{ posts(groupBy: [{field: {isPublished: true}}],
            order: [{field: {title: DESC_NULLS_LAST}}, {field: {id: ASC_NULLS_FIRST}}]) {
            groups { key { isPublished }
              items(first: 1) { edges { node { title } } } } } }"""
        result = await run(schema, query)
        assert result.errors is None, result.errors
        assert [
            (g["key"]["isPublished"], g["items"]["edges"][0]["node"]["title"])
            for g in result.data["posts"]["groups"]
        ] == [("True", "Rust Adventures"), ("False", "Draft Post")]

    async def test_no_rows_and_no_groups(self, orm, models, hide, run):
        schema = _posts_schema(orm, models, hide)
        query = """{ posts(filter: {field: {title: {exact: "Nothing"}}},
            groupBy: [{object: {author: {field: {id: true}}}}]) {
            groups { edgeIndices } }
          none: posts(groupBy: []) { groups { edgeIndices } } }"""
        result = await run(schema, query)
        assert result.errors is None, result.errors
        assert result.data == {"posts": {"groups": []}, "none": {"groups": []}}

    async def test_grouping_needs_no_order_on_the_grouped_column(
        self, orm, models, hide, run, by_title
    ):
        """An order on another column must not split the groups row by row."""
        PostNode = _post_node(orm, models, hide)
        Post = models.Post

        @strawberry.type
        class Query:
            @orm.connection(ORMListConnection[PostNode])
            def posts(self, info: strawberry.Info) -> list[PostNode]:
                return by_title(Post)

        schema = orm.schema(query=Query)
        result = await run(
            schema,
            """{ posts(groupBy: [{field: {isPublished: true}}]) {
                edges { node { title } }
                groups { key { isPublished } aggregates { count } edgeIndices } } }""",
        )
        assert _titles(result) == [
            "Draft Post",
            "GraphQL Guide",
            "Hello World",
            "Rust Adventures",
        ]
        assert sorted(_summary(result, lambda key: key["isPublished"])) == [
            ("False", 1, [0]),
            ("True", 3, [1, 2, 3]),
        ]


#: Post 3 on a Sunday and post 4 on a Monday: a week starts on a Monday.
DATES = {
    1: datetime.datetime(2026, 1, 15, 10, 0),
    2: datetime.datetime(2026, 1, 20, 10, 0),
    3: datetime.datetime(2026, 5, 3, 23, 30),
    4: datetime.datetime(2027, 3, 1, 0, 30),
}

INTERVALS = {
    "DAY": [
        ("2026-01-15T00:00:00", [0]),
        ("2026-01-20T00:00:00", [1]),
        ("2026-05-03T00:00:00", [2]),
        ("2027-03-01T00:00:00", [3]),
    ],
    "WEEK": [
        ("2026-01-12T00:00:00", [0]),
        ("2026-01-19T00:00:00", [1]),
        ("2026-04-27T00:00:00", [2]),
        ("2027-03-01T00:00:00", [3]),
    ],
    "MONTH": [
        ("2026-01-01T00:00:00", [0, 1]),
        ("2026-05-01T00:00:00", [2]),
        ("2027-03-01T00:00:00", [3]),
    ],
    "QUARTER": [
        ("2026-01-01T00:00:00", [0, 1]),
        ("2026-04-01T00:00:00", [2]),
        ("2027-01-01T00:00:00", [3]),
    ],
    "YEAR": [
        ("2026-01-01T00:00:00", [0, 1, 2]),
        ("2027-01-01T00:00:00", [3]),
    ],
}


@pytest.mark.asyncio
class AbstractTestGroupingByDateInterval:
    @pytest.mark.parametrize("interval", list(INTERVALS))
    async def test_groups_by_the_start_of_each_period(
        self, orm, models, hide, run, date_posts, interval
    ):
        await date_posts(DATES)
        schema = _posts_schema(orm, models, hide)
        query = _posts(
            f"[{{field: {{createdAt: {{interval: {interval}}}}}}}]",
            order="[{field: {createdAt: ASC}}]",
            groups="key { createdAt }",
        )
        groups = _summary(await run(schema, query), lambda key: key["createdAt"])
        assert [(key, indices) for key, _, indices in groups] == INTERVALS[interval]
        assert [count for _, count, _ in groups] == [
            len(indices) for _, indices in INTERVALS[interval]
        ]

    @pytest.mark.parametrize(
        ("interval", "periods"),
        [
            (
                "MONTH",
                [
                    (["2026-01-01T00:00:00"], [0]),
                    (["2026-05-01T00:00:00"], [1]),
                    (["2027-03-01T00:00:00"], [2]),
                ],
            ),
            (
                "YEAR",
                [
                    (["2026-01-01T00:00:00"], [0, 1]),
                    (["2027-01-01T00:00:00"], [2]),
                ],
            ),
        ],
    )
    async def test_objects_cuts_each_related_date_to_its_period(
        self, orm, models, run, date_posts, interval, periods
    ):
        """Alice's two January posts are one period; Bob shares 2026 with her."""
        await date_posts(DATES)
        User, Post = models.User, models.Post

        @orm.type(Post, group=orm.group(Post))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            created_at: auto

        @orm.type(User, order=orm.order(User), group=orm.group(User))
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            posts: ORMListConnection[PostNode] = orm.connection.eager()

        @strawberry.type
        class Query:
            users: ORMListConnection[UserNode] = orm.connection.eager()

        result = await run(
            orm.schema(query=Query),
            f"""{{ users(order: [{{field: {{id: ASC}}}}], groupBy: [{{objects: {{posts:
                {{field: {{createdAt: {{interval: {interval}}}}}}}}}}}]) {{
                edges {{ node {{ id }} }}
                groups {{ key {{ objects {{ posts {{ createdAt }} }} }} edgeIndices }} }} }}""",
        )
        assert result.errors is None, result.errors
        groups = [
            (
                [post["createdAt"] for post in group["key"]["objects"]["posts"]],
                group["edgeIndices"],
            )
            for group in result.data["users"]["groups"]
        ]
        assert groups == periods, groups

    async def test_groups_follow_a_descending_order(
        self, orm, models, hide, run, date_posts
    ):
        await date_posts(DATES)
        schema = _posts_schema(orm, models, hide)
        query = _posts(
            "[{field: {createdAt: {interval: MONTH}}}]",
            order="[{field: {createdAt: DESC}}]",
            groups="key { createdAt }",
        )
        assert _summary(await run(schema, query), lambda key: key["createdAt"]) == [
            ("2027-03-01T00:00:00", 1, [0]),
            ("2026-05-01T00:00:00", 1, [1]),
            ("2026-01-01T00:00:00", 2, [2, 3]),
        ]

    async def test_a_page_of_items_within_a_period(
        self, orm, models, hide, run, date_posts
    ):
        await date_posts(DATES)
        schema = _posts_schema(orm, models, hide)
        query = f"""{{ posts(groupBy: [{{field: {{createdAt: {{interval: YEAR}}}}}}],
            order: [{{field: {{createdAt: ASC}}}}]) {{
            groups {{ key {{ createdAt }}
              items(first: 1, after: "{cursor(0)}") {{ edges {{ node {{ title }} }} }} }} }} }}"""
        result = await run(schema, query)
        assert result.errors is None, result.errors
        first_year = result.data["posts"]["groups"][0]
        assert first_year["key"] == {"createdAt": "2026-01-01T00:00:00"}
        assert [e["node"]["title"] for e in first_year["items"]["edges"]] == [
            "GraphQL Guide"
        ]


def _users_schema(orm, models):
    User, Post = models.User, models.Post

    @orm.type(
        Post, filters=orm.filter(Post), order=orm.order(Post), group=orm.group(Post)
    )
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        is_published: auto

    @orm.type(
        User, filters=orm.filter(User), order=orm.order(User), group=orm.group(User)
    )
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        posts: ORMListConnection[PostNode] = orm.connection.eager()

    @strawberry.type
    class Query:
        users: ORMListConnection[UserNode] = orm.connection.eager()

    return orm.schema(query=Query)


@pytest.mark.asyncio
class AbstractTestFilteringThroughAToManyRelation:
    """A filter through a to-many relation keeps each row once.

    Alice has two published posts; a join to them must not list her twice,
    count her twice, or put two of her in her group.
    """

    @pytest.mark.parametrize(
        "condition", ["isPublished: {exact: true}", 'title: {iContains: "o"}']
    )
    async def test_each_row_once(self, orm, models, run, condition):
        schema = _users_schema(orm, models)
        result = await run(
            schema,
            f"""{{ users(filter: {{object: {{posts: {{field: {{{condition}}}}}}}}},
                order: [{{field: {{id: ASC}}}}], groupBy: [{{field: {{name: true}}}}]) {{
                totalCount aggregates {{ count }} edges {{ node {{ name }} }}
                groups {{ key {{ name }} aggregates {{ count }}
                  items {{ edges {{ node {{ name }} }} }} }} }} }}""",
        )
        assert result.errors is None, result.errors
        users = result.data["users"]
        names = [edge["node"]["name"] for edge in users["edges"]]
        assert len(names) == len(set(names)), names
        assert users["totalCount"] == len(names)
        assert users["aggregates"]["count"] == len(names)
        for group in users["groups"]:
            assert group["aggregates"]["count"] == 1, group
            assert len(group["items"]["edges"]) == 1, group

    async def test_aggregates_of_a_nested_connection(self, orm, models, run):
        schema = _users_schema(orm, models)
        result = await run(
            schema,
            """{ users(order: [{field: {id: ASC}}]) { edges { node { name
                posts(groupBy: [{field: {isPublished: true}}]) {
                  totalCount aggregates { count max { id } }
                  groups { key { isPublished } aggregates { count } } } } } } }""",
        )
        assert result.errors is None, result.errors
        found = {
            edge["node"]["name"]: (
                edge["node"]["posts"]["totalCount"],
                edge["node"]["posts"]["aggregates"],
                [
                    (g["key"]["isPublished"], g["aggregates"]["count"])
                    for g in edge["node"]["posts"]["groups"]
                ],
            )
            for edge in result.data["users"]["edges"]
        }
        assert found == {
            "Alice": (2, {"count": 2, "max": {"id": 2}}, [("True", 2)]),
            "Bob": (1, {"count": 1, "max": {"id": 3}}, [("False", 1)]),
            "Charlie": (1, {"count": 1, "max": {"id": 4}}, [("True", 1)]),
        }


def _counted_schema(orm, models):
    Post = models.Post

    @orm.type(
        Post, filters=orm.filter(Post), order=orm.order(Post), group=orm.group(Post)
    )
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        is_published: auto
        created_at: auto
        word_count: auto

    @strawberry.type
    class Query:
        posts: ORMListConnection[PostNode] = orm.connection.eager()

    return orm.schema(query=Query)


#: Drafts first, each group's posts by id: post 3, then posts 1, 2 and 4.
_BY_PUBLISHED = (
    "groupBy: [{field: {isPublished: true}}], "
    "order: [{field: {isPublished: ASC}}, {field: {id: ASC}}]"
)


def _instant(value):
    return datetime.datetime.fromisoformat(value).replace(tzinfo=None)


@pytest.mark.asyncio
class AbstractTestAggregateSelections:
    """Aggregates read the columns the client names, as a client names them.

    Word counts: post 1 has 100, post 2 250, post 3 (the draft) 40, post 4 310.
    """

    async def test_two_word_fields(self, orm, models, run):
        schema = _counted_schema(orm, models)
        selection = (
            "sum { wordCount } avg { wordCount } min { wordCount } max { wordCount }"
        )
        result = await run(
            schema,
            f"""{{ posts({_BY_PUBLISHED}) {{ aggregates {{ {selection} }}
                groups {{ key {{ isPublished }} aggregates {{ {selection} }} }} }} }}""",
        )
        assert result.errors is None, result.errors
        posts = result.data["posts"]

        def counts(aggregates):
            return {name: value["wordCount"] for name, value in aggregates.items()}

        assert counts(posts["aggregates"]) == {
            "sum": 700,
            "avg": 175,
            "min": 40,
            "max": 310,
        }
        assert [
            (group["key"]["isPublished"], counts(group["aggregates"]))
            for group in posts["groups"]
        ] == [
            ("False", {"sum": 40, "avg": 40, "min": 40, "max": 40}),
            ("True", {"sum": 660, "avg": 220, "min": 100, "max": 310}),
        ]

    async def test_typename_and_fragments(self, orm, models, run):
        """What Apollo adds to every selection is not a column."""
        schema = _counted_schema(orm, models)
        result = await run(
            schema,
            f"""{{ posts({_BY_PUBLISHED}) {{
                aggregates {{ __typename count sum {{ __typename wordCount }} ...Mean }}
                groups {{ aggregates {{ __typename
                  ... on PostAggregates {{ max {{ __typename wordCount }} }} }} }} }} }}
            fragment Mean on PostAggregates {{ avg {{ wordCount }} }}""",
        )
        assert result.errors is None, result.errors
        posts = result.data["posts"]
        assert posts["aggregates"] == {
            "__typename": "PostAggregates",
            "count": 4,
            "sum": {"__typename": "PostSumAggregates", "wordCount": 700},
            "avg": {"wordCount": 175},
        }
        assert [group["aggregates"] for group in posts["groups"]] == [
            {
                "__typename": "PostAggregates",
                "max": {"__typename": "PostMaxAggregates", "wordCount": 40},
            },
            {
                "__typename": "PostAggregates",
                "max": {"__typename": "PostMaxAggregates", "wordCount": 310},
            },
        ]

    async def test_an_aggregate_selected_twice_reads_both_columns(
        self, orm, models, run
    ):
        """Under an alias, or once in a fragment and once outside it."""
        schema = _counted_schema(orm, models)
        result = await run(
            schema,
            f"""{{ posts({_BY_PUBLISHED}) {{ ...Totals
                groups {{ ...GroupTotals }} }} }}
            fragment Totals on PostConnection {{
                aggregates {{ sum {{ wordCount }} ...Ids
                  words: max {{ wordCount }} ids: max {{ id }} }} }}
            fragment GroupTotals on PostGroup {{
                aggregates {{ sum {{ __typename }} ...Ids }} }}
            fragment Ids on PostAggregates {{ sum {{ id }} }}""",
        )
        assert result.errors is None, result.errors
        posts = result.data["posts"]
        assert posts["aggregates"] == {
            "sum": {"wordCount": 700, "id": 10},
            "words": {"wordCount": 310},
            "ids": {"id": 4},
        }
        assert [group["aggregates"] for group in posts["groups"]] == [
            {"sum": {"__typename": "PostSumAggregates", "id": 3}},
            {"sum": {"__typename": "PostSumAggregates", "id": 7}},
        ]

    async def test_a_typename_alone_is_an_empty_aggregate(self, orm, models, run):
        schema = _counted_schema(orm, models)
        result = await run(schema, "{ posts { aggregates { sum { __typename } } } }")
        assert result.errors is None, result.errors
        aggregates = result.data["posts"]["aggregates"]
        assert aggregates == {"sum": {"__typename": "PostSumAggregates"}}

    async def test_the_least_and_greatest_date_are_dates(
        self, orm, models, run, date_posts
    ):
        await date_posts(DATES)
        schema = _counted_schema(orm, models)
        for name in ("PostMinAggregates", "PostMaxAggregates"):
            fields = schema._schema.type_map[name].fields
            assert str(fields["createdAt"].type) == "DateTime"
            assert str(fields["wordCount"].type) == "Float"
        result = await run(
            schema,
            f"""{{ posts({_BY_PUBLISHED}, first: 2) {{
                aggregates {{ min {{ createdAt }} max {{ createdAt }} }}
                pageInfo {{ aggregates {{ min {{ createdAt }} max {{ createdAt }} }} }}
                groups {{ aggregates {{ min {{ createdAt }} max {{ createdAt }} }} }}
            }} }}""",
        )
        assert result.errors is None, result.errors
        posts = result.data["posts"]

        def span(aggregates):
            return (
                _instant(aggregates["min"]["createdAt"]),
                _instant(aggregates["max"]["createdAt"]),
            )

        assert span(posts["aggregates"]) == (DATES[1], DATES[4])
        # The page holds the draft and post 1.
        assert span(posts["pageInfo"]["aggregates"]) == (DATES[1], DATES[3])
        assert [span(group["aggregates"]) for group in posts["groups"]] == [
            (DATES[3], DATES[3]),
            (DATES[1], DATES[4]),
        ]

    async def test_the_median_of_a_page(self, orm, models, run):
        schema = _counted_schema(orm, models)
        assert "wordCount" in fields(schema, "PostMedianAggregates")
        result = await run(
            schema,
            """{ posts(order: [{field: {id: ASC}}], first: 3) {
                pageInfo { aggregates { median { wordCount } } } } }""",
        )
        assert result.errors is None, result.errors
        page = result.data["posts"]["pageInfo"]["aggregates"]
        assert page == {"median": {"wordCount": 100}}

    @pytest.mark.parametrize(
        "selection",
        [
            "aggregates { median { wordCount } }",
            "groups { aggregates { median { wordCount } } }",
        ],
    )
    async def test_the_median_needs_postgresql(self, orm, models, run, selection):
        schema = _counted_schema(orm, models)
        result = await run(schema, f"{{ posts({_BY_PUBLISHED}) {{ {selection} }} }}")
        assert result.errors is not None
        assert "median is supported on PostgreSQL" in result.errors[0].message


def _tagged_schema(orm, models, hide):
    """Posts and their tags, ``rust`` hidden: post 4's only tag."""
    User, Post, Tag = models.User, models.Post, models.Tag

    @orm.type(Tag, filters=orm.filter(Tag))
    class TagNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

        @classmethod
        def scope_rows(cls, qs, info):
            return hide(Tag, "rust", qs)

    @orm.type(User, filters=orm.filter(User))
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

    @orm.type(Post, filters=orm.filter(Post), order=orm.order(Post))
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        tags: list[TagNode]
        author: UserNode | None

    @orm.type(User, filters=orm.filter(User), order=orm.order(User))
    class AuthorNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        posts: ORMListConnection[PostNode] = orm.connection.eager()

    @strawberry.type
    class Query:
        posts: ORMListConnection[PostNode] = orm.connection.eager()
        authors: ORMListConnection[AuthorNode] = orm.connection.eager()

    return orm.schema(query=Query)


@pytest.mark.asyncio
class AbstractTestARelationToManyRowsCanBeEmpty:
    """``object: {tags: {isNull: true}}`` asks for rows with none the reader sees.

    Post 3 has no tag, post 4 only ``rust``, which the tag type hides.
    """

    @pytest.mark.parametrize(
        ("is_null", "titles"),
        [
            ("true", ["Draft Post", "Rust Adventures"]),
            ("false", ["Hello World", "GraphQL Guide"]),
        ],
    )
    async def test_through_a_many_to_many_relation(
        self, orm, models, hide, run, is_null, titles
    ):
        schema = _tagged_schema(orm, models, hide)
        result = await run(
            schema,
            f"""{{ posts(filter: {{object: {{tags: {{isNull: {is_null}}}}}}},
                order: [{{field: {{id: ASC}}}}]) {{ edges {{ node {{ title }} }} }} }}""",
        )
        assert result.errors is None, result.errors
        assert _titles(result) == titles

    @pytest.mark.parametrize(
        ("is_null", "names"),
        [("true", []), ("false", ["Alice", "Bob", "Charlie"])],
    )
    async def test_through_a_reverse_foreign_key(
        self, orm, models, hide, run, is_null, names
    ):
        schema = _tagged_schema(orm, models, hide)
        result = await run(
            schema,
            f"""{{ authors(filter: {{object: {{posts: {{isNull: {is_null}}}}}}},
                order: [{{field: {{id: ASC}}}}]) {{ edges {{ node {{ name }} }} }} }}""",
        )
        assert result.errors is None, result.errors
        found = [edge["node"]["name"] for edge in result.data["authors"]["edges"]]
        assert found == names


def _ordered_schema(orm, models, by_title):
    """Posts over a statement already ordered by title."""
    User, Post = models.User, models.Post

    @orm.type(User)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

    orm.order(User)

    @orm.type(Post, order=orm.order(Post))
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        author: UserNode | None

    def resolve_posts(info: strawberry.types.Info) -> list[PostNode]:
        return by_title(Post)

    @strawberry.type
    class Query:
        posts = orm.connection(ORMListConnection[PostNode], resolver=resolve_posts)

        @orm.field.lazy(order=orm.order(Post))
        def post_list(self, info: strawberry.types.Info) -> list[PostNode]:
            return by_title(Post)

    return orm.schema(query=Query)


#: Posts 4 to 1, by id descending.
_NEWEST_FIRST = ["Rust Adventures", "Draft Post", "GraphQL Guide", "Hello World"]


@pytest.mark.asyncio
class AbstractTestTheClientsOrderReplacesTheStatements:
    """A resolver's statement orders by title; the client's ``order`` wins."""

    async def test_a_connection(self, orm, models, by_title, run):
        schema = _ordered_schema(orm, models, by_title)
        result = await run(
            schema,
            "{ posts(order: [{field: {id: DESC}}]) { edges { node { title } } } }",
        )
        assert result.errors is None, result.errors
        assert _titles(result) == _NEWEST_FIRST

    async def test_without_an_order_the_statements_holds(
        self, orm, models, by_title, run
    ):
        schema = _ordered_schema(orm, models, by_title)
        result = await run(schema, "{ posts { edges { node { title } } } }")
        assert result.errors is None, result.errors
        assert _titles(result) == sorted(_NEWEST_FIRST)

    async def test_a_list(self, orm, models, by_title, run):
        schema = _ordered_schema(orm, models, by_title)
        result = await run(
            schema, "{ postList(order: [{field: {id: DESC}}]) { title } }"
        )
        assert result.errors is None, result.errors
        assert [post["title"] for post in result.data["postList"]] == _NEWEST_FIRST

    async def test_by_a_related_column(self, orm, models, by_title, run):
        schema = _ordered_schema(orm, models, by_title)
        result = await run(
            schema,
            """{ posts(order: [{object: {author: {field: {name: DESC}}}},
                {field: {id: ASC}}]) { edges { node { title } } } }""",
        )
        assert result.errors is None, result.errors
        assert _titles(result) == [
            "Rust Adventures",
            "Draft Post",
            "Hello World",
            "GraphQL Guide",
        ]

    async def test_pages_are_disjoint_and_complete(self, orm, models, by_title, run):
        schema = _ordered_schema(orm, models, by_title)
        pages = []
        after = ""
        for _ in range(2):
            result = await run(
                schema,
                f"""{{ posts(order: [{{field: {{id: DESC}}}}], first: 2{after}) {{
                    edges {{ node {{ title }} }} pageInfo {{ endCursor }} }} }}""",
            )
            assert result.errors is None, result.errors
            pages.append(_titles(result))
            after = f', after: "{result.data["posts"]["pageInfo"]["endCursor"]}"'
        assert pages == [_NEWEST_FIRST[:2], _NEWEST_FIRST[2:]]


def together_path(backend, Post):
    """The path ``objects: {tags: {field: {name: true}}}`` groups posts by."""
    return GroupPath(
        alias="_g0",
        name="tags",
        through=backend.relation_hop(Post, "tags"),
        columns=("name",),
        intervals=(None,),
        readers=(str,),
    )


@pytest.mark.asyncio
class AbstractTestTheRowsTogetherOnEachDatabase:
    """``objects:`` writes each row's related rows as JSON in SQL of its own.

    Backends supply ``together_sql(database)``: awaitable SQL grouping posts by
    their tags' names together, written for ``"postgres"`` or ``"mysql"``
    without running it, and ``together_read(column, interval)``: what reads
    one of a post's columns back out of that JSON.
    """

    async def test_postgresql_sorts_inside_the_aggregate(self, together_sql):
        sql = (await together_sql("postgres")).lower()
        assert "json_agg(json_build_array(" in sql, sql
        assert "order by" in sql.split("json_agg(", 1)[1], sql

    async def test_another_database_is_refused(self, together_sql):
        with pytest.raises(ValueError, match="supported on PostgreSQL and SQLite"):
            await together_sql("mysql")

    async def test_a_datetime_cut_to_a_period_keeps_its_instant(self, together_read):
        read = together_read("created_at", DateGroupByInterval.DAY)
        value = read("2026-01-15T00:00:00-08:00")
        assert value == datetime.datetime(2026, 1, 15, 8, tzinfo=datetime.UTC), value
