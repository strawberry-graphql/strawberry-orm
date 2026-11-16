"""A one-to-one relation is a relation everywhere a client can name it (Tortoise)."""

from types import SimpleNamespace

import pytest
import pytest_asyncio
import strawberry
from tortoise import Tortoise

from strawberry_orm import AbstractRepo, StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.one_to_one_relations import (
    PROFILE,
    AbstractTestForwardOneToOne,
    AbstractTestOneToOneNodeInputs,
    AbstractTestReverseOneToOne,
    _members_schema,
    names,
)
from tests.backends.tortoise.one_to_one_models import Member, Profile


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest_asyncio.fixture
async def models():
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"one_to_one": ["tests.backends.tortoise.one_to_one_models"]},
    )
    await Tortoise.generate_schemas()
    yield SimpleNamespace(Member=Member, Profile=Profile)
    await Tortoise.close_connections()


@pytest.fixture
def narrow():
    return SimpleNamespace(
        exclude=lambda query, model, field, value: query.exclude(**{field: value}),
    )


@pytest_asyncio.fixture
async def run(models):
    ann = await Member.create(id=1, name="Ann")
    ben = await Member.create(id=2, name="Ben")
    await Member.create(id=3, name="Cy")
    await Profile.create(id=1, bio="Ann's", is_public=True, member=ann)
    await Profile.create(id=2, bio="Ben's", is_public=False, member=ben)

    async def _run(schema, query, variables=None):
        return await schema.execute(query, variable_values=variables, context_value={})

    return _run


async def test_a_missing_reverse_row_is_null_without_a_scope(orm, models, narrow, run):
    schema = _members_schema(orm, models, narrow)
    absent = await run(schema, PROFILE % "{ isNull: true }")
    present = await run(schema, PROFILE % "{ isNull: false }")
    assert absent.data == names("Cy")
    assert present.data == names("Ann", "Ben")


@pytest.mark.parametrize("optimizer", [True, False])
async def test_a_reverse_row_of_returned_rows_is_read_through_the_scope(
    orm, models, run, optimizer
):
    """Rows a resolver returns, which the optimizer has no query to plan for."""

    @orm.type(Profile)
    class ProfileType:
        bio: auto

        @classmethod
        def scope_rows(cls, query, info):
            return query.filter(is_public=True)

    @orm.type(Member)
    class MemberType:
        name: auto
        profile: ProfileType | None

    @strawberry.type
    class Query:
        @strawberry.field
        async def members(self) -> list[MemberType]:
            return list(await Member.all().order_by("id"))

    result = await run(
        orm.schema(query=Query, optimizer=optimizer),
        "{ members { name profile { bio } } }",
    )
    assert result.data == {
        "members": [
            {"name": "Ann", "profile": {"bio": "Ann's"}},
            {"name": "Ben", "profile": None},
            {"name": "Cy", "profile": None},
        ]
    }


async def test_a_one_to_one_is_linked_through_the_related_repo(models, run):
    links = []

    class MemberRepo(AbstractRepo[Member]):
        def scope_query(self, query, info):
            return query.exclude(name="Ben")

        def can_link(self, parent, field, instance, info):
            links.append((field, instance.name))
            return True

    orm = StrawberryORM.for_tortoise(
        repos={Member: MemberRepo}, lazy_resolution="off", warn_missing_scope=False
    )
    ProfileUpsert = orm.mutations.upsert_node_input(
        model=Profile,
        project={
            "_meta": {"update": True, "upsert": {"where": ["id"], "onMatch": "UPDATE"}},
            "member": {},
        },
        name="ProfileUpsert",
    )

    @strawberry.type
    class Query:
        ok: bool = True

    @strawberry.type
    class Mutation:
        @strawberry.mutation
        async def save_profile(
            self, info: strawberry.Info, input: ProfileUpsert
        ) -> bool:
            await orm.apply_upsert(input, info)
            return True

    schema = orm.schema(query=Query, mutation=Mutation)
    link = (
        'mutation { saveProfile(input: { where: { id: "1" }, '
        'update: { member: { update: { id: "%s" } } } }) }'
    )
    hidden = await run(schema, link % 2)
    visible = await run(schema, link % 3)
    assert [error.message for error in hidden.errors] == ["No Member 2"]
    assert visible.data == {"saveProfile": True}
    assert links == [("member", "Cy")]
    assert (await Profile.get(id=1)).member_id == 3


class TestForwardOneToOne(AbstractTestForwardOneToOne):
    pass


class TestReverseOneToOne(AbstractTestReverseOneToOne):
    pass


class TestOneToOneNodeInputs(AbstractTestOneToOneNodeInputs):
    pass
