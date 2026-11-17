"""A generic foreign key's content type is written no more than its object key.

``object_id`` keys a row of whichever model ``content_type`` names, so setting
the content type points the same key at a row of another model, which no repo
is asked about. Generated inputs leave both out, and a project naming either
is refused.
"""

import pytest
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from strawberry_orm import StrawberryORM
from strawberry_orm.upserts import _declared_fields


class WaRemark(models.Model):
    text = models.CharField(max_length=50)
    kind = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="+")
    key = models.PositiveIntegerField()
    target = GenericForeignKey("kind", "key")

    class Meta:
        app_label = "testapp"
        db_table = "wa_remark"


def fields_of(input_type: type) -> list[str]:
    return list(input_type.__dataclass_fields__)


@pytest.fixture
def orm():
    return StrawberryORM.for_django(warn_missing_scope=False)


def test_generated_inputs_leave_out_a_generic_keys_content_type(orm):
    create = orm.mutations.create_node_input(models=[WaRemark], name="WaRemarkCreate")
    update = orm.mutations.update_node_input(models=[WaRemark], name="WaRemarkUpdate")
    assert fields_of(_declared_fields(create)["waRemark"]) == ["text"]
    assert fields_of(_declared_fields(update)["waRemark"]) == ["text", "id"]
    upsert = orm.mutations.upsert_node_input(
        model=WaRemark,
        project={"_meta": {"create": True, "update": True, "upsert": {"where": True}}},
        name="WaRemarkUpsert",
    )
    assert {
        part: fields_of(_declared_fields(upsert)[part])
        for part in ("where", "create", "update")
    } == {"where": ["id", "text"], "create": ["text"], "update": ["text"]}


@pytest.mark.parametrize(
    "project",
    [
        {"kind": {}},
        {"_meta": {"create": ["kind_id"]}},
        {"_meta": {"update": ["kind"]}},
        {"_meta": {"upsert": {"where": ["kind_id"]}}},
        {"_meta": {"upsert": {"where": ["key"]}}},
    ],
)
def test_a_project_naming_a_generic_keys_columns_is_refused(orm, project):
    meta = {"create": True, "update": True, "upsert": {"where": ["id"]}}
    with pytest.raises(ValueError, match="kind|key"):
        orm.mutations.upsert_node_input(
            model=WaRemark,
            project={**project, "_meta": {**meta, **project.get("_meta", {})}},
            name="WaRemarkByKind",
        )


def test_a_project_naming_the_content_type_link_says_why(orm):
    with pytest.raises(ValueError, match="content type of a generic foreign key"):
        orm.mutations.create_node_input(
            models=[WaRemark], project={"waRemark": {"kind": {}}}, name="WaRemarkKind"
        )


def test_orm_input_leaves_out_a_generic_keys_columns_unless_included(orm):
    assert fields_of(orm.input(WaRemark, name="WaRemarkInput")) == ["text"]
    included = orm.input(WaRemark, include=["key", "kind_id"], name="WaRemarkKeyed")
    assert fields_of(included) == ["kind_id", "key"]
