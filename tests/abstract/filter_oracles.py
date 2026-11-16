"""A filter must not answer questions about rows the caller cannot read.

Each class drives one way a filter could be turned into an oracle on hidden
rows - or simply give the wrong answer - and the cases that must keep working
next to it.

Backends supply:

``orm``                             a fresh ORM
``models``                          ``User``, ``Post``, ``Comment``, ``Tag``,
                                    ``Publisher`` and ``Book``
``run(schema, query, variables)``   awaitable result of running *query*
``narrow``                          ``equal``, ``exclude`` and ``starts_with``,
                                    each ``(query, model, field, value)`` and
                                    returning *query* narrowed that way
``numeric_text_keys``               seeds publishers ``00123`` (Zero Padded)
                                    and ``123`` (Plain), with the books
                                    "Padded" and "Plain Book"
``statements()``                    context manager yielding the list of SQL
                                    statements sent while it is open

The seed: Alice wrote posts 1 and 2, Bob post 3 (a draft), Charlie post 4.
Post 1 is tagged python; post 2 python and graphql; post 4 rust. Bob wrote
"Nice post!" and Alice "Thanks!" on post 1; Charlie "Great guide" on post 2.
Posts have word counts 100, 250, 40 and 310.
"""

import re

import strawberry
from strawberry import relay

from strawberry_orm import filter_field
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto

ALL_TITLES = ["Draft Post", "GraphQL Guide", "Hello World", "Rust Adventures"]


def rows(result, field, column):
    assert result.errors is None, result.errors
    return sorted(row[column] for row in result.data[field])


def titles(result):
    return rows(result, "posts", "title")


# ---------------------------------------------------------------------------
# not: across a to-many relation
# ---------------------------------------------------------------------------

NOT_COMMENT = (
    "{ posts(filter: { not: { object: { comments: { field: { body: "
    '{ startsWith: "%s" } } } } } }) { title } }'
)
NOT_TAG = (
    "{ posts(filter: { not: { object: { tags: { field: { name: "
    '{ exact: "%s" } } } } } }) { title } }'
)


def _negation_schema(orm, models, narrow):
    """Only Alice's comments, and every tag but ``graphql``, are readable."""
    Comment, Post, Tag = models.Comment, models.Post, models.Tag
    comment_filter = orm.filter(Comment)
    tag_filter = orm.filter(Tag)
    post_filter = orm.filter(Post)

    @orm.type(Comment, filters=comment_filter)
    class CommentType:
        id: auto
        body: auto

        @classmethod
        def scope_rows(cls, query, info):
            return narrow.equal(query, Comment, "author_id", 1)

    @orm.type(Tag, filters=tag_filter)
    class TagType:
        id: auto
        name: auto

        @classmethod
        def scope_rows(cls, query, info):
            return narrow.exclude(query, Tag, "name", "graphql")

    @orm.type(Post, filters=post_filter)
    class PostType:
        id: auto
        title: auto
        comments: list[CommentType]
        tags: list[TagType]

    @strawberry.type
    class Query:
        posts: list[PostType] = orm.field.auto()

    return orm.schema(query=Query)


class AbstractTestNegationAcrossAToManyRelation:
    """``not:`` negates "a readable related row matches", as one condition.

    Negated piece by piece, the scope stops being tied to the row that
    matched: a hidden row's value then decides the answer, and a row with no
    related rows at all can drop out.
    """

    async def test_not_over_a_to_many_relation_ignores_hidden_rows(
        self, orm, models, narrow, run
    ):
        schema = _negation_schema(orm, models, narrow)
        hidden = titles(await run(schema, NOT_COMMENT % "Nice"))
        absent = titles(await run(schema, NOT_COMMENT % "zzz"))
        assert hidden == absent == ALL_TITLES

    async def test_not_over_a_to_many_relation_excludes_a_readable_match(
        self, orm, models, narrow, run
    ):
        schema = _negation_schema(orm, models, narrow)
        result = await run(schema, NOT_COMMENT % "Thanks")
        assert titles(result) == ["Draft Post", "GraphQL Guide", "Rust Adventures"]

    async def test_not_over_a_to_many_relation_keeps_rows_with_no_related_rows(
        self, orm, models, narrow, run
    ):
        schema = _negation_schema(orm, models, narrow)
        assert titles(await run(schema, NOT_COMMENT % "zzz")) == ALL_TITLES

    async def test_not_over_a_many_to_many_relation_ignores_hidden_rows(
        self, orm, models, narrow, run
    ):
        schema = _negation_schema(orm, models, narrow)
        assert titles(await run(schema, NOT_TAG % "graphql")) == ALL_TITLES

    async def test_not_over_a_many_to_many_relation_excludes_a_readable_match(
        self, orm, models, narrow, run
    ):
        schema = _negation_schema(orm, models, narrow)
        result = await run(schema, NOT_TAG % "python")
        assert titles(result) == ["Draft Post", "Rust Adventures"]

    async def test_a_to_many_match_is_still_scoped(self, orm, models, narrow, run):
        schema = _negation_schema(orm, models, narrow)
        query = (
            "{ posts(filter: { object: { comments: { field: { body: "
            '{ startsWith: "%s" } } } } }) { title } }'
        )
        assert titles(await run(schema, query % "Nice")) == []
        assert titles(await run(schema, query % "Thanks")) == ["Hello World"]


# ---------------------------------------------------------------------------
# @filter_field through object:, and under not: / any: / oneOf:
# ---------------------------------------------------------------------------


def _custom_filter_schema(orm, models, narrow):
    """Bob is hidden; users and comments carry a ``@filter_field`` each."""
    User, Post, Comment = models.User, models.Post, models.Comment

    @orm.filter_type(User)
    class UserFilter:
        name: auto

        @filter_field
        def email_starts(self, value: str, query):
            return narrow.starts_with(query, User, "email", value)

    @orm.filter_type(Comment)
    class CommentFilter:
        body: auto

        @filter_field
        def body_starts(self, value: str, query):
            return narrow.starts_with(query, Comment, "body", value)

    post_filter = orm.filter(Post)

    @orm.type(User, filters=UserFilter)
    class UserType:
        id: auto
        name: auto

        @classmethod
        def scope_rows(cls, query, info):
            return narrow.exclude(query, User, "name", "Bob")

    @orm.type(Comment, filters=CommentFilter)
    class CommentType:
        id: auto
        body: auto

    @orm.type(Post, filters=post_filter)
    class PostType:
        id: auto
        title: auto
        author: UserType
        comments: list[CommentType]

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.auto()
        posts: list[PostType] = orm.field.auto()

    return orm.schema(query=Query)


THROUGH_AUTHOR = (
    '{ posts(filter: { object: { author: { emailStarts: "%s" } } }) { title } }'
)


def names(result):
    return rows(result, "users", "name")


class AbstractTestCustomFiltersFollowTheirOperator:
    """A ``@filter_field`` means what the operators around it say.

    Through ``object:`` it narrows the related rows the caller can read and
    is tied back to each parent, rather than narrowing the parent's query on
    its own. Under ``not:`` it is negated, and in ``any:`` it is one
    alternative, rather than being applied as if the operator were absent.
    """

    async def test_custom_filter_through_object_cannot_probe_a_hidden_row(
        self, orm, models, narrow, run
    ):
        schema = _custom_filter_schema(orm, models, narrow)
        assert titles(await run(schema, THROUGH_AUTHOR % "bob@")) == []

    async def test_custom_filter_through_object_matches_the_related_row(
        self, orm, models, narrow, run
    ):
        schema = _custom_filter_schema(orm, models, narrow)
        result = await run(schema, THROUGH_AUTHOR % "alice@")
        assert titles(result) == ["GraphQL Guide", "Hello World"]

    async def test_custom_filter_through_a_to_many_relation(
        self, orm, models, narrow, run
    ):
        schema = _custom_filter_schema(orm, models, narrow)
        query = '{ posts(filter: { object: { comments: { bodyStarts: "Great" } } }) { title } }'
        assert titles(await run(schema, query)) == ["GraphQL Guide"]

    async def test_negated_custom_filter_through_object(self, orm, models, narrow, run):
        schema = _custom_filter_schema(orm, models, narrow)
        query = (
            "{ posts(filter: { not: { object: { author: "
            '{ emailStarts: "alice@" } } } }) { title } }'
        )
        result = await run(schema, query)
        assert titles(result) == ["Draft Post", "Rust Adventures"]

    async def test_custom_filter_under_not_is_negated(self, orm, models, narrow, run):
        schema = _custom_filter_schema(orm, models, narrow)
        query = '{ users(filter: { not: { emailStarts: "alice@" } }) { name } }'
        assert names(await run(schema, query)) == ["Charlie"]

    async def test_custom_filter_in_any_is_one_alternative(
        self, orm, models, narrow, run
    ):
        schema = _custom_filter_schema(orm, models, narrow)
        query = (
            '{ users(filter: { any: [{ emailStarts: "alice@" }, '
            '{ field: { name: { exact: "Charlie" } } }] }) { name } }'
        )
        assert names(await run(schema, query)) == ["Alice", "Charlie"]

    async def test_custom_filter_in_one_of_is_one_alternative(
        self, orm, models, narrow, run
    ):
        schema = _custom_filter_schema(orm, models, narrow)
        query = (
            '{ users(filter: { oneOf: [{ emailStarts: "alice@" }, '
            '{ field: { name: { exact: "Charlie" } } }] }) { name } }'
        )
        assert names(await run(schema, query)) == ["Alice", "Charlie"]

    async def test_custom_filter_at_the_top_still_narrows(
        self, orm, models, narrow, run
    ):
        schema = _custom_filter_schema(orm, models, narrow)
        alone = '{ users(filter: { emailStarts: "charlie@" }) { name } }'
        combined = (
            '{ users(filter: { all: [{ emailStarts: "c" }, '
            '{ field: { name: { exact: "Charlie" } } }] }) { name } }'
        )
        assert names(await run(schema, alone)) == ["Charlie"]
        assert names(await run(schema, combined)) == ["Charlie"]

    async def test_custom_filter_at_the_top_cannot_reach_a_hidden_row(
        self, orm, models, narrow, run
    ):
        schema = _custom_filter_schema(orm, models, narrow)
        query = '{ users(filter: { any: [{ emailStarts: "bob@" }] }) { name } }'
        assert names(await run(schema, query)) == []


# ---------------------------------------------------------------------------
# The scope is written before the client's conditions
# ---------------------------------------------------------------------------


def _scope_first_schema(orm, models, narrow):
    """Users are read without Bob, at the root and as a post's author."""
    User, Post = models.User, models.Post
    user_filter = orm.filter(User)
    post_filter = orm.filter(Post)

    @orm.type(User, filters=user_filter)
    class UserType:
        id: auto
        name: auto
        email: auto

        @classmethod
        def scope_rows(cls, query, info):
            return narrow.exclude(query, User, "name", "Bob")

    @orm.type(Post, filters=post_filter)
    class PostType:
        id: auto
        title: auto
        author: UserType | None

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.auto()
        posts: list[PostType] = orm.field.auto()

    return orm.schema(query=Query)


def compared_in(sent, first, then):
    """The columns of the statement comparing *then*, as its WHERE first names them."""
    conditions = [s[s.upper().index("WHERE") :] for s in sent if "WHERE" in s.upper()]
    where = next(c for c in conditions if re.search(rf"\b{then}\b", c))
    return sorted([first, then], key=lambda c: re.search(rf"\b{c}\b", where).start())


class AbstractTestTheScopeIsWrittenFirst:
    """A database that runs conditions in the order written, as SQLite does,
    runs a client's condition - a costly regex, say - only on rows the scope
    lets through, so its cost cannot time a hidden row."""

    async def test_at_the_root(self, orm, models, narrow, run, statements):
        schema = _scope_first_schema(orm, models, narrow)
        query = '{ users(filter: { field: { email: { startsWith: "%s" } } }) { name } }'
        with statements() as sent:
            assert names(await run(schema, query % "bob")) == []
        assert compared_in(sent, "name", "email") == ["name", "email"]
        assert names(await run(schema, query % "alice")) == ["Alice"]

    async def test_through_a_relation(self, orm, models, narrow, run, statements):
        schema = _scope_first_schema(orm, models, narrow)
        query = (
            "{ posts(filter: { object: { author: { field: { email: "
            '{ startsWith: "%s" } } } } }) { title } }'
        )
        with statements() as sent:
            assert titles(await run(schema, query % "bob")) == []
        assert compared_in(sent, "name", "email") == ["name", "email"]
        assert titles(await run(schema, query % "charlie")) == ["Rust Adventures"]


# ---------------------------------------------------------------------------
# scope= of the type a field returns
# ---------------------------------------------------------------------------


def _edge_scope_schema(orm, models, narrow):
    """``PostAdmin`` reads every comment; ``PostPublic``, registered second,
    reads only Alice's. Comments themselves are not scoped, so the edge's
    ``scope=`` is all that hides Bob's and Charlie's."""
    User, Post, Comment = models.User, models.Post, models.Comment
    comment_filter = orm.filter(Comment)
    orm.group(Comment)
    post_filter = orm.filter(Post)
    post_group = orm.group(Post)
    user_filter = orm.filter(User)

    def only_alices(query, info):
        return narrow.equal(query, Comment, "author_id", 1)

    @orm.type(Comment, filters=comment_filter)
    class CommentType:
        id: auto
        body: auto

    @orm.type(Post, name="PostAdmin")
    class PostAdmin:
        id: auto
        title: auto
        comments: list[CommentType]

    @orm.type(Post, filters=post_filter, group=post_group)
    class PostPublic(relay.Node):
        id: relay.NodeID[int]
        title: auto
        comments: list[CommentType] = orm.field.eager(only_alices)

    @orm.type(User, filters=user_filter)
    class UserType:
        id: auto
        name: auto
        posts: list[PostPublic]

    @strawberry.type
    class Query:
        posts: list[PostPublic] = orm.field.auto()
        post_conn: ORMListConnection[PostPublic] = orm.connection.eager()
        users: list[UserType] = orm.field.auto()

    return orm.schema(query=Query)


THROUGH_COMMENTS = (
    "{ posts(filter: { object: { comments: { field: { body: "
    '{ startsWith: "%s" } } } } }) { title } }'
)


class AbstractTestEdgeScopeOfTheReturnedType:
    """A relation's ``scope=`` is read from the type the field returns.

    One model can be served by several types. Filtering and grouping at the
    root used to take the edge scope of whichever was registered first, so a
    public type's ``scope=`` was skipped when an admin type came before it.
    """

    async def test_filter_through_the_edge_applies_the_returned_types_scope(
        self, orm, models, narrow, run
    ):
        schema = _edge_scope_schema(orm, models, narrow)
        assert titles(await run(schema, THROUGH_COMMENTS % "Nice")) == []

    async def test_filter_through_the_edge_still_matches_a_readable_row(
        self, orm, models, narrow, run
    ):
        schema = _edge_scope_schema(orm, models, narrow)
        result = await run(schema, THROUGH_COMMENTS % "Thanks")
        assert titles(result) == ["Hello World"]

    async def test_grouping_through_the_edge_applies_the_returned_types_scope(
        self, orm, models, narrow, run
    ):
        schema = _edge_scope_schema(orm, models, narrow)
        result = await run(
            schema,
            "{ postConn(groupBy: [{ objects: { comments: { field: { body: true } } } }]) "
            "{ groups { key { objects { comments { body } } } } } }",
        )
        assert result.errors is None, result.errors
        bodies = {
            comment["body"]
            for group in result.data["postConn"]["groups"]
            for comment in group["key"]["objects"]["comments"]
        }
        assert bodies == {"Thanks!"}

    async def test_a_hop_of_unknown_type_applies_every_candidate_edge_scope(
        self, orm, models, narrow, run
    ):
        """Below the root the type of each hop is not tracked, so every type
        serving the model contributes its ``scope=``."""
        schema = _edge_scope_schema(orm, models, narrow)
        query = (
            "{ users(filter: { object: { posts: { object: { comments: { field: "
            '{ body: { startsWith: "%s" } } } } } } }) { name } }'
        )
        assert names(await run(schema, query % "Nice")) == []
        assert names(await run(schema, query % "Thanks")) == ["Alice"]

    async def test_a_hop_served_by_several_scoped_types_applies_all_their_scopes(
        self, orm, models, narrow, run
    ):
        schema = _two_edge_scopes_schema(orm, models, narrow)
        query = (
            "{ users(filter: { object: { posts: { object: { comments: { field: "
            '{ body: { startsWith: "%s" } } } } } } }) { name } }'
        )
        assert names(await run(schema, query % "Nice")) == []
        assert names(await run(schema, query % "Great")) == []
        assert names(await run(schema, query % "Thanks")) == ["Alice"]


def _two_edge_scopes_schema(orm, models, narrow):
    """Two types serve posts: one hides Charlie's comments, the other Bob's."""
    User, Post, Comment = models.User, models.Post, models.Comment
    comment_filter = orm.filter(Comment)
    post_filter = orm.filter(Post)
    user_filter = orm.filter(User)

    def without_charlies(query, info):
        return narrow.exclude(query, Comment, "author_id", 3)

    def without_bobs(query, info):
        return narrow.exclude(query, Comment, "author_id", 2)

    @orm.type(Comment, filters=comment_filter)
    class CommentType:
        id: auto
        body: auto

    @orm.type(Post, name="PostWithoutCharlie", filters=post_filter)
    class PostWithoutCharlie:
        id: auto
        comments: list[CommentType] = orm.field.eager(without_charlies)

    @orm.type(Post, name="PostWithoutBob")
    class PostWithoutBob:
        id: auto
        comments: list[CommentType] = orm.field.eager(without_bobs)

    @orm.type(User, filters=user_filter)
    class UserType:
        id: auto
        name: auto
        posts: list[PostWithoutBob]

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.auto()
        posts: list[PostWithoutCharlie] = orm.field.auto()

    return orm.schema(query=Query)


# ---------------------------------------------------------------------------
# The foreign-key shortcut
# ---------------------------------------------------------------------------


def _shortcut_schema(orm, models):
    Post, Comment = models.Post, models.Comment
    post_filter = orm.filter(Post)
    comment_filter = orm.filter(Comment)

    @orm.type(Post, filters=post_filter)
    class PostType:
        id: auto
        title: auto
        word_count: auto

    @orm.type(Comment, filters=comment_filter)
    class CommentType:
        id: auto
        body: auto
        post: PostType

    @strawberry.type
    class Query:
        comments: list[CommentType] = orm.field.auto()

    return orm.schema(query=Query)


THROUGH_POST = "{ comments(filter: { object: { post: { field: %s } } }) { body } }"


def bodies(result):
    return rows(result, "comments", "body")


class AbstractTestForeignKeyShortcutReadsOnlyTheKey:
    """``object.<relation>.field.<key>`` compares the parent's foreign key.

    Only the key the foreign key holds can be read off it. Any other integer
    column of the related model has to be compared on the related row, or
    ``wordCount == 2`` would match the posts whose id is 2.
    """

    async def test_another_integer_column_is_not_read_as_the_foreign_key(
        self, orm, models, run
    ):
        schema = _shortcut_schema(orm, models)
        query = THROUGH_POST % "{ wordCount: { exact: 1 } }"
        assert bodies(await run(schema, query)) == []

    async def test_another_integer_column_is_compared_on_the_related_row(
        self, orm, models, run
    ):
        schema = _shortcut_schema(orm, models)
        exact = THROUGH_POST % "{ wordCount: { exact: 100 } }"
        listed = THROUGH_POST % "{ wordCount: { inList: [250] } }"
        assert bodies(await run(schema, exact)) == ["Nice post!", "Thanks!"]
        assert bodies(await run(schema, listed)) == ["Great guide"]

    async def test_the_key_still_uses_the_shortcut(self, orm, models, run):
        schema = _shortcut_schema(orm, models)
        query = THROUGH_POST % "{ id: { exact: 2 } }"
        assert bodies(await run(schema, query)) == ["Great guide"]


def _reverse_schema(orm, models):
    User, Post = models.User, models.Post
    post_filter = orm.filter(Post)
    user_filter = orm.filter(User)

    @orm.type(Post, filters=post_filter)
    class PostType:
        id: auto
        title: auto

    @orm.type(User, filters=user_filter)
    class UserType:
        id: auto
        name: auto
        posts: list[PostType]

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.auto()

    return orm.schema(query=Query)


THROUGH_POSTS = "{ users(filter: { object: { posts: { field: %s } } }) { name } }"


class AbstractTestFiltersThroughAReverseRelation:
    """A reverse relation has no column on the parent to shortcut through."""

    async def test_a_reverse_relation_is_filtered_by_its_rows_key(
        self, orm, models, run
    ):
        schema = _reverse_schema(orm, models)
        query = THROUGH_POSTS % "{ id: { exact: 3 } }"
        assert names(await run(schema, query)) == ["Bob"]

    async def test_a_reverse_relation_is_filtered_by_a_list_of_keys(
        self, orm, models, run
    ):
        schema = _reverse_schema(orm, models)
        query = THROUGH_POSTS % "{ id: { inList: [1, 4] } }"
        assert names(await run(schema, query)) == ["Alice", "Charlie"]

    async def test_each_hop_through_a_to_many_relation_is_its_own_condition(
        self, orm, models, run
    ):
        schema = _reverse_schema(orm, models)
        query = """
            { users(filter: { all: [
                { object: { posts: { field: { title: { exact: "Hello World" } } } } }
                { object: { posts: { field: { title: { exact: "GraphQL Guide" } } } } }
            ] }) { name } }
        """
        assert names(await run(schema, query)) == ["Alice"]


# ---------------------------------------------------------------------------
# Reference lookups
# ---------------------------------------------------------------------------


def _reference_schema(orm, models):
    Publisher, Book = models.Publisher, models.Book
    publisher_filter = orm.filter(Publisher)
    book_filter = orm.filter(Book)

    @orm.type(Publisher, filters=publisher_filter)
    class PublisherType:
        publisher_code: auto
        name: auto

    @orm.type(Book, filters=book_filter)
    class BookType:
        id: auto
        title: auto
        publisher: PublisherType

    @strawberry.type
    class Query:
        publishers: list[PublisherType] = orm.field.auto()
        books: list[BookType] = orm.field.auto()

    return orm.schema(query=Query)


class AbstractTestReferenceLookupsKeepTheKeysType:
    """An ID is matched as the key column holds it, never guessed from its text.

    ``"00123"`` names a text key; read as the integer 123 it misses that row
    and matches the one keyed ``"123"`` instead.
    """

    async def test_a_numeric_looking_text_key_matches_itself(
        self, orm, models, run, numeric_text_keys
    ):
        schema = _reference_schema(orm, models)
        exact = '{ publishers(filter: { field: { publisherCode: { exact: "00123" } } }) { name } }'
        listed = '{ publishers(filter: { field: { publisherCode: { inList: ["00123"] } } }) { name } }'
        assert rows(await run(schema, exact), "publishers", "name") == ["Zero Padded"]
        assert rows(await run(schema, listed), "publishers", "name") == ["Zero Padded"]

    async def test_a_numeric_looking_text_key_through_the_foreign_key(
        self, orm, models, run, numeric_text_keys
    ):
        schema = _reference_schema(orm, models)
        query = (
            "{ books(filter: { object: { publisher: { field: { publisherCode: "
            '{ exact: "00123" } } } } }) { title } }'
        )
        assert rows(await run(schema, query), "books", "title") == ["Padded"]

    async def test_a_numeric_looking_text_key_excludes_only_itself(
        self, orm, models, run, numeric_text_keys
    ):
        schema = _reference_schema(orm, models)
        query = (
            "{ publishers(filter: { field: { publisherCode: "
            '{ notInList: ["00123"] } } }) { name } }'
        )
        assert rows(await run(schema, query), "publishers", "name") == ["Plain"]
