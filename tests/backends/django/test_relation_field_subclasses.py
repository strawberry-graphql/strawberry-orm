"""A relation declared with a subclass of Django's relation fields is that relation.

django-mptt's ``TreeForeignKey`` and modelcluster's ``ParentalKey`` extend
``ForeignKey``. Told apart by class name, they read as plain columns, and the
raw key could be filtered, grouped and ordered on past the related type's
``scope_rows``. Bob is hidden here.

A ``GenericRelation`` is loaded like any to-many relation, but its rows are
also told apart by content type, so filters do not follow it.
"""

import pytest
import strawberry
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import connection, models

from strawberry_orm import StrawberryORM
from strawberry_orm.mutations import _django_relation_specs
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from strawberry_orm.upserts import _declared_fields


class TreeForeignKey(models.ForeignKey):
    pass


class SubclassedOneToOneField(models.OneToOneField):
    pass


class SubclassedManyToManyField(models.ManyToManyField):
    pass


class Reader(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "test_relsub_reader"


class TreeDoc(models.Model):
    title = models.CharField(max_length=50)
    owner = TreeForeignKey(
        Reader, null=True, on_delete=models.CASCADE, related_name="docs"
    )
    reviewer = SubclassedOneToOneField(
        Reader, null=True, on_delete=models.SET_NULL, related_name="reviewed"
    )
    readers = SubclassedManyToManyField(
        Reader, related_name="read_docs", db_table="test_relsub_treedoc_readers"
    )

    class Meta:
        app_label = "testapp"
        db_table = "test_relsub_treedoc"


class PlainDoc(models.Model):
    title = models.CharField(max_length=50)
    owner = models.ForeignKey(
        Reader, null=True, on_delete=models.CASCADE, related_name="+"
    )
    reviewer = models.OneToOneField(
        Reader, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    readers = models.ManyToManyField(Reader, related_name="+")

    class Meta:
        app_label = "testapp"
        db_table = "test_relsub_plaindoc"


class Remark(models.Model):
    text = models.CharField(max_length=50)
    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, related_name="+"
    )
    object_id = models.PositiveIntegerField()
    target = GenericForeignKey("content_type", "object_id")

    class Meta:
        app_label = "testapp"
        db_table = "test_relsub_remark"


class Dossier(models.Model):
    title = models.CharField(max_length=50)
    remarks = GenericRelation(Remark)

    class Meta:
        app_label = "testapp"
        db_table = "test_relsub_dossier"


@pytest.fixture
def docs(setup_tables):
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in (Reader, TreeDoc, Remark, Dossier):
            if model._meta.db_table not in existing:
                editor.create_model(model)
    ContentType.objects.clear_cache()
    alice = Reader.objects.create(id=1, name="Alice")
    bob = Reader.objects.create(id=2, name="Bob")
    TreeDoc.objects.create(id=1, title="alice doc", owner=alice, reviewer=bob)
    bobs = TreeDoc.objects.create(id=2, title="bob doc", owner=bob, reviewer=alice)
    TreeDoc.objects.create(id=3, title="orphan doc")
    bobs.readers.add(alice, bob)
    one = Dossier.objects.create(id=1, title="one")
    two = Dossier.objects.create(id=2, title="two")
    Remark.objects.create(id=1, text="public remark", target=one)
    Remark.objects.create(id=2, text="secret remark", target=two)
    yield
    for model in (TreeDoc.readers.through, TreeDoc, Remark, Dossier, Reader):
        model.objects.all().delete()


def build():
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    @orm.type(Reader, filters=orm.filter(Reader), group=orm.group(Reader))
    class ReaderType:
        name: auto

        @classmethod
        def scope_rows(cls, queryset, info):
            return queryset.exclude(name="Bob")

    @orm.type(
        TreeDoc,
        filters=orm.filter(TreeDoc),
        order=orm.order(TreeDoc),
        group=orm.group(TreeDoc),
    )
    class DocType:
        title: auto
        owner: ReaderType | None
        reviewer: ReaderType | None
        readers: list[ReaderType]

    @orm.type(Remark)
    class RemarkType:
        text: auto

        @classmethod
        def scope_rows(cls, queryset, info):
            return queryset.exclude(text__startswith="secret")

    @orm.type(Dossier, filters=orm.filter(Dossier))
    class DossierType:
        title: auto
        remarks: list[RemarkType]

    @strawberry.type
    class Query:
        docs: list[DocType] = orm.field.eager()
        doc_connection: ORMListConnection[DocType] = orm.connection.eager()
        dossiers: list[DossierType] = orm.field.eager()

    return orm.schema(query=Query)


def run(schema, query):
    result = schema.execute_sync(query)
    assert result.errors is None, result.errors
    return result.data


def refused(schema, query):
    result = schema.execute_sync(query)
    assert result.data is None
    return [error.message for error in result.errors]


def input_fields(schema, name):
    data = run(schema, f'{{ __type(name: "{name}") {{ inputFields {{ name }} }} }}')
    return [field["name"] for field in data["__type"]["inputFields"]]


class TestRelationFieldSubclasses:
    def test_a_subclassed_relation_is_introspected_as_the_field_it_extends(self):
        backend = StrawberryORM.for_django().backend
        assert backend._introspect_model(TreeDoc) == backend._introspect_model(PlainDoc)

    def test_a_subclassed_relation_is_written_as_the_field_it_extends(self):
        row_keys = StrawberryORM.for_django().backend._row_keys

        def specs(model):
            return {
                name: (spec.relation_mode, spec.fk_column, spec.remote_attr)
                for name, spec in _django_relation_specs(model, row_keys).items()
            }

        assert (
            specs(TreeDoc)
            == specs(PlainDoc)
            == {
                "owner": ("forward_fk", "owner_id", None),
                "reviewer": ("forward_fk", "reviewer_id", None),
                "readers": ("many_to_many", None, None),
            }
        )
        assert specs(Reader) == {
            "docs": ("reverse_fk", None, "owner"),
            "read_docs": ("many_to_many", None, None),
        }

    def test_a_subclassed_key_is_filtered_through_the_related_type(self, docs):
        schema = build()
        assert "owner" not in input_fields(schema, "TreeDocField")
        by_owner = '{ docs(filter: {object: {owner: {field: {name: {exact: "%s"}}}}}) { title } }'
        assert run(schema, by_owner % "Bob") == {"docs": []}
        assert run(schema, by_owner % "Alice") == {"docs": [{"title": "alice doc"}]}
        by_reviewer = by_owner.replace("owner", "reviewer")
        assert run(schema, by_reviewer % "Bob") == {"docs": []}
        assert run(schema, by_reviewer % "Alice") == {"docs": [{"title": "bob doc"}]}
        assert refused(
            schema, '{ docs(filter: {field: {owner: {exact: "2"}}}) { title } }'
        ) == [
            "Expected value of type 'TreeDocField' not to include unknown field 'owner',"
            ' found: { owner: { exact: "2" } }.'
        ]

    def test_a_subclassed_key_is_not_grouped_or_ordered_as_a_column(self, docs):
        schema = build()
        assert refused(
            schema,
            "{ docConnection(groupBy: [{field: {owner: true}}]) { groups { aggregates { count } } } }",
        ) == [
            "Expected value of type 'TreeDocGroupByField' not to include unknown field"
            " 'owner', found: { owner: true }."
        ]
        assert refused(
            schema,
            "{ docConnection(order: [{field: {owner: DESC}}]) { edges { node { title } } } }",
        ) == [
            "Expected value of type 'TreeDocOrderField' not to include unknown field"
            " 'owner', found: { owner: DESC }."
        ]
        data = run(
            schema,
            """{ docConnection(groupBy: [{object: {owner: {field: {name: true}}}}]) {
                groups { key { owner { name } } aggregates { count } } } }""",
        )
        assert data == {
            "docConnection": {
                "groups": [
                    {"key": {"owner": {"name": None}}, "aggregates": {"count": 2}},
                    {"key": {"owner": {"name": "Alice"}}, "aggregates": {"count": 1}},
                ]
            }
        }

    def test_a_subclassed_many_to_many_is_read_and_filtered_through_the_scope(
        self, docs
    ):
        schema = build()
        assert run(schema, "{ docs { title owner { name } readers { name } } }") == {
            "docs": [
                {"title": "alice doc", "owner": {"name": "Alice"}, "readers": []},
                {"title": "bob doc", "owner": None, "readers": [{"name": "Alice"}]},
                {"title": "orphan doc", "owner": None, "readers": []},
            ]
        }
        by_reader = '{ docs(filter: {object: {readers: {field: {name: {exact: "%s"}}}}}) { title } }'
        assert run(schema, by_reader % "Bob") == {"docs": []}
        assert run(schema, by_reader % "Alice") == {"docs": [{"title": "bob doc"}]}

    def test_a_generic_relation_is_read_scoped_and_not_filtered_on(self, docs):
        schema = build()
        assert run(schema, "{ dossiers { title remarks { text } } }") == {
            "dossiers": [
                {"title": "one", "remarks": [{"text": "public remark"}]},
                {"title": "two", "remarks": []},
            ]
        }
        assert input_fields(schema, "DossierField") == ["title"]
        assert "object" not in input_fields(schema, "DossierFilter")

    def test_a_generic_keys_column_is_not_written_or_matched_on(self):
        """``object_id`` would point the remark at any row of its content type."""
        orm = StrawberryORM.for_django(warn_missing_scope=False)
        meta = {"create": True, "update": True}
        upsert = orm.mutations.upsert_node_input(
            model=Remark,
            project={
                "_meta": {**meta, "upsert": {"where": ["id"], "onMatch": "UPDATE"}}
            },
            name="RemarkUpsert",
        )
        for operation in ("create", "update"):
            assert list(_declared_fields(upsert)[operation].__dataclass_fields__) == [
                "text"
            ]
        with pytest.raises(ValueError, match="object_id"):
            orm.mutations.upsert_node_input(
                model=Remark,
                project={"_meta": {**meta, "upsert": {"where": ["object_id"]}}},
                name="RemarkByTarget",
            )
