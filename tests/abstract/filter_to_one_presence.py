"""A to-one relation into a scoped type: a hidden related row reads as none.

A filter through the relation must answer the same for a row whose related row
the caller cannot read as for a row with no related row at all - asked
directly, through ``isNull``, or under ``not:`` - or the answer tells the two
apart.

Backends supply:

``orm``                             a fresh ORM
``models``                          ``Comment``
``run(schema, query, variables)``   awaitable result of running *query*
``narrow``                          ``exclude``, ``(query, model, field, value)``
                                    returning *query* without the rows where
                                    *field* is *value*

The seed: Bob wrote "Nice post!", Alice "Thanks!" in reply to it, and Charlie
"Great guide", which replies to nothing.
"""

import strawberry

from strawberry_orm.types import auto

BOB, CHARLIE = 2, 3

PARENT = "{ comments(filter: { object: { parent: %s } }) { body } }"
NOT_PARENT = "{ comments(filter: { not: { object: { parent: %s } } }) { body } }"


def _schema(orm, models, narrow, hidden_author):
    """Comments, a reply's parent among them, by every author but one."""
    Comment = models.Comment

    class ParentType:
        id: auto
        body: auto

        @classmethod
        def scope_rows(cls, query, info):
            return narrow.exclude(query, Comment, "author_id", hidden_author)

    # A filter goes on through a parent only to what it shows. Set here, as
    # types local to a function cannot be named in a string.
    ParentType.__annotations__["parent"] = ParentType | None
    orm.type(Comment, name="ParentComment", filters=orm.filter(Comment))(ParentType)

    @orm.type(Comment, name="ReplyComment", filters=orm.filter(Comment))
    class ReplyType:
        id: auto
        body: auto
        parent: ParentType | None

    @strawberry.type
    class Query:
        comments: list[ReplyType] = orm.field.auto()

    return orm.schema(query=Query)


def bodies(*names):
    return {"comments": [{"body": name} for name in names]}


class AbstractTestToOnePresenceIntoAScopedType:
    """Bob is hidden: "Thanks!" replies to a row the caller cannot read."""

    async def test_a_hidden_parent_is_null(self, orm, models, narrow, run):
        schema = _schema(orm, models, narrow, BOB)
        result = await run(schema, PARENT % "{ isNull: true }")
        assert result.data == bodies("Thanks!", "Great guide")

    async def test_a_hidden_parent_is_not_present(self, orm, models, narrow, run):
        schema = _schema(orm, models, narrow, BOB)
        result = await run(schema, PARENT % "{ isNull: false }")
        assert result.data == bodies()

    async def test_not_present_matches_a_hidden_parent(self, orm, models, narrow, run):
        schema = _schema(orm, models, narrow, BOB)
        result = await run(schema, NOT_PARENT % "{ isNull: false }")
        assert result.data == bodies("Thanks!", "Great guide")

    async def test_not_null_skips_a_hidden_parent(self, orm, models, narrow, run):
        schema = _schema(orm, models, narrow, BOB)
        result = await run(schema, NOT_PARENT % "{ isNull: true }")
        assert result.data == bodies()

    async def test_a_hidden_parent_is_not_matched(self, orm, models, narrow, run):
        schema = _schema(orm, models, narrow, BOB)
        result = await run(
            schema, PARENT % '{ field: { body: { startsWith: "Nice" } } }'
        )
        assert result.data == bodies()

    async def test_not_a_match_answers_alike_for_a_hidden_or_no_parent(
        self, orm, models, narrow, run
    ):
        schema = _schema(orm, models, narrow, BOB)
        hidden = await run(
            schema, NOT_PARENT % '{ field: { body: { startsWith: "Nice" } } }'
        )
        absent = await run(
            schema, NOT_PARENT % '{ field: { body: { startsWith: "zzz" } } }'
        )
        assert hidden.data == absent.data == bodies("Thanks!", "Great guide")

    async def test_not_the_hidden_parents_key_matches_it(
        self, orm, models, narrow, run
    ):
        schema = _schema(orm, models, narrow, BOB)
        result = await run(schema, NOT_PARENT % "{ field: { id: { exact: 1 } } }")
        assert result.data == bodies("Thanks!", "Great guide")


class AbstractTestToOnePresenceOfAReadableRow:
    """Charlie is hidden: "Thanks!" replies to a row the caller can read."""

    async def test_no_parent_is_null(self, orm, models, narrow, run):
        schema = _schema(orm, models, narrow, CHARLIE)
        result = await run(schema, PARENT % "{ isNull: true }")
        assert result.data == bodies("Nice post!")

    async def test_a_readable_parent_is_present(self, orm, models, narrow, run):
        schema = _schema(orm, models, narrow, CHARLIE)
        result = await run(schema, PARENT % "{ isNull: false }")
        assert result.data == bodies("Thanks!")

    async def test_not_present_skips_a_readable_parent(self, orm, models, narrow, run):
        schema = _schema(orm, models, narrow, CHARLIE)
        result = await run(schema, NOT_PARENT % "{ isNull: false }")
        assert result.data == bodies("Nice post!")

    async def test_a_readable_parent_is_matched(self, orm, models, narrow, run):
        schema = _schema(orm, models, narrow, CHARLIE)
        result = await run(
            schema, PARENT % '{ field: { body: { startsWith: "Nice" } } }'
        )
        assert result.data == bodies("Thanks!")

    async def test_not_a_match_skips_a_readable_match(self, orm, models, narrow, run):
        schema = _schema(orm, models, narrow, CHARLIE)
        result = await run(
            schema, NOT_PARENT % '{ field: { body: { startsWith: "Nice" } } }'
        )
        assert result.data == bodies("Nice post!")

    async def test_a_second_hop_through_the_relation_is_applied(
        self, orm, models, narrow, run
    ):
        """However deep, each hop takes the whole filter, ``object`` included.

        No comment here has a grandparent, let alone a great-grandparent; a
        hop left without ``object`` would match every comment with a parent.
        """
        schema = _schema(orm, models, narrow, CHARLIE)
        hop = "{ object: { parent: %s } }"
        for depth in (1, 2):
            deepest = "{ isNull: false }"
            for _ in range(depth):
                deepest = hop % deepest
            result = await run(schema, PARENT % deepest)
            assert result.data == bodies()
        result = await run(schema, PARENT % (hop % "{ isNull: true }"))
        assert result.data == bodies("Thanks!")
