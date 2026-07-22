"""Tests for mutation projection _meta ops, field allowlists, and upsert."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import strawberry
from sqlalchemy import select
from strawberry import relay
from strawberry.types.cast import cast as strawberry_cast

from strawberry_orm import StrawberryORM
from strawberry_orm.mutations import OpFields, RelationPolicy
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.models import Comment as SAComment
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import Tag as SATag
from tests.backends.sqlalchemy.models import User as SAUser

_orm = StrawberryORM.for_sqlalchemy(
    dialect="sqlite",
    session_getter=lambda info: info.context["session"],
)


@_orm.type(SAUser)
class UserNode(relay.Node):
    id: relay.NodeID[int]
    name: auto
    email: auto


@_orm.type(SATag)
class TagNode(relay.Node):
    id: relay.NodeID[int]
    name: auto


@_orm.type(SAComment)
class CommentNode(relay.Node):
    id: relay.NodeID[int]
    body: auto


@_orm.type(SAPost)
class PostNode(relay.Node):
    id: relay.NodeID[int]
    title: auto
    body: auto

    @strawberry.field
    def author(self) -> UserNode:
        return strawberry_cast(UserNode, self.author)

    @strawberry.field
    def tags(self) -> list[TagNode]:
        return [strawberry_cast(TagNode, tag) for tag in self.tags]

    @strawberry.field
    def comments(self) -> list[CommentNode]:
        return [
            strawberry_cast(CommentNode, comment)
            for comment in sorted(self.comments, key=lambda c: c.id)
        ]


@pytest.fixture
def orm():
    return _orm


def _field_names(type_: type) -> set[str]:
    return {f.name for f in type_.__strawberry_definition__.fields}


def _make_schema(orm, *, project, models, input_prefix: str, types):
    @strawberry.type
    class Mutation:
        create_node = orm.mutations.create_node(
            models=models,
            project=project,
            input_name=f"{input_prefix}CreateNodeInput",
        )
        update_node = orm.mutations.update_node(
            models=models,
            project=project,
            input_name=f"{input_prefix}UpdateNodeInput",
        )

    @strawberry.type
    class Query:
        @strawberry.field
        def ok(self) -> bool:
            return True

    return strawberry.Schema(query=Query, mutation=Mutation, types=types)


def _execute(schema, sa_session, query: str):
    result = schema.execute_sync(query, context_value={"session": sa_session})
    assert result.errors is None, result.errors
    return result.data


def _execute_expecting_error(schema, sa_session, query: str) -> str:
    result = schema.execute_sync(query, context_value={"session": sa_session})
    assert result.errors is not None
    return str(result.errors[0])


# ---------------------------------------------------------------------------
# Normalize
# ---------------------------------------------------------------------------


class TestNormalizeMetaOps:
    def test_default_policy_when_only_on_replace(self, orm):
        project = orm.mutations._normalize_model_project(
            SAUser, {"_meta": {"onReplace": "DISCONNECT"}}
        )
        policy: RelationPolicy = project["_meta"]
        assert policy.explicit_ops is False
        assert policy.on_replace_options == ("DISCONNECT",)
        assert policy.create is None
        assert policy.update is None
        assert policy.upsert_where is None
        assert policy.unlink is False
        assert policy.delete is False

    def test_empty_project_is_not_explicit(self, orm):
        project = orm.mutations._normalize_model_project(SAUser, {})
        policy: RelationPolicy = project["_meta"]
        assert policy.explicit_ops is False
        assert project["relations"] == {}

    def test_create_true_means_all_scalars(self, orm):
        project = orm.mutations._normalize_model_project(
            SAUser, {"_meta": {"create": True}}
        )
        assert project["_meta"].create == OpFields(fields=None)
        assert project["_meta"].explicit_ops is True

    def test_explicit_ops_and_upsert_where(self, orm):
        project = orm.mutations._normalize_model_project(
            SAUser,
            {
                "_meta": {
                    "create": ["email", "name"],
                    "update": ["name"],
                    "upsert": {"where": ["email"]},
                    "onReplace": ["DISCONNECT", "DELETE"],
                }
            },
        )
        policy: RelationPolicy = project["_meta"]
        assert policy.explicit_ops is True
        assert policy.create == OpFields(fields=("email", "name"))
        assert policy.update == OpFields(fields=("name",))
        assert policy.upsert_where == ("email",)

    def test_upsert_where_string_normalizes_to_tuple(self, orm):
        project = orm.mutations._normalize_model_project(
            SATag, {"_meta": {"upsert": {"where": "name"}}}
        )
        assert project["_meta"].upsert_where == ("name",)

    def test_upsert_where_dedupes(self, orm):
        project = orm.mutations._normalize_model_project(
            SAUser, {"_meta": {"upsert": {"where": ["email", "email", "name"]}}}
        )
        assert project["_meta"].upsert_where == ("email", "name")

    def test_upsert_where_allows_id(self, orm):
        project = orm.mutations._normalize_model_project(
            SAUser, {"_meta": {"upsert": {"where": ["id"]}}}
        )
        assert project["_meta"].upsert_where == ("id",)

    def test_upsert_only(self, orm):
        project = orm.mutations._normalize_model_project(
            SATag, {"_meta": {"upsert": {"where": "name"}, "unlink": True}}
        )
        policy: RelationPolicy = project["_meta"]
        assert policy.explicit_ops is True
        assert policy.create is None
        assert policy.update is None
        assert policy.upsert_where == ("name",)
        assert policy.unlink is True

    def test_rejects_unknown_field_in_create(self, orm):
        with pytest.raises(ValueError, match="Unknown field"):
            orm.mutations._normalize_model_project(
                SAUser, {"_meta": {"create": ["not_a_field"]}}
            )

    def test_rejects_unknown_field_in_update(self, orm):
        with pytest.raises(ValueError, match="Unknown field"):
            orm.mutations._normalize_model_project(
                SAUser, {"_meta": {"update": ["nope"]}}
            )

    def test_rejects_unknown_field_in_where(self, orm):
        with pytest.raises(ValueError, match="Unknown field"):
            orm.mutations._normalize_model_project(
                SAUser, {"_meta": {"upsert": {"where": ["missing"]}}}
            )

    def test_rejects_upsert_without_where(self, orm):
        with pytest.raises(ValueError, match="requires a non-empty 'where'"):
            orm.mutations._normalize_model_project(SAUser, {"_meta": {"upsert": {}}})

    def test_rejects_empty_where_list(self, orm):
        with pytest.raises(ValueError, match="cannot be empty"):
            orm.mutations._normalize_model_project(
                SAUser, {"_meta": {"upsert": {"where": []}}}
            )

    def test_rejects_upsert_not_dict(self, orm):
        with pytest.raises(ValueError, match="must be a dict"):
            orm.mutations._normalize_model_project(
                SAUser, {"_meta": {"upsert": ["email"]}}
            )

    def test_rejects_unknown_upsert_keys(self, orm):
        with pytest.raises(ValueError, match="Unknown _meta.upsert key"):
            orm.mutations._normalize_model_project(
                SAUser,
                {"_meta": {"upsert": {"where": ["email"], "create": ["name"]}}},
            )

    def test_rejects_fields_wrapper(self, orm):
        with pytest.raises(ValueError, match="must be True or a list"):
            orm.mutations._normalize_model_project(
                SAUser, {"_meta": {"create": {"fields": ["name"]}}}
            )

    def test_rejects_empty_dict_op_config(self, orm):
        with pytest.raises(ValueError, match="must be True or a list"):
            orm.mutations._normalize_model_project(SAUser, {"_meta": {"create": {}}})

    def test_unlink_must_be_true(self, orm):
        with pytest.raises(ValueError, match="must be True"):
            orm.mutations._normalize_model_project(SATag, {"_meta": {"unlink": False}})

    def test_delete_must_be_true(self, orm):
        with pytest.raises(ValueError, match="must be True"):
            orm.mutations._normalize_model_project(SATag, {"_meta": {"delete": "yes"}})

    def test_rejects_unknown_meta_key(self, orm):
        with pytest.raises(ValueError, match="Unknown _meta key"):
            orm.mutations._normalize_model_project(
                SAUser, {"_meta": {"lookup": ["email"]}}
            )

    def test_unlink_on_singular_raises_at_input_build(self, orm):
        post_project = orm.mutations._normalize_model_project(
            SAPost,
            {"author": {"_meta": {"unlink": True}}},
        )
        spec = orm.mutations._relation_specs(SAPost)["author"]
        with pytest.raises(ValueError, match="unlink/delete are only valid"):
            orm.mutations._single_relation_input(
                SAPost, spec, post_project["relations"]["author"]
            )

    def test_enabled_ops_defaults(self, orm):
        policy = RelationPolicy()
        assert orm.mutations._enabled_ops(policy, kind="single") == {
            "create",
            "update",
        }
        assert orm.mutations._enabled_ops(policy, kind="many") == {
            "create",
            "update",
            "unlink",
            "delete",
        }


# ---------------------------------------------------------------------------
# Schema / codegen shapes
# ---------------------------------------------------------------------------


class TestDefaultSchemaUnchanged:
    def test_default_singular_has_create_update_and_on_replace(self, orm):
        post_project = orm.mutations._normalize_model_project(
            SAPost, {"author": {"_meta": {"onReplace": ["DISCONNECT", "DELETE"]}}}
        )
        spec = orm.mutations._relation_specs(SAPost)["author"]
        wrapper = orm.mutations._single_relation_input(
            SAPost, spec, post_project["relations"]["author"]
        )
        names = _field_names(wrapper)
        assert "create" in names
        assert "update" in names
        assert "upsert" not in names
        assert "on_replace" in names or "onReplace" in names

    def test_default_list_has_create_update_unlink_delete(self, orm):
        post_project = orm.mutations._normalize_model_project(SAPost, {"tags": {}})
        spec = orm.mutations._relation_specs(SAPost)["tags"]
        ref = orm.mutations._list_relation_ref_type(
            SAPost, spec, post_project["relations"]["tags"]
        )
        assert _field_names(ref) == {"create", "update", "unlink", "delete"}


class TestOpFieldInputs:
    def test_create_input_respects_allowlist(self, orm):
        project = orm.mutations._normalize_model_project(
            SAUser, {"_meta": {"create": ["name"]}}
        )
        create_input = orm.mutations._create_input(SAUser, project)
        names = _field_names(create_input)
        assert "name" in names
        assert "email" not in names

    def test_update_input_respects_allowlist(self, orm):
        project = orm.mutations._normalize_model_project(
            SAUser, {"_meta": {"update": ["email"]}}
        )
        update_input = orm.mutations._update_input(SAUser, project)
        names = _field_names(update_input)
        assert "id" in names
        assert "email" in names
        assert "name" not in names

    def test_create_and_update_allowlists_differ(self, orm):
        project = orm.mutations._normalize_model_project(
            SAUser,
            {"_meta": {"create": ["email", "name"], "update": ["name"]}},
        )
        create_names = _field_names(orm.mutations._create_input(SAUser, project))
        update_names = _field_names(orm.mutations._update_input(SAUser, project))
        assert "email" in create_names
        assert "email" not in update_names
        assert "name" in create_names
        assert "name" in update_names
        assert "id" in update_names
        assert "id" not in create_names

    def test_singular_update_only_omits_on_replace(self, orm):
        post_project = orm.mutations._normalize_model_project(
            SAPost, {"author": {"_meta": {"update": ["name"]}}}
        )
        spec = orm.mutations._relation_specs(SAPost)["author"]
        wrapper = orm.mutations._single_relation_input(
            SAPost, spec, post_project["relations"]["author"]
        )
        assert _field_names(wrapper) == {"update"}

    def test_singular_create_only(self, orm):
        post_project = orm.mutations._normalize_model_project(
            SAPost, {"author": {"_meta": {"create": True, "onReplace": "DISCONNECT"}}}
        )
        spec = orm.mutations._relation_specs(SAPost)["author"]
        wrapper = orm.mutations._single_relation_input(
            SAPost, spec, post_project["relations"]["author"]
        )
        names = _field_names(wrapper)
        assert names == {"create"}
        assert "on_replace" not in names  # single string fixes policy, no field

    def test_singular_with_create_exposes_on_replace_choice(self, orm):
        post_project = orm.mutations._normalize_model_project(
            SAPost,
            {
                "author": {
                    "_meta": {
                        "create": True,
                        "upsert": {"where": ["email"]},
                        "onReplace": ["DISCONNECT", "DELETE"],
                    }
                },
            },
        )
        spec = orm.mutations._relation_specs(SAPost)["author"]
        wrapper = orm.mutations._single_relation_input(
            SAPost, spec, post_project["relations"]["author"]
        )
        names = _field_names(wrapper)
        assert "create" in names
        assert "upsert" in names
        assert "update" not in names
        assert "on_replace" in names or "onReplace" in names

    def test_list_upsert_only_ref(self, orm):
        post_project = orm.mutations._normalize_model_project(
            SAPost,
            {"tags": {"_meta": {"upsert": {"where": ["name"]}, "unlink": True}}},
        )
        spec = orm.mutations._relation_specs(SAPost)["tags"]
        ref = orm.mutations._list_relation_ref_type(
            SAPost, spec, post_project["relations"]["tags"]
        )
        assert _field_names(ref) == {"upsert", "unlink"}

    def test_list_create_only_omits_update_unlink_delete(self, orm):
        post_project = orm.mutations._normalize_model_project(
            SAPost, {"tags": {"_meta": {"create": True}}}
        )
        spec = orm.mutations._relation_specs(SAPost)["tags"]
        ref = orm.mutations._list_relation_ref_type(
            SAPost, spec, post_project["relations"]["tags"]
        )
        assert _field_names(ref) == {"create"}

    def test_upsert_where_input_fields(self, orm):
        project = orm.mutations._normalize_model_project(
            SAUser, {"_meta": {"upsert": {"where": ["email", "name"]}}}
        )
        where_input = orm.mutations._where_input(SAUser, project)
        assert _field_names(where_input) == {"email", "name"}

    def test_upsert_update_sub_input_omits_id(self, orm):
        project = orm.mutations._normalize_model_project(
            SAUser,
            {"_meta": {"update": ["name"], "upsert": {"where": ["email"]}}},
        )
        upsert_input = orm.mutations._upsert_input(SAUser, project)
        update_field = next(
            f
            for f in upsert_input.__strawberry_definition__.fields
            if f.name == "update"
        )
        update_type = update_field.type.of_type  # Optional unwrap may vary
        # Walk to concrete input type
        while hasattr(update_type, "of_type"):
            update_type = update_type.of_type
        assert "id" not in _field_names(update_type)
        assert "name" in _field_names(update_type)

    def test_upsert_reuses_create_allowlist(self, orm):
        project = orm.mutations._normalize_model_project(
            SAUser,
            {
                "_meta": {
                    "create": ["email", "name"],
                    "upsert": {"where": ["email"]},
                }
            },
        )
        create_type = orm.mutations._create_input(SAUser, project)
        upsert_input = orm.mutations._upsert_input(SAUser, project)
        create_field = next(
            f
            for f in upsert_input.__strawberry_definition__.fields
            if f.name == "create"
        )
        nested = create_field.type
        while hasattr(nested, "of_type"):
            nested = nested.of_type
        assert _field_names(nested) == _field_names(create_type)
        assert "email" in _field_names(nested)
        assert "email" in _field_names(create_type)

    def test_upsert_only_defaults_create_update_to_all_scalars(self, orm):
        project = orm.mutations._normalize_model_project(
            SATag, {"_meta": {"upsert": {"where": ["name"]}}}
        )
        create_names = _field_names(orm.mutations._create_input(SATag, project))
        update_names = _field_names(
            orm.mutations._update_input(SATag, project, include_id=False)
        )
        assert "name" in create_names
        assert "name" in update_names
        assert "id" not in update_names


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class TestUpsertRuntime:
    def test_upsert_author_miss_creates_and_hit_updates(self, orm, sa_session, seed):
        project = {
            "post": {
                "author": {
                    "_meta": {
                        "create": ["email", "name"],
                        "update": ["name"],
                        "upsert": {"where": ["email"]},
                        "onReplace": "DISCONNECT",
                    }
                },
            }
        }
        schema = _make_schema(
            orm,
            project=project,
            models=[SAPost],
            input_prefix="AuthorUpsert",
            types=[UserNode, PostNode],
        )

        create_data = _execute(
            schema,
            sa_session,
            """
            mutation {
              createNode(input: {
                post: {
                  title: "Upsert Post"
                  body: "Body"
                  author: {
                    upsert: {
                      where: { email: "upsert@example.com" }
                      create: { email: "upsert@example.com", name: "Upsert User" }
                      update: { name: "Should Not Apply" }
                    }
                  }
                }
              }) {
                ... on PostNode {
                  title
                  author { name email }
                }
              }
            }
            """,
        )
        assert create_data["createNode"]["author"] == {
            "name": "Upsert User",
            "email": "upsert@example.com",
        }

        post_id = (
            sa_session.execute(select(SAPost).where(SAPost.title == "Upsert Post"))
            .scalar_one()
            .id
        )
        update_data = _execute(
            schema,
            sa_session,
            f"""
            mutation {{
              updateNode(input: {{
                post: {{
                  id: "{post_id}"
                  author: {{
                    upsert: {{
                      where: {{ email: "upsert@example.com" }}
                      create: {{ email: "upsert@example.com", name: "Ignored On Hit" }}
                      update: {{ name: "Upserted Again" }}
                    }}
                  }}
                }}
              }}) {{
                ... on PostNode {{
                  author {{ name email }}
                }}
              }}
            }}
            """,
        )
        assert update_data["updateNode"]["author"] == {
            "name": "Upserted Again",
            "email": "upsert@example.com",
        }
        assert (
            len(
                sa_session.execute(
                    select(SAUser).where(SAUser.email == "upsert@example.com")
                )
                .scalars()
                .all()
            )
            == 1
        )

    def test_miss_merges_where_into_create_and_create_wins_overlap(
        self, orm, sa_session, seed
    ):
        project = {
            "post": {
                "author": {
                    "_meta": {
                        "create": ["email", "name"],
                        "upsert": {"where": ["email"]},
                        "onReplace": "DISCONNECT",
                    }
                },
            }
        }
        schema = _make_schema(
            orm,
            project=project,
            models=[SAPost],
            input_prefix="MergeWhere",
            types=[UserNode, PostNode],
        )
        data = _execute(
            schema,
            sa_session,
            """
            mutation {
              createNode(input: {
                post: {
                  title: "Merge Post"
                  body: "Body"
                  author: {
                    upsert: {
                      where: { email: "merge@example.com" }
                      create: { email: "merge-override@example.com", name: "Merged" }
                    }
                  }
                }
              }) {
                ... on PostNode { author { name email } }
              }
            }
            """,
        )
        # create wins on overlap for email
        assert data["createNode"]["author"] == {
            "name": "Merged",
            "email": "merge-override@example.com",
        }

    def test_miss_uses_where_when_create_omitted(self, orm, sa_session, seed):
        project = {
            "post": {
                "tags": {"_meta": {"upsert": {"where": ["name"]}}},
            }
        }
        schema = _make_schema(
            orm,
            project=project,
            models=[SAPost],
            input_prefix="WhereOnlyCreate",
            types=[TagNode, PostNode],
        )
        data = _execute(
            schema,
            sa_session,
            """
            mutation {
              updateNode(input: {
                post: {
                  id: "1"
                  tags: [{ upsert: { where: { name: "from-where-only" } } }]
                }
              }) {
                ... on PostNode { tags { name } }
              }
            }
            """,
        )
        names = {t["name"] for t in data["updateNode"]["tags"]}
        assert "from-where-only" in names
        assert (
            len(
                sa_session.execute(select(SATag).where(SATag.name == "from-where-only"))
                .scalars()
                .all()
            )
            == 1
        )

    def test_hit_with_empty_update_links_without_changing_fields(
        self, orm, sa_session, seed
    ):
        project = {
            "post": {
                "tags": {"_meta": {"upsert": {"where": ["name"]}}},
            }
        }
        schema = _make_schema(
            orm,
            project=project,
            models=[SAPost],
            input_prefix="EmptyUpdateLink",
            types=[TagNode, PostNode],
        )
        before = sa_session.get(SATag, 1)
        assert before is not None
        assert before.name == "python"
        data = _execute(
            schema,
            sa_session,
            """
            mutation {
              updateNode(input: {
                post: {
                  id: "3"
                  tags: [{ upsert: { where: { name: "python" }, update: {} } }]
                }
              }) {
                ... on PostNode { tags { name } }
              }
            }
            """,
        )
        names = {t["name"] for t in data["updateNode"]["tags"]}
        assert "python" in names
        sa_session.refresh(before)
        assert before.name == "python"

    def test_upsert_tag_by_name_create_and_link(self, orm, sa_session, seed):
        project = {
            "post": {
                "tags": {
                    "_meta": {
                        "upsert": {"where": ["name"]},
                        "unlink": True,
                    }
                },
            }
        }
        schema = _make_schema(
            orm,
            project=project,
            models=[SAPost],
            input_prefix="TagUpsert",
            types=[TagNode, PostNode],
        )
        data = _execute(
            schema,
            sa_session,
            """
            mutation {
              updateNode(input: {
                post: {
                  id: "1"
                  tags: [
                    { upsert: { where: { name: "python" }, update: {} } }
                    { upsert: { where: { name: "new-tag" }, create: { name: "new-tag" } } }
                  ]
                }
              }) {
                ... on PostNode { tags { name } }
              }
            }
            """,
        )
        names = {t["name"] for t in data["updateNode"]["tags"]}
        assert "python" in names
        assert "new-tag" in names

    def test_upsert_where_by_id(self, orm, sa_session, seed):
        project = {
            "post": {
                "author": {
                    "_meta": {
                        "update": ["name"],
                        "upsert": {"where": ["id"]},
                        "onReplace": "DISCONNECT",
                    }
                },
            }
        }
        schema = _make_schema(
            orm,
            project=project,
            models=[SAPost],
            input_prefix="WhereById",
            types=[UserNode, PostNode],
        )
        data = _execute(
            schema,
            sa_session,
            """
            mutation {
              updateNode(input: {
                post: {
                  id: "1"
                  author: {
                    upsert: {
                      where: { id: "2" }
                      update: { name: "Bob Via Upsert Id" }
                    }
                  }
                }
              }) {
                ... on PostNode { author { id name } }
              }
            }
            """,
        )
        author = data["updateNode"]["author"]
        assert author["name"] == "Bob Via Upsert Id"
        # Relay global id may encode type; compare decoded/raw via DB
        bob = sa_session.get(SAUser, 2)
        assert bob is not None
        assert bob.name == "Bob Via Upsert Id"

    def test_ambiguous_where_errors(self, orm, sa_session, seed):
        # Two users with same name after we duplicate
        sa_session.add(SAUser(id=90, name="Alice", email="alice2@example.com"))
        sa_session.flush()

        project = orm.mutations._normalize_model_project(
            SAUser, {"_meta": {"upsert": {"where": ["name"]}, "update": ["email"]}}
        )
        upsert_type = orm.mutations._upsert_input(SAUser, project)
        where_type = orm.mutations._where_input(SAUser, project)
        update_type = orm.mutations._update_input(SAUser, project, include_id=False)

        payload = upsert_type(
            where=where_type(name="Alice"),
            update=update_type(email="should-not-write@example.com"),
        )
        info = SimpleNamespace(context={"session": sa_session})
        with pytest.raises(ValueError, match="matched 2 rows"):
            orm.mutations._upsert_sync(SAUser, payload, info, project=project)

    def test_graphql_rejects_omitted_create_branch(self, orm, sa_session, seed):
        project = {
            "post": {
                "author": {"_meta": {"update": ["name"], "onReplace": "DISCONNECT"}},
            }
        }
        schema = _make_schema(
            orm,
            project=project,
            models=[SAPost],
            input_prefix="NoCreateBranch",
            types=[UserNode, PostNode],
        )
        err = _execute_expecting_error(
            schema,
            sa_session,
            """
            mutation {
              updateNode(input: {
                post: {
                  id: "1"
                  author: { create: { name: "X", email: "x@example.com" } }
                }
              }) { ... on PostNode { id } }
            }
            """,
        )
        assert "create" in err.lower() or "Field" in err

    def test_graphql_rejects_disallowed_scalar_on_create(self, orm, sa_session, seed):
        project = {
            "post": {
                "author": {
                    "_meta": {
                        "create": ["name"],
                        "onReplace": "DISCONNECT",
                    }
                },
            }
        }
        schema = _make_schema(
            orm,
            project=project,
            models=[SAPost],
            input_prefix="NoEmailOnCreate",
            types=[UserNode, PostNode],
        )
        err = _execute_expecting_error(
            schema,
            sa_session,
            """
            mutation {
              createNode(input: {
                post: {
                  title: "T"
                  body: "B"
                  author: {
                    create: { name: "X", email: "x@example.com" }
                  }
                }
              }) { ... on PostNode { id } }
            }
            """,
        )
        assert "email" in err.lower() or "Field" in err

    def test_bare_create_and_update_still_work_with_upsert_enabled(
        self, orm, sa_session, seed
    ):
        project = {
            "post": {
                "author": {
                    "_meta": {
                        "create": ["email", "name"],
                        "update": ["name"],
                        "upsert": {"where": ["email"]},
                        "onReplace": "DISCONNECT",
                    }
                },
            }
        }
        schema = _make_schema(
            orm,
            project=project,
            models=[SAPost],
            input_prefix="BareOps",
            types=[UserNode, PostNode],
        )
        created = _execute(
            schema,
            sa_session,
            """
            mutation {
              createNode(input: {
                post: {
                  title: "Bare Create"
                  body: "Body"
                  author: {
                    create: { email: "bare@example.com", name: "Bare" }
                  }
                }
              }) {
                ... on PostNode { author { name email } }
              }
            }
            """,
        )
        assert created["createNode"]["author"]["email"] == "bare@example.com"

        post_id = (
            sa_session.execute(select(SAPost).where(SAPost.title == "Bare Create"))
            .scalar_one()
            .id
        )
        updated = _execute(
            schema,
            sa_session,
            f"""
            mutation {{
              updateNode(input: {{
                post: {{
                  id: "{post_id}"
                  author: {{
                    update: {{ id: "1", name: "Alice Renamed Via Bare Update" }}
                  }}
                }}
              }}) {{
                ... on PostNode {{ author {{ id name }} }}
              }}
            }}
            """,
        )
        assert (
            updated["updateNode"]["author"]["name"] == "Alice Renamed Via Bare Update"
        )

    def test_upsert_on_replace_delete_removes_previous_author(
        self, orm, sa_session, seed
    ):
        disposable = SAUser(
            id=91, name="Disposable Author", email="disposable-author@example.com"
        )
        sa_session.add(disposable)
        sa_session.flush()
        post = sa_session.get(SAPost, 1)
        assert post is not None
        post.author_id = disposable.id
        sa_session.flush()

        project = {
            "post": {
                "author": {
                    "_meta": {
                        "create": ["email", "name"],
                        "upsert": {"where": ["email"]},
                        "onReplace": "DELETE",
                    }
                },
            }
        }
        schema = _make_schema(
            orm,
            project=project,
            models=[SAPost],
            input_prefix="OnReplaceUpsert",
            types=[UserNode, PostNode],
        )
        data = _execute(
            schema,
            sa_session,
            """
            mutation {
              updateNode(input: {
                post: {
                  id: "1"
                  author: {
                    upsert: {
                      where: { email: "replacement@example.com" }
                      create: {
                        email: "replacement@example.com"
                        name: "Replacement"
                      }
                    }
                  }
                }
              }) {
                ... on PostNode { author { name email } }
              }
            }
            """,
        )
        assert data["updateNode"]["author"]["email"] == "replacement@example.com"
        assert sa_session.get(SAUser, disposable.id) is None

    def test_list_upsert_with_unlink(self, orm, sa_session, seed):
        project = {
            "post": {
                "tags": {
                    "_meta": {
                        "upsert": {"where": ["name"]},
                        "unlink": True,
                    }
                },
            }
        }
        schema = _make_schema(
            orm,
            project=project,
            models=[SAPost],
            input_prefix="UpsertUnlink",
            types=[TagNode, PostNode],
        )
        # post 2 starts with python + graphql
        data = _execute(
            schema,
            sa_session,
            """
            mutation {
              updateNode(input: {
                post: {
                  id: "2"
                  tags: [
                    { unlink: { id: "1" } }
                    { upsert: { where: { name: "rust" }, update: {} } }
                  ]
                }
              }) {
                ... on PostNode { tags { name } }
              }
            }
            """,
        )
        names = {t["name"] for t in data["updateNode"]["tags"]}
        assert "python" not in names
        assert "rust" in names
        assert "graphql" in names  # untouched existing link remains
