"""A row is read as the type it was cast to only in the execution that cast it.

``strawberry.cast(StaffMember, row)`` marks the row object, and an interface
or union reads a marked row as that type. Rows can outlive a request: an app
caches them, or a long-lived session's identity map hands back the same
objects. So a cast one request made, whether by the app, by a list field of
the library's or by ``node()``, must not decide how a later request reads the
row. There, with ``PublicMember`` and ``StaffMember`` both over the row's
model, the row is refused as ambiguous, as it was before anything cast it.

A backend provides ``orm`` (a fresh ORM), ``models`` (``Member``) and
``cached`` (the member "ada", id 1, read once and kept between executions),
and on the test class:

- ``asynchronous``: whether ``run`` executes asynchronously;
- ``run(schema, query, context)``: executing with *context* as the context.
"""

import asyncio
from typing import Annotated

import pytest
import strawberry
from strawberry import relay
from strawberry.types.cast import get_strawberry_type_cast

from strawberry_orm import auto

EMAIL = "ada@corp.example"
REFUSED = "PublicMember and StaffMember each read"

STAMPS = {
    "staff": "{ staff { id } }",
    "promoted": "{ promoted { name } }",
    "node": '{ node(id: "%(staff)s") { id } }',
    "nodes": '{ nodes(ids: ["%(staff)s"]) { id } }',
}
READS = {
    "feed": "{ feed { name ... on StaffMember { email } } }",
    "people": "{ people { ... on PublicMember { name } ... on StaffMember { email } } }",
}


def _gid(type_name):
    return str(relay.GlobalID(type_name, "1"))


class AbstractTestCastsPerExecution:
    def schema(self, orm, models, cached, *, optimizer):
        suite = self
        MemberFilter = orm.filter(models.Member)

        def staff_only(info):
            if not info.context["staff"]:
                raise PermissionError("staff only")

        def pick(node_ids):
            by_id = {str(row.id): row for row in cached}
            return [by_id.get(str(pk)) for pk in node_ids]

        @strawberry.interface
        class Named:
            name: str

        @orm.type(models.Member, exclude=["email"])
        class PublicMember(Named, relay.Node):
            id: relay.NodeID[int]
            name: auto

            @classmethod
            def resolve_nodes(cls, *, info, node_ids, required=False):
                return pick(node_ids)

        @orm.type(models.Member)
        class StaffMember(Named, relay.Node):
            id: relay.NodeID[int]
            name: auto
            email: auto

            @classmethod
            def resolve_nodes(cls, *, info, node_ids, required=False):
                staff_only(info)
                return pick(node_ids)

        Person = Annotated[PublicMember | StaffMember, strawberry.union("Person")]

        @strawberry.type
        class Query:
            node: relay.Node | None = relay.node()
            nodes: list[relay.Node | None] = relay.node()

            @orm.field.lazy(filters=MemberFilter)
            def staff(self, info: strawberry.Info) -> list[StaffMember]:
                staff_only(info)
                return list(cached)

            @strawberry.field
            def promoted(self, info: strawberry.Info) -> list[Named]:
                staff_only(info)
                return [strawberry.cast(StaffMember, row) for row in cached]

            @strawberry.field
            def feed(self, info: strawberry.Info) -> list[Named]:
                return list(cached)

            @strawberry.field
            def people(self, info: strawberry.Info) -> list[Person]:
                return list(cached)

            @strawberry.field
            async def promoted_slowly(self, info: strawberry.Info) -> list[Named]:
                staff_only(info)
                rows = [strawberry.cast(StaffMember, row) for row in cached]
                suite.staff_cast.set()
                await suite.guest_cast.wait()
                return rows

            @strawberry.field
            async def publicized(self, info: strawberry.Info) -> list[Named]:
                await suite.staff_cast.wait()
                rows = [strawberry.cast(PublicMember, row) for row in cached]
                suite.guest_cast.set()
                return rows

        return orm.schema(
            query=Query, optimizer=optimizer, types=[PublicMember, StaffMember]
        )

    async def as_staff(self, schema, query):
        result = await self.run(schema, query, {"staff": True})
        assert result.errors is None, result.errors
        return result.data

    async def as_guest(self, schema, query):
        return await self.run(schema, query, {"staff": False})

    @staticmethod
    def assert_refused(result):
        assert EMAIL not in str(result.data), result.data
        assert result.errors, result.data
        assert REFUSED in result.errors[0].message, result.errors

    # -- a cast another execution made ----------------------------------------

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("read", sorted(READS))
    @pytest.mark.parametrize("stamp", sorted(STAMPS))
    async def test_a_cast_from_another_execution_does_not_type_the_row(
        self, orm, models, cached, stamp, read, optimizer
    ):
        schema = self.schema(orm, models, cached, optimizer=optimizer)
        await self.as_staff(schema, STAMPS[stamp] % {"staff": _gid("StaffMember")})
        self.assert_refused(await self.as_guest(schema, READS[read]))

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("read", sorted(READS))
    async def test_a_cast_made_outside_an_execution_does_not_type_the_row(
        self, orm, models, cached, read, optimizer
    ):
        schema = self.schema(orm, models, cached, optimizer=optimizer)
        strawberry.cast(schema.get_type_by_name("StaffMember").origin, cached[0])
        self.assert_refused(await self.as_guest(schema, READS[read]))

    # -- controls --------------------------------------------------------------

    def test_a_cast_made_outside_an_execution_holds_outside_one(
        self, orm, models, cached
    ):
        staff = self.schema(orm, models, cached, optimizer=True).get_type_by_name(
            "StaffMember"
        )
        assert get_strawberry_type_cast(cached[0]) is None
        strawberry.cast(staff.origin, cached[0])
        assert get_strawberry_type_cast(cached[0]) is staff.origin
        assert get_strawberry_type_cast(models.Member) is None

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("read", sorted(READS))
    async def test_a_row_nothing_cast_is_refused(
        self, orm, models, cached, read, optimizer
    ):
        schema = self.schema(orm, models, cached, optimizer=optimizer)
        self.assert_refused(await self.as_guest(schema, READS[read]))

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_a_cast_types_the_row_in_its_own_execution(
        self, orm, models, cached, optimizer
    ):
        schema = self.schema(orm, models, cached, optimizer=optimizer)
        data = await self.as_staff(
            schema,
            "{ promoted { name ... on StaffMember { email } } "
            f'node(id: "{_gid("StaffMember")}") {{ ... on StaffMember {{ email }} }} }}',
        )
        assert data == {
            "promoted": [{"name": "ada", "email": EMAIL}],
            "node": {"email": EMAIL},
        }, data
        guest = await self.as_guest(
            schema,
            f'{{ node(id: "{_gid("PublicMember")}") '
            "{ ... on PublicMember { name } ... on StaffMember { email } } }",
        )
        assert guest.errors is None, guest.errors
        assert guest.data == {"node": {"name": "ada"}}, guest.data

    async def test_concurrent_executions_each_keep_their_own_cast(
        self, orm, models, cached
    ):
        if not self.asynchronous:
            pytest.skip("concurrent executions need async execution")
        self.staff_cast = asyncio.Event()
        self.guest_cast = asyncio.Event()
        schema = self.schema(orm, models, cached, optimizer=True)
        selection = "{ name ... on StaffMember { email } }"
        staff, guest = await asyncio.gather(
            self.as_staff(schema, f"{{ promotedSlowly {selection} }}"),
            self.as_guest(schema, f"{{ publicized {selection} }}"),
        )
        assert staff == {"promotedSlowly": [{"name": "ada", "email": EMAIL}]}, staff
        assert guest.errors is None, guest.errors
        assert guest.data == {"publicized": [{"name": "ada"}]}, guest.data
