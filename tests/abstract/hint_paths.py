"""``using=`` paths: several hops, loaded with the parent, scoped like a selection.

A backend provides ``orm`` (a fresh ORM), ``lenient_orm`` (one built with
``strict_hints=False``), ``models`` (``User``, ``Post``, ``Comment`` and
``Tag``), and on the test class:

- ``run(schema, query)``, executing against the seeded rows and recording the
  number of SQL statements in ``self.query_count``;
- ``read(obj, name)``, reading a relation the way a resolver on that ORM would:
  a row, or ``None`` when hidden, for a to-one relation, and a list otherwise;
- ``hide(qs, model, field, value)``, excluding rows whose *field* is *value*;
- ``materialized_posts``, a ``Query`` resolver returning loaded ``Post`` rows,
  so relations are loaded onto rows the caller already holds.

Seed: Alice wrote "Hello World" and "GraphQL Guide", Bob "Draft Post", Charlie
"Rust Adventures". "Hello World" has comments by Bob ("Nice post!") and Alice
("Thanks!"), "GraphQL Guide" one by Charlie. Tags: python on the first two,
graphql on the second, rust on the last.
"""

import strawberry

from strawberry_orm import auto

COUNTS_BY_TITLE = {
    "Hello World": 2,
    "GraphQL Guide": 2,
    "Draft Post": 1,
    "Rust Adventures": 1,
}
ALICE_HIDDEN = {
    "Hello World": "hidden",
    "GraphQL Guide": "hidden",
    "Draft Post": "Bob",
    "Rust Adventures": "Charlie",
}


def _by_title(data, key):
    return {post["title"]: post[key] for post in data["posts"]}


class AbstractTestHintPaths:
    async def execute(self, schema, query):
        result = await self.run(schema, query)
        assert result.errors is None, result.errors
        return result.data

    def posts_schema(self, orm, post_type):
        @strawberry.type
        class Query:
            posts: list[post_type] = orm.field.eager()

        return orm.schema(query=Query)

    def user_type(self, orm, models, *, hide_alice=False, **annotations):
        hide = self.hide
        namespace = {"__annotations__": {"id": auto, "name": auto, **annotations}}
        if hide_alice:

            def scope_rows(cls, qs, info):
                return hide(qs, models.User, "name", "Alice")

            namespace["scope_rows"] = classmethod(scope_rows)
        return orm.type(models.User)(type("UserType", (), namespace))

    async def test_two_hops_load_with_the_parent_query(self, orm, models):
        read = self.read

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy(using=["author.posts"])
            def author_post_count(self, info: strawberry.Info) -> int:
                return len(read(read(self, "author"), "posts"))

        data = await self.execute(
            self.posts_schema(orm, PostType), "{ posts { title authorPostCount } }"
        )

        assert _by_title(data, "authorPostCount") == COUNTS_BY_TITLE
        # One statement per hop at most, never one per row.
        assert self.query_count <= 3

    async def test_django_spelling_names_the_same_path(self, orm, models):
        read = self.read

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy(using=["author__posts"])
            def author_post_count(self, info: strawberry.Info) -> int:
                return len(read(read(self, "author"), "posts"))

        data = await self.execute(
            self.posts_schema(orm, PostType), "{ posts { title authorPostCount } }"
        )

        assert _by_title(data, "authorPostCount") == COUNTS_BY_TITLE
        assert self.query_count <= 3

    async def test_reverse_then_forward(self, orm, models):
        read = self.read

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy(using=["comments.author"])
            def commenters(self, info: strawberry.Info) -> list[str]:
                return sorted(read(c, "author").name for c in read(self, "comments"))

        data = await self.execute(
            self.posts_schema(orm, PostType), "{ posts { title commenters } }"
        )

        assert _by_title(data, "commenters") == {
            "Hello World": ["Alice", "Bob"],
            "GraphQL Guide": ["Charlie"],
            "Draft Post": [],
            "Rust Adventures": [],
        }
        assert self.query_count <= 3

    async def test_three_hops_through_many_to_many(self, orm, models):
        read = self.read

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy(using=["tags.posts.author"])
            def tag_authors(self, info: strawberry.Info) -> list[str]:
                return sorted(
                    {
                        read(post, "author").name
                        for tag in read(self, "tags")
                        for post in read(tag, "posts")
                    }
                )

        data = await self.execute(
            self.posts_schema(orm, PostType), "{ posts { title tagAuthors } }"
        )

        assert _by_title(data, "tagAuthors") == {
            "Hello World": ["Alice"],
            "GraphQL Guide": ["Alice"],
            "Draft Post": [],
            "Rust Adventures": ["Charlie"],
        }
        assert self.query_count <= 4

    async def test_path_under_a_nested_selection(self, orm, models):
        read = self.read

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy(using=["comments.author"])
            def commenters(self, info: strawberry.Info) -> list[str]:
                return sorted(read(c, "author").name for c in read(self, "comments"))

        UserType = self.user_type(orm, models, posts=list[PostType])

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.eager()

        data = await self.execute(
            orm.schema(query=Query), "{ users { name posts { title commenters } } }"
        )

        alice = next(user for user in data["users"] if user["name"] == "Alice")
        assert {post["title"]: post["commenters"] for post in alice["posts"]} == {
            "Hello World": ["Alice", "Bob"],
            "GraphQL Guide": ["Charlie"],
        }
        assert self.query_count <= 4

    async def test_a_scoped_middle_hop_hides_what_is_behind_it(self, orm, models):
        read = self.read
        self.user_type(orm, models, hide_alice=True)

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy(using=["author.posts"])
            def author_post_count(self, info: strawberry.Info) -> int:
                author = read(self, "author")
                return -1 if author is None else len(read(author, "posts"))

        data = await self.execute(
            self.posts_schema(orm, PostType), "{ posts { title authorPostCount } }"
        )

        assert _by_title(data, "authorPostCount") == {
            "Hello World": -1,
            "GraphQL Guide": -1,
            "Draft Post": 1,
            "Rust Adventures": 1,
        }

    async def test_a_scoped_last_hop_drops_hidden_rows(self, orm, models):
        read = self.read
        hide = self.hide

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

            @orm.field.lazy(using=["comments.author"])
            def commenters(self, info: strawberry.Info) -> list[str]:
                return sorted(read(c, "author").name for c in read(self, "comments"))

        data = await self.execute(
            self.posts_schema(orm, PostType), "{ posts { title commenters } }"
        )

        assert _by_title(data, "commenters")["Hello World"] == ["Bob"]

    async def test_hint_alone_sees_the_scoped_rows(self, orm, models):
        read = self.read
        self.user_type(orm, models, hide_alice=True)

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy(using=["author"])
            def byline(self, info: strawberry.Info) -> str:
                author = read(self, "author")
                return "hidden" if author is None else author.name

        data = await self.execute(
            self.posts_schema(orm, PostType), "{ posts { title byline } }"
        )

        assert _by_title(data, "byline") == ALICE_HIDDEN

    async def test_a_selected_relation_on_the_path_stays_scoped(self, orm, models):
        """The hint and the selection share one load, so neither leaks."""
        read = self.read
        UserType = self.user_type(orm, models, hide_alice=True)

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto
            author: UserType | None

            @orm.field.lazy(using=["author"])
            def byline(self, info: strawberry.Info) -> str:
                author = read(self, "author")
                return "hidden" if author is None else author.name

        schema = self.posts_schema(orm, PostType)
        for query in (
            "{ posts { title byline author { name } } }",
            "{ posts { title author { name } byline } }",
        ):
            data = await self.execute(schema, query)
            assert _by_title(data, "byline") == ALICE_HIDDEN, query
            assert _by_title(data, "author")["Hello World"] is None, query
            assert _by_title(data, "author")["Draft Post"] == {"name": "Bob"}, query

    async def test_selection_under_a_hint_path_shares_the_load(self, orm, models):
        read = self.read

        @orm.type(models.Post, name="AuthoredPost")
        class AuthoredPostType:
            id: auto
            title: auto

        UserType = self.user_type(orm, models, posts=list[AuthoredPostType])

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto
            author: UserType

            @orm.field.lazy(using=["author.posts"])
            def author_post_count(self, info: strawberry.Info) -> int:
                return len(read(read(self, "author"), "posts"))

        data = await self.execute(
            self.posts_schema(orm, PostType),
            "{ posts { title authorPostCount author { posts { title } } } }",
        )

        assert _by_title(data, "authorPostCount") == COUNTS_BY_TITLE
        alice_posts = _by_title(data, "author")["Hello World"]["posts"]
        assert sorted(post["title"] for post in alice_posts) == [
            "GraphQL Guide",
            "Hello World",
        ]
        assert self.query_count <= 3

    async def test_hiding_a_forward_row_keeps_its_key(self, orm, models):
        read = self.read
        self.user_type(orm, models, hide_alice=True)

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy(using=["author"])
            def author_key(self, info: strawberry.Info) -> str:
                hidden = " hidden" if read(self, "author") is None else ""
                return f"{self.author_id}{hidden}"

        data = await self.execute(
            self.posts_schema(orm, PostType), "{ posts { title authorKey } }"
        )

        assert _by_title(data, "authorKey")["Hello World"] == "1 hidden"
        assert _by_title(data, "authorKey")["Draft Post"] == "2"

    async def test_hops_that_cannot_load_are_skipped_without_strict_hints(
        self, lenient_orm, models
    ):
        @lenient_orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

            @lenient_orm.field.lazy(using=["author.name", "author.nope", "nope.x"])
            def shout(self, info: strawberry.Info) -> str:
                return self.title.upper()

        data = await self.execute(
            self.posts_schema(lenient_orm, PostType), "{ posts { title shout } }"
        )

        assert _by_title(data, "shout")["Draft Post"] == "DRAFT POST"

    async def test_rows_the_resolver_already_loaded(self, orm, models):
        read = self.read

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy(using=["author.posts"])
            def author_post_count(self, info: strawberry.Info) -> int:
                return len(read(read(self, "author"), "posts"))

        @strawberry.type
        class Query:
            posts: list[PostType] = strawberry.field(resolver=self.materialized_posts)

        data = await self.execute(
            orm.schema(query=Query), "{ posts { title authorPostCount } }"
        )

        assert _by_title(data, "authorPostCount") == COUNTS_BY_TITLE
        assert self.query_count <= 4
