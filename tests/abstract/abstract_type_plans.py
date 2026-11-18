"""Under an interface or a union, rows load what the type they are read as says.

Two types over one model can declare different ``using=`` paths, and
different ``scope=`` on a relation of one name. Under an interface, a union
or Relay's ``node``/``nodes``, the type a row is read as is the one it is
``strawberry.cast`` to, and the optimizer planned every row as the first type
registered over the model instead. The other type's ``using=`` paths were
never loaded, so its resolvers read those relations straight from the
database, past every scope, and its ``scope=`` gave way to the first type's.

Each place an abstract type can return rows - an interface or a union field
at the root, Relay's ``node`` and ``nodes``, an interface or a union field
below another type - is crossed with which type each user is cast to. Every
row must show exactly what its own type allows, whichever type comes first.

A backend provides ``orm`` (a fresh ORM) and ``models`` (``User``, ``Post``,
``Comment``), and on the test class:

- ``run(schema, query, variables)``, executing against the seeded rows;
- ``load_users()``, every user, read before any execution, in id order;
- ``read(obj, name)``, reading a relation the way a resolver on that ORM would;
- ``published_only(qs)``, keeping the published posts;
- ``hide(qs, model, field, value)``, excluding rows whose *field* is *value*.

Seed: Alice wrote "Hello World" and "GraphQL Guide", Bob the unpublished
"Draft Post", Charlie "Rust Adventures". Bob commented "Nice post!" and Alice
"Thanks!" on "Hello World", Charlie "Great guide" on "GraphQL Guide".
"""

from typing import Annotated

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import auto

STAFF, PUBLIC = "StaffUser", "PublicUser"

#: What each type shows of each user. ``StaffUser`` hides "GraphQL Guide"
#: from ``posts`` and reads the user's comments; ``PublicUser`` keeps the
#: published posts and reads the comments on them. ``CommentType`` hides
#: "Thanks!" from both.
SHOWN = {
    STAFF: {
        1: {"posts": ["Hello World"], "staffTitles": ["Hello World"], "remarks": []},
        2: {
            "posts": ["Draft Post"],
            "staffTitles": ["Draft Post"],
            "remarks": ["Nice post!"],
        },
        3: {
            "posts": ["Rust Adventures"],
            "staffTitles": ["Rust Adventures"],
            "remarks": ["Great guide"],
        },
    },
    PUBLIC: {
        1: {
            "posts": ["GraphQL Guide", "Hello World"],
            "postTitles": ["GraphQL Guide", "Hello World"],
            "postRemarks": ["Great guide", "Nice post!"],
        },
        2: {"posts": [], "postTitles": [], "postRemarks": []},
        3: {
            "posts": ["Rust Adventures"],
            "postTitles": ["Rust Adventures"],
            "postRemarks": [],
        },
    },
}
NAMES = {1: "Alice", 2: "Bob", 3: "Charlie"}

#: Which type each user is cast to.
CASTS = {
    "all staff": {1: STAFF, 2: STAFF, 3: STAFF},
    "all public": {1: PUBLIC, 2: PUBLIC, 3: PUBLIC},
    "staff first": {1: STAFF, 2: PUBLIC, 3: STAFF},
    "public first": {1: PUBLIC, 2: STAFF, 3: PUBLIC},
}

OWN_FIELDS = {
    STAFF: "... on StaffUser { name posts { title } staffTitles remarks }",
    PUBLIC: "... on PublicUser { name posts { title } postTitles postRemarks }",
}
FIELDS = f"__typename {OWN_FIELDS[STAFF]} {OWN_FIELDS[PUBLIC]}"


def gid(type_name, pk):
    return str(relay.GlobalID(type_name, str(pk)))


def _shown(row):
    return {**row, "posts": sorted(post["title"] for post in row["posts"])}


def _expected(casts, pk):
    type_name = casts[pk]
    return {"__typename": type_name, "name": NAMES[pk], **SHOWN[type_name][pk]}


def _differences(rows, expected):
    """What each row shows that its type does not allow, and the reverse."""
    return [
        (row["__typename"], row["name"], key, row[key], want[key])
        for row, want in zip(rows, expected, strict=True)
        for key in want
        if row.get(key) != want[key]
    ]


class AbstractTestAbstractTypePlans:
    async def execute(self, schema, query, variables=None):
        result = await self.run(schema, query, variables)
        assert result.errors is None, result.errors
        return result.data

    async def schema(self, orm, models, casts):
        users = {user.id: user for user in await self.load_users()}
        read, published_only, hide = self.read, self.published_only, self.hide

        @orm.type(models.Comment)
        class CommentType:
            id: auto
            body: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return hide(qs, models.Comment, "body", "Thanks!")

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

        @strawberry.interface
        class Person:
            name: str

        def by_id(cls, *, info, node_ids, required=False):
            return [users[int(pk)] for pk in node_ids]

        # Registered first, so the type every row was planned as.
        @orm.type(models.User, name=STAFF)
        class StaffUser(Person, relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: list[PostType] = orm.field.auto(
                scope=lambda qs, info: hide(qs, models.Post, "title", "GraphQL Guide")
            )

            @orm.field.lazy(using=["posts"])
            def staff_titles(self, info: strawberry.Info) -> list[str]:
                return sorted(post.title for post in read(self, "posts"))

            @orm.field.lazy(using=["comments"])
            def remarks(self, info: strawberry.Info) -> list[str]:
                return sorted(comment.body for comment in read(self, "comments"))

            resolve_nodes = classmethod(by_id)

        @orm.type(models.User, name=PUBLIC)
        class PublicUser(Person, relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: list[PostType] = orm.field.auto(
                scope=lambda qs, info: published_only(qs)
            )

            @orm.field.lazy(using=["posts"])
            def post_titles(self, info: strawberry.Info) -> list[str]:
                return sorted(post.title for post in read(self, "posts"))

            @orm.field.lazy(using=["posts.comments"])
            def post_remarks(self, info: strawberry.Info) -> list[str]:
                return sorted(
                    comment.body
                    for post in read(self, "posts")
                    for comment in read(post, "comments")
                )

            resolve_nodes = classmethod(by_id)

        types = {STAFF: StaffUser, PUBLIC: PublicUser}

        def cast(pk):
            return strawberry.cast(types[casts[pk]], users[pk])

        Member = Annotated[StaffUser | PublicUser, strawberry.union("Member")]

        # A second type over Post, so that it can name the types above.
        @orm.type(models.Post, name="Article")
        class Article:
            id: auto
            title: auto

            @strawberry.field
            def writer(self) -> Person:
                return cast(self.author_id)

            @strawberry.field
            def member(self) -> Member:
                return cast(self.author_id)

        @strawberry.type
        class Query:
            node: relay.Node = relay.node()
            nodes: list[relay.Node] = relay.node()
            articles: list[Article] = orm.field.eager()
            staff: list[StaffUser] = orm.field.eager()
            public: list[PublicUser] = orm.field.eager()

            @strawberry.field
            def people(self) -> list[Person]:
                return [cast(pk) for pk in sorted(users)]

            @strawberry.field
            def members(self) -> list[Member]:
                return [cast(pk) for pk in sorted(users)]

        return orm.schema(query=Query, types=[StaffUser, PublicUser])

    async def _rows(self, schema, place, casts):
        """The users *place* returns, in id order."""
        if place in ("people", "members"):
            data = await self.execute(schema, f"{{ {place} {{ {FIELDS} }} }}")
            return data[place]
        if place == "nodes":
            query = f"query ($ids: [ID!]!) {{ nodes(ids: $ids) {{ {FIELDS} }} }}"
            ids = [gid(casts[pk], pk) for pk in sorted(NAMES)]
            return (await self.execute(schema, query, {"ids": ids}))["nodes"]
        if place == "node":
            nodes = " ".join(
                f'u{pk}: node(id: "{gid(casts[pk], pk)}") {{ {FIELDS} }}'
                for pk in sorted(NAMES)
            )
            data = await self.execute(schema, f"{{ {nodes} }}")
            return [data[f"u{pk}"] for pk in sorted(NAMES)]
        data = await self.execute(
            schema, f"{{ articles {{ title {place} {{ {FIELDS} }} }} }}"
        )
        writers = {article["title"]: article[place] for article in data["articles"]}
        return [
            writers["Hello World"],
            writers["Draft Post"],
            writers["Rust Adventures"],
        ]

    @pytest.mark.parametrize("casts", list(CASTS), ids=str)
    @pytest.mark.parametrize(
        "place",
        ["people", "members", "nodes", "node", "writer", "member"],
        ids=[
            "interface list",
            "union list",
            "nodes",
            "node",
            "interface relation",
            "union relation",
        ],
    )
    async def test_each_row_reads_what_its_own_type_allows(
        self, orm, models, place, casts
    ):
        casts = CASTS[casts]
        schema = await self.schema(orm, models, casts)
        rows = [_shown(row) for row in await self._rows(schema, place, casts)]
        expected = [_expected(casts, pk) for pk in sorted(NAMES)]
        assert rows == expected, _differences(rows, expected)

    @pytest.mark.parametrize("field", ["staff", "public"])
    async def test_a_field_of_one_type_reads_what_that_type_allows(
        self, orm, models, field
    ):
        """The control: no abstract type, so the type is the field's."""
        type_name = STAFF if field == "staff" else PUBLIC
        schema = await self.schema(orm, models, CASTS["all staff"])
        fields = f"__typename {OWN_FIELDS[type_name]}"
        data = await self.execute(schema, f"{{ {field} {{ {fields} }} }}")
        rows = [_shown(row) for row in data[field]]
        expected = [_expected(dict.fromkeys(NAMES, type_name), pk) for pk in NAMES]
        assert rows == expected, _differences(rows, expected)

    async def poc_schema(self, orm, models):
        """The audit's schema: ``PublicUser`` reads posts ``PostType`` scopes."""
        users = await self.load_users()
        read, published_only = self.read, self.published_only

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return published_only(qs)

        @strawberry.interface
        class Person:
            name: str

        @orm.type(models.User, name="StaffUser")
        class StaffUser(Person):
            id: auto
            name: auto

        @orm.type(models.User, name="PublicUser")
        class PublicUser(Person):
            id: auto
            name: auto

            @orm.field.lazy(using=["posts"])
            def post_titles(self, info: strawberry.Info) -> list[str]:
                return sorted(post.title for post in read(self, "posts"))

        @strawberry.type
        class Query:
            public_people: list[PublicUser] = orm.field.eager()

            @strawberry.field
            def people(self) -> list[Person]:
                return [strawberry.cast(PublicUser, user) for user in users]

        return orm.schema(query=Query, types=[StaffUser, PublicUser])

    @pytest.mark.parametrize("field", ["people", "publicPeople"])
    async def test_the_reported_interface_reads_only_published_posts(
        self, orm, models, field
    ):
        schema = await self.poc_schema(orm, models)
        data = await self.execute(
            schema, f"{{ {field} {{ name ... on PublicUser {{ postTitles }} }} }}"
        )
        titles = {person["name"]: person["postTitles"] for person in data[field]}
        assert titles == {
            "Alice": ["GraphQL Guide", "Hello World"],
            "Bob": [],
            "Charlie": ["Rust Adventures"],
        }, titles
