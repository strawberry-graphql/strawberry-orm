"""A foreign key to a unique column holds another column than the primary key.

``Member.org`` holds the org's ``code``, and each org's code is another org's
primary key, so a check that compares the key with primary keys finds the
wrong org. SecretOrg is hidden from orgs, and Bob - its member - from members.
"""

import pytest
import strawberry
from sqlalchemy import ForeignKey, String, create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


class Base(DeclarativeBase):
    pass


class KeyedOrg(Base):
    __tablename__ = "test_to_field_org"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[int] = mapped_column(unique=True)
    name: Mapped[str] = mapped_column(String(50))
    members: Mapped[list["KeyedMember"]] = relationship(
        back_populates="org", order_by="KeyedMember.id"
    )


class KeyedMember(Base):
    __tablename__ = "test_to_field_member"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    org_code: Mapped[int] = mapped_column(ForeignKey("test_to_field_org.code"))
    org: Mapped[KeyedOrg] = relationship(back_populates="members")


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                KeyedOrg(id=1, code=2, name="Public"),
                KeyedOrg(id=2, code=3, name="SecretOrg"),
                KeyedOrg(id=3, code=1, name="Other"),
                KeyedMember(id=1, name="Alice", org_code=2),
                KeyedMember(id=2, name="Bob", org_code=3),
                KeyedMember(id=3, name="Carol", org_code=1),
            ]
        )
        session.commit()
        session.expunge_all()
        yield session


def build(*, hide_orgs=True, hide_members=False, by_members=False):
    orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", warn_missing_scope=False)

    def org_scope(cls, stmt, info):
        return stmt.where(KeyedOrg.name != "SecretOrg") if hide_orgs else stmt

    def member_scope(cls, stmt, info):
        return stmt.where(KeyedMember.name != "Bob") if hide_members else stmt

    # Of two models that relate to each other, the group-by built first cannot
    # group by the other, and one built again after it differs from it: a
    # schema groups members by their org, or orgs by their members.
    org_group = None if by_members else orm.group(KeyedOrg)

    @orm.type(
        KeyedOrg,
        filters=orm.filter(KeyedOrg),
        order=orm.order(KeyedOrg),
        group=org_group,
    )
    class OrgNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

        scope_rows = classmethod(org_scope)

    @orm.type(
        KeyedMember,
        filters=orm.filter(KeyedMember),
        order=orm.order(KeyedMember, exclude=["org"]),
        group=orm.group(KeyedMember),
    )
    class MemberNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        org: OrgNode | None

        scope_rows = classmethod(member_scope)

    # Grouped too, its connection would take the name MemberNode's has.
    @orm.type(KeyedMember)
    class MemberOfOrg(relay.Node):
        id: relay.NodeID[int]
        name: auto

        scope_rows = classmethod(member_scope)

    @orm.type(
        KeyedOrg,
        order=orm.order(KeyedOrg, exclude=["members"]),
        group=orm.group(KeyedOrg) if by_members else org_group,
    )
    class OrgWithMembers(relay.Node):
        id: relay.NodeID[int]
        name: auto
        members: ORMListConnection[MemberOfOrg] = orm.connection.eager()

        scope_rows = classmethod(org_scope)

    @strawberry.type
    class Query:
        members: ORMListConnection[MemberNode] = orm.connection.eager()
        orgs: ORMListConnection[OrgWithMembers] = orm.connection.eager()

    return orm.schema(query=Query)


@pytest.fixture
def run(session):
    def _run(schema, query):
        result = schema.execute_sync(query, context_value={"session": session})
        assert result.errors is None, result.errors
        return result.data

    return _run


def names(connection):
    return [edge["node"]["name"] for edge in connection["edges"]]


class TestToFieldKeys:
    def test_a_member_of_a_hidden_org_reads_no_org(self, run):
        data = run(
            build(),
            "{ members(order: [{field: {id: ASC}}]) { edges { node { name org { name } } } } }",
        )
        assert [edge["node"] for edge in data["members"]["edges"]] == [
            {"name": "Alice", "org": {"name": "Public"}},
            {"name": "Bob", "org": None},
            {"name": "Carol", "org": {"name": "Other"}},
        ]

    def test_grouping_by_the_orgs_name_hides_the_hidden_orgs_name(self, run):
        data = run(
            build(),
            """{ members(order: [{field: {id: ASC}}],
                         groupBy: [{object: {org: {field: {name: true}}}}]) {
                groups { key { org { name } } aggregates { count }
                         items { edges { node { name } } } } } }""",
        )
        groups = {
            group["key"]["org"]["name"]: (
                group["aggregates"]["count"],
                names(group["items"]),
            )
            for group in data["members"]["groups"]
        }
        assert groups == {
            "Public": (1, ["Alice"]),
            None: (1, ["Bob"]),
            "Other": (1, ["Carol"]),
        }

    def test_grouping_orgs_by_their_members_follows_the_key(self, run):
        data = run(
            build(hide_orgs=False, hide_members=True, by_members=True),
            """{ orgs(order: [{field: {id: ASC}}],
                      groupBy: [{objects: {members: {field: {name: true}}}}]) {
                groups { key { objects { members { name } } }
                         items { edges { node { name } } } } } }""",
        )
        groups = {
            tuple(
                member["name"] for member in group["key"]["objects"]["members"]
            ): names(group["items"])
            for group in data["orgs"]["groups"]
        }
        assert groups == {
            ("Alice",): ["Public"],
            (): ["SecretOrg"],
            ("Carol",): ["Other"],
        }

    def test_an_orgs_members_connection_holds_its_own_members(self, run):
        data = run(
            build(),
            """{ orgs(order: [{field: {id: ASC}}]) { edges { node {
                name members(first: 5) { totalCount edges { node { name } } } } } } }""",
        )
        assert [edge["node"] for edge in data["orgs"]["edges"]] == [
            {
                "name": "Public",
                "members": {"totalCount": 1, "edges": [{"node": {"name": "Alice"}}]},
            },
            {
                "name": "Other",
                "members": {"totalCount": 1, "edges": [{"node": {"name": "Carol"}}]},
            },
        ]

    def test_a_filter_through_the_key_sees_the_hidden_org_as_absent(self, run):
        schema = build()
        by_org = (
            '{ members(filter: {object: {org: {field: {name: {exact: "%s"}}}}})'
            " { edges { node { name } } } }"
        )
        assert run(schema, by_org % "SecretOrg") == {"members": {"edges": []}}
        assert run(schema, by_org % "Other") == {
            "members": {"edges": [{"node": {"name": "Carol"}}]}
        }
