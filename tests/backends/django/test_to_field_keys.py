"""A key declared with ``to_field`` holds another column than the primary key.

``Member.org`` holds the org's ``code``, and each org's code is another org's
primary key, so a check that compares the key with primary keys finds the
wrong org. SecretOrg is hidden from orgs, and Bob - its member - from members.
"""

import pytest
import strawberry
from django.db import connection, models
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


class KeyedOrg(models.Model):
    id = models.AutoField(primary_key=True)
    code = models.IntegerField(unique=True)
    name = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "test_to_field_org"


class KeyedMember(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=50)
    org = models.ForeignKey(
        KeyedOrg, to_field="code", on_delete=models.CASCADE, related_name="members"
    )

    class Meta:
        app_label = "testapp"
        db_table = "test_to_field_member"


@pytest.fixture
def orgs(setup_tables):
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in (KeyedOrg, KeyedMember):
            if model._meta.db_table not in existing:
                editor.create_model(model)
    public = KeyedOrg.objects.create(id=1, code=2, name="Public")
    secret = KeyedOrg.objects.create(id=2, code=3, name="SecretOrg")
    other = KeyedOrg.objects.create(id=3, code=1, name="Other")
    KeyedMember.objects.create(id=1, name="Alice", org=public)
    KeyedMember.objects.create(id=2, name="Bob", org=secret)
    KeyedMember.objects.create(id=3, name="Carol", org=other)
    yield
    KeyedMember.objects.all().delete()
    KeyedOrg.objects.all().delete()


def build(*, hide_orgs=True, hide_members=False):
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    def org_scope(cls, queryset, info):
        return queryset.exclude(name="SecretOrg") if hide_orgs else queryset

    def member_scope(cls, queryset, info):
        return queryset.exclude(name="Bob") if hide_members else queryset

    @orm.type(
        KeyedOrg,
        filters=orm.filter(KeyedOrg),
        order=orm.order(KeyedOrg),
        group=orm.group(KeyedOrg),
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

    @orm.type(KeyedMember, group=orm.group(KeyedMember))
    class MemberOfOrg(relay.Node):
        id: relay.NodeID[int]
        name: auto

        scope_rows = classmethod(member_scope)

    @orm.type(
        KeyedOrg,
        order=orm.order(KeyedOrg, exclude=["members"]),
        group=orm.group(KeyedOrg),
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


def run(schema, query):
    result = schema.execute_sync(query)
    assert result.errors is None, result.errors
    return result.data


def names(connection):
    return [edge["node"]["name"] for edge in connection["edges"]]


class TestToFieldKeys:
    def test_a_member_of_a_hidden_org_reads_no_org(self, orgs):
        data = run(
            build(),
            "{ members(order: [{field: {id: ASC}}]) { edges { node { name org { name } } } } }",
        )
        assert [edge["node"] for edge in data["members"]["edges"]] == [
            {"name": "Alice", "org": {"name": "Public"}},
            {"name": "Bob", "org": None},
            {"name": "Carol", "org": {"name": "Other"}},
        ]

    def test_grouping_by_the_orgs_name_hides_the_hidden_orgs_name(self, orgs):
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

    def test_grouping_orgs_by_their_members_follows_the_key(self, orgs):
        data = run(
            build(hide_orgs=False, hide_members=True),
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

    def test_an_orgs_members_connection_holds_its_own_members(self, orgs):
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

    def test_a_filter_through_the_key_sees_the_hidden_org_as_absent(self, orgs):
        schema = build()
        by_org = (
            '{ members(filter: {object: {org: {field: {name: {exact: "%s"}}}}})'
            " { edges { node { name } } } }"
        )
        assert run(schema, by_org % "SecretOrg") == {"members": {"edges": []}}
        assert run(schema, by_org % "Other") == {
            "members": {"edges": [{"node": {"name": "Carol"}}]}
        }
