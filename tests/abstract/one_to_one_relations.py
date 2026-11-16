"""A one-to-one relation is a relation everywhere a client can name it.

Read as a column, its key could be compared with any value, through the
related type's scope, and written straight into a node input past the related
repo's checks.

Backends supply:

``orm``                             a fresh ORM
``models``                          ``Member`` (``name``) and ``Profile``
                                    (``bio``, ``is_public``), joined one-to-one
                                    by ``Profile.member``, ``Member.profile``
                                    the other way
``run(schema, query, variables)``   awaitable result of running *query*
``narrow``                          ``exclude``, ``(query, model, field, value)``
                                    returning *query* without the rows where
                                    *field* is *value*

The seed: Ann's profile is public and Ben's private; Cy has none.
"""

import strawberry

from strawberry_orm.types import auto
from strawberry_orm.upserts import _declared_fields

BEN = 2


def fields(schema, type_name):
    return set(schema._schema.type_map[type_name].fields)


def _profiles_schema(orm, models, narrow, hidden_member=None):
    Member, Profile = models.Member, models.Profile

    @orm.type(Member, filters=orm.filter(Member), order=orm.order(Member))
    class MemberType:
        id: auto
        name: auto

        if hidden_member is not None:

            @classmethod
            def scope_rows(cls, query, info):
                return narrow.exclude(query, Member, "id", hidden_member)

    # Ordering through a scoped relation is refused when the order is built.
    order = orm.order(Profile) if hidden_member is None else None

    @orm.type(Profile, filters=orm.filter(Profile), order=order)
    class ProfileType:
        id: auto
        bio: auto
        is_public: auto
        member: MemberType

    @strawberry.type
    class Query:
        profiles: list[ProfileType] = orm.field.auto()

    return orm.schema(query=Query)


def _members_schema(orm, models, narrow, hide_private=False):
    Member, Profile = models.Member, models.Profile

    @orm.type(Profile, filters=orm.filter(Profile))
    class ProfileType:
        id: auto
        bio: auto

        if hide_private:

            @classmethod
            def scope_rows(cls, query, info):
                return narrow.exclude(query, Profile, "is_public", False)

    @orm.type(Member, filters=orm.filter(Member))
    class MemberType:
        id: auto
        name: auto
        profile: ProfileType | None

    @strawberry.type
    class Query:
        members: list[MemberType] = orm.field.auto()

    return orm.schema(query=Query)


def bios(*names):
    return {"profiles": [{"bio": name} for name in names]}


def names(*found):
    return {"members": [{"name": name} for name in found]}


MEMBER = "{ profiles(filter: { object: { member: %s } }) { bio } }"
PROFILE = "{ members(filter: { object: { profile: %s } }) { name } }"
NOT_PROFILE = "{ members(filter: { not: { object: { profile: %s } } }) { name } }"


class AbstractTestForwardOneToOne:
    async def test_it_is_no_column_of_the_filter_or_order(
        self, orm, models, narrow, run
    ):
        schema = _profiles_schema(orm, models, narrow)
        assert fields(schema, "ProfileField") == {"id", "bio", "isPublic"}
        assert fields(schema, "ProfileOrderField") == {"id", "bio", "isPublic"}
        assert "member" in fields(schema, "ProfileFilterObject")

    async def test_its_key_cannot_be_compared_as_a_column(
        self, orm, models, narrow, run
    ):
        schema = _profiles_schema(orm, models, narrow, hidden_member=BEN)
        result = await run(
            schema,
            '{ profiles(filter: { field: { member: { exact: "2" } } }) { bio } }',
        )
        assert result.data is None
        assert [error.message for error in result.errors] == [
            "Expected value of type 'ProfileField' not to include unknown field "
            "'member', found: { member: { exact: \"2\" } }."
        ]

    async def test_it_matches_through_the_related_row(self, orm, models, narrow, run):
        schema = _profiles_schema(orm, models, narrow)
        result = await run(schema, MEMBER % '{ field: { name: { exact: "Ben" } } }')
        assert result.data == bios("Ben's")

    async def test_a_hidden_related_row_is_not_matched(self, orm, models, narrow, run):
        schema = _profiles_schema(orm, models, narrow, hidden_member=BEN)
        by_name = await run(schema, MEMBER % '{ field: { name: { exact: "Ben" } } }')
        by_key = await run(schema, MEMBER % "{ field: { id: { exact: 2 } } }")
        assert by_name.data == by_key.data == bios()

    async def test_a_hidden_related_row_is_null(self, orm, models, narrow, run):
        schema = _profiles_schema(orm, models, narrow, hidden_member=BEN)
        result = await run(schema, MEMBER % "{ isNull: true }")
        assert result.data == bios("Ben's")

    async def test_rows_order_through_the_related_row(self, orm, models, narrow, run):
        schema = _profiles_schema(orm, models, narrow)
        result = await run(
            schema,
            "{ profiles(order: [{ object: { member: { field: { name: DESC } } } }]) "
            "{ bio } }",
        )
        assert result.data == bios("Ben's", "Ann's")


class AbstractTestReverseOneToOne:
    async def test_a_hidden_row_is_null(self, orm, models, narrow, run):
        schema = _members_schema(orm, models, narrow, hide_private=True)
        absent = await run(schema, PROFILE % "{ isNull: true }")
        present = await run(schema, PROFILE % "{ isNull: false }")
        assert absent.data == names("Ben", "Cy")
        assert present.data == names("Ann")

    async def test_not_a_match_answers_alike_for_a_hidden_or_no_row(
        self, orm, models, narrow, run
    ):
        schema = _members_schema(orm, models, narrow, hide_private=True)
        hidden = await run(
            schema, NOT_PROFILE % '{ field: { bio: { startsWith: "Ben" } } }'
        )
        absent = await run(
            schema, NOT_PROFILE % '{ field: { bio: { startsWith: "zzz" } } }'
        )
        assert hidden.data == absent.data == names("Ann", "Ben", "Cy")

    async def test_it_reads_through_the_related_types_scope(
        self, orm, models, narrow, run
    ):
        schema = _members_schema(orm, models, narrow, hide_private=True)
        result = await run(schema, "{ members { name profile { bio } } }")
        assert result.data == {
            "members": [
                {"name": "Ann", "profile": {"bio": "Ann's"}},
                {"name": "Ben", "profile": None},
                {"name": "Cy", "profile": None},
            ]
        }


class AbstractTestOneToOneNodeInputs:
    async def test_its_key_is_written_only_through_the_relation(self, orm, models):
        Profile = models.Profile
        create = orm.mutations.create_node_input(
            models=[Profile], project={"profile": {"member": {}}}, name="CreateNode"
        )
        upsert = orm.mutations.upsert_node_input(
            model=Profile,
            project={
                "_meta": {"create": True, "update": True, "upsert": {"where": ["id"]}},
                "member": {},
            },
            name="ProfileUpsert",
        )
        for generated in (
            _declared_fields(create)["profile"],
            _declared_fields(upsert)["create"],
            _declared_fields(upsert)["update"],
        ):
            assert set(_declared_fields(generated)) == {"bio", "is_public", "member"}
