"""The schema an insert-only projection generates, and the configuration it accepts.

Every backend generates the same SDL, so the snapshots in ``snapshots/`` are
shared; run with ``UPDATE_SNAPSHOTS=1`` to rewrite them after an intended
change. Each backend provides an ``orm`` fixture and a ``models`` fixture with
``User``, ``Post``, ``Person``, ``Address`` and ``Event``.
"""

import os
import re
from pathlib import Path
from typing import Any

import pytest
import strawberry

from strawberry_orm.mutations import RelationPolicy

SNAPSHOTS = Path(__file__).parent / "snapshots"


def _sdl(*inputs: type) -> str:
    def field_for(input_type: type) -> Any:
        def resolver(input: Any) -> bool:
            return True

        resolver.__annotations__ = {"input": input_type, "return": bool}
        return strawberry.field(resolver=resolver)

    namespace = {f"take{index}": field_for(t) for index, t in enumerate(inputs)}
    mutation = strawberry.type(type("Mutation", (), namespace))
    query = strawberry.type(
        type("Query", (), {"__annotations__": {"ok": bool}, "ok": True})
    )
    return strawberry.Schema(query=query, mutation=mutation).as_str() + "\n"


def _assert_snapshot(name: str, sdl: str) -> None:
    path = SNAPSHOTS / f"{name}.graphql"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.write_text(sdl)
    assert sdl == path.read_text()


def _without_descriptions(sdl: str) -> str:
    sdl = re.sub(r'\s*"""[\s\S]*?"""', "", sdl)
    return re.sub(r"\n\s*\n(\s+\w)", r"\n\1", sdl)


def _posts(name: str, **upsert: Any) -> dict[str, Any]:
    return {
        "_meta": {
            "name": name,
            "create": ["title", "body"],
            "upsert": {"where": ["title"], **upsert},
        }
    }


def _user(name: str, posts: dict[str, Any], **meta: Any) -> dict[str, Any]:
    return {
        "_meta": {
            "name": name,
            "create": ["name", "email"],
            "upsert": {"where": ["email"]},
            **meta,
        },
        "posts": posts,
    }


# -- Snapshots -----------------------------------------------------------------


class AbstractTestSnapshots:
    def test_default(self, orm, models):
        project = {
            "_meta": {"upsert": {"where": ["email"]}},
            "posts": {"_meta": {"create": True, "upsert": {"where": ["title"]}}},
        }
        upsert = orm.mutations.upsert_node_input(
            model=models.User, project=project, name="DefaultUserInput"
        )
        _assert_snapshot("default", _sdl(upsert))

    @pytest.mark.parametrize("on_match", ["UPDATE", "IGNORE", "APPEND"])
    def test_on_match(self, orm, models, on_match):
        posts = _posts(f"{on_match.title()}Post", onMatch=on_match)
        user = _user(f"{on_match.title()}User", posts)
        user["_meta"]["upsert"]["onMatch"] = on_match
        if on_match == "UPDATE":
            posts["_meta"]["update"] = ["title"]
            user["_meta"]["update"] = ["name"]
        upsert = orm.mutations.upsert_node_input(
            model=models.User, project=user, name=f"{on_match.title()}UserInput"
        )
        _assert_snapshot(f"on_match_{on_match.lower()}", _sdl(upsert))

    def test_immutable(self, orm, models):
        user = _user("FrozenUser", _posts("FrozenPost"), immutable=True)
        user["posts"]["_meta"]["immutable"] = True
        upsert = orm.mutations.upsert_node_input(
            model=models.User, project=user, name="FrozenUserInput"
        )
        _assert_snapshot("immutable", _sdl(upsert))

    def test_a_recursive_named_projection(self, orm, models):
        user: dict[str, Any] = _user("LoopUser", {}, immutable=True)
        user["posts"] = {
            "_meta": {"name": "LoopPost", "immutable": True, "create": ["title"]},
            "author": user,
        }
        upsert = orm.mutations.upsert_node_input(
            model=models.User, project=user, name="LoopUserInput"
        )
        _assert_snapshot("recursive", _sdl(upsert))

    def test_immutable_and_mutable_projections_in_one_tree(self, orm, models):
        events = {
            "_meta": {
                "name": "EditableEvent",
                "create": True,
                "update": ["kind"],
                "upsert": {"where": ["kind", "occurred_on"]},
                "delete": True,
            }
        }
        person = {
            "_meta": {
                "name": "MixedPerson",
                "immutable": True,
                "create": ["ssn", "dob"],
                "upsert": {"where": ["ssn", "dob"]},
            },
            "events": events,
        }
        upsert = orm.mutations.upsert_node_input(
            model=models.Person, project=person, name="MixedPersonInput"
        )
        _assert_snapshot("mixed", _sdl(upsert))


class AbstractTestAcceptanceExample:
    def test_the_insert_only_people_schema(self, orm, models):
        def immutable(name: str, where: list[str], **meta: Any) -> dict[str, Any]:
            return {
                "_meta": {
                    "name": name,
                    "immutable": True,
                    "create": True,
                    "upsert": {"where": where},
                    **meta,
                }
            }

        person_ref = immutable("PersonRef", ["ssn", "dob"])
        person = immutable("Person", ["ssn", "dob"])
        person["events"] = immutable("Event", ["kind", "occurred_on"])
        address = immutable("Address", ["line1"])
        address["person"] = person_ref
        sdl = _without_descriptions(
            _sdl(
                orm.mutations.upsert_node_input(
                    model=models.Person, project=person, name="PersonInput"
                ),
                orm.mutations.upsert_node_input(
                    model=models.Address, project=address, name="AddressInput"
                ),
            )
        )

        for expected in (
            "input PersonInput {\n  where: PersonWhere!\n  create: PersonCreate\n}",
            "input PersonEventsItem @oneOf {\n  create: EventCreate\n"
            "  upsert: EventUpsert\n}",
            "input EventUpsert {\n  where: EventWhere!\n  create: EventCreate\n}",
            "input AddressPersonLink @oneOf {\n  create: PersonRefCreate\n"
            "  upsert: PersonRefUpsert\n}",
        ):
            assert expected in sdl
        assert not re.search(r"Person\w*(Patch|Update)\b", sdl)
        assert "onReplace" not in sdl


# -- Generated shapes ------------------------------------------------------------


def _field_names(input_type: type) -> set[str]:
    return {f.python_name for f in input_type.__strawberry_definition__.fields}


class AbstractTestGeneratedShapes:
    def test_an_immutable_projection_alone_allows_only_create(self, orm, models):
        project = orm.mutations._normalize_model_project(
            models.Post,
            {
                "author": {"_meta": {"immutable": True}},
                "tags": {"_meta": {"immutable": True}},
            },
        )
        specs = orm.mutations._relation_specs(models.Post)
        link = orm.mutations._single_relation_input(
            models.Post, specs["author"], project["relations"]["author"]
        )
        item = orm.mutations._list_relation_ref_type(
            models.Post, specs["tags"], project["relations"]["tags"]
        )
        assert _field_names(link) == {"create"}
        assert _field_names(item) == {"create"}
        assert link.__strawberry_definition__.is_one_of

    def test_a_link_to_an_immutable_target_has_no_on_replace(self, orm, models):
        project = orm.mutations._normalize_model_project(
            models.Address,
            {
                "person": {
                    "_meta": {
                        "immutable": True,
                        "create": True,
                        "upsert": {"where": ["ssn", "dob"]},
                    }
                }
            },
        )
        spec = orm.mutations._relation_specs(models.Address)["person"]
        link = orm.mutations._single_relation_input(
            models.Address, spec, project["relations"]["person"]
        )
        assert _field_names(link) == {"create", "upsert"}
        assert link.__relation_policy__ == {
            "on_replace_options": ("DISCONNECT",),
            "default_on_replace": "DISCONNECT",
            "allowed_ops": frozenset({"create", "upsert"}),
            "on_match": "APPEND",
            "immutable": True,
            "max_visits": None,
        }

    def test_a_link_with_on_replace_stays_a_plain_input(self, orm, models):
        project = orm.mutations._normalize_model_project(models.Post, {"author": {}})
        spec = orm.mutations._relation_specs(models.Post)["author"]
        link = orm.mutations._single_relation_input(
            models.Post, spec, project["relations"]["author"]
        )
        assert "on_replace" in _field_names(link)
        assert not link.__strawberry_definition__.is_one_of

    def test_upsert_and_item_types_carry_their_policy(self, orm, models):
        project = {
            "_meta": {
                "immutable": True,
                "upsert": {"where": ["ssn", "dob"], "onMatch": "IGNORE"},
                "maxVisits": 3,
            },
            "events": {
                "_meta": {
                    "immutable": True,
                    "create": True,
                    "upsert": {"where": ["kind"], "onMatchLinks": "IGNORE"},
                }
            },
        }
        upsert = orm.mutations.upsert_node_input(
            model=models.Person, project=project, name="PolicyPersonInput"
        )
        assert upsert.__upsert_policy__ == {
            "where_fields": ("ssn", "dob"),
            "on_match": "IGNORE",
            "on_match_links": "ERROR",
            "immutable": True,
            "max_visits": 3,
        }
        create = next(
            f.type.of_type
            for f in upsert.__strawberry_definition__.fields
            if f.python_name == "create"
        )
        item = create.__annotations__["events"].__args__[0].__args__[0]
        assert item.__relation_policy__ == {
            "allowed_ops": frozenset({"create", "upsert"}),
            "on_match": "APPEND",
            "immutable": True,
            "max_visits": None,
        }
        event_upsert = item.__annotations__["upsert"].__args__[0]
        assert event_upsert.__upsert_policy__["on_match_links"] == "IGNORE"

    def test_descriptions_appear_only_when_match_rules_are_configured(
        self, orm, models
    ):
        plain = orm.mutations.upsert_node_input(
            model=models.Event, project={"_meta": {"upsert": {"where": ["kind"]}}}
        )
        explicit = orm.mutations.upsert_node_input(
            model=models.Event,
            project={
                "_meta": {
                    "update": ["kind"],
                    "upsert": {"where": ["kind"], "onMatch": "UPDATE"},
                }
            },
        )
        descriptions = {
            f.python_name: f.description
            for f in explicit.__strawberry_definition__.fields
        }
        assert all(
            f.description is None for f in plain.__strawberry_definition__.fields
        )
        assert descriptions["where"] == (
            "Fields that find an existing Event. A matched row is updated from "
            "`update`."
        )
        assert descriptions["update"] is None


# -- Configuration errors ----------------------------------------------------------


class AbstractTestConfigurationErrors:
    @pytest.mark.parametrize(
        ("meta", "message"),
        [
            (
                {"immutable": True, "update": True},
                "_meta.immutable for Person cannot be combined with update",
            ),
            (
                {"immutable": True, "unlink": True, "delete": True},
                "_meta.immutable for Person cannot be combined with unlink, delete",
            ),
            (
                {"immutable": True, "onReplace": "DELETE"},
                "_meta.immutable for Person cannot be combined with onReplace",
            ),
            (
                {"upsert": {"where": ["ssn"], "onMatch": "UPDATE"}},
                "_meta.upsert.onMatch 'UPDATE' for Person requires _meta.update",
            ),
            (
                {"immutable": True, "upsert": {"where": ["ssn"], "onMatch": "UPDATE"}},
                "onMatch 'UPDATE' for Person cannot be combined with _meta.immutable",
            ),
            (
                {"upsert": {"onMatch": "IGNORE"}},
                "_meta.upsert.onMatch for Person requires _meta.upsert.where",
            ),
            (
                {"upsert": {"onMatchLinks": "IGNORE"}},
                "_meta.upsert.onMatchLinks for Person requires _meta.upsert.where",
            ),
            (
                {"upsert": {"where": ["ssn"], "onMatch": "MERGE"}},
                "_meta.upsert.onMatch for Person must be one of UPDATE, IGNORE, APPEND",
            ),
            (
                {"upsert": {"where": ["ssn"], "onMatchLinks": "IGNORE"}},
                "onMatchLinks for Person only applies when onMatch is APPEND",
            ),
            (
                {
                    "upsert": {
                        "where": ["ssn"],
                        "onMatch": "APPEND",
                        "onMatchLinks": "x",
                    }
                },
                "_meta.upsert.onMatchLinks for Person must be one of ERROR, IGNORE",
            ),
            ({"immutable": "yes"}, "_meta.immutable for Person must be a boolean"),
            ({"maxVisits": 0}, "_meta.maxVisits for Person must be a positive integer"),
            ({"maxVisits": True}, "_meta.maxVisits for Person must be a positive"),
        ],
    )
    def test_contradictory_meta_is_rejected(self, orm, models, meta, message):
        with pytest.raises(ValueError, match=re.escape(message)):
            orm.mutations._normalize_model_project(models.Person, {"_meta": meta})

    def test_update_fields_are_never_generated_for_a_projection_without_update(
        self, orm, models
    ):
        project = orm.mutations._normalize_model_project(
            models.Person, {"_meta": {"immutable": True}}
        )
        with pytest.raises(ValueError, match="Update fields were requested for this"):
            orm.mutations._op_fields_for(project, "update")
        with pytest.raises(ValueError, match="requested for Person, whose projection"):
            orm.mutations._update_input(models.Person, project)

    def test_an_update_root_needs_an_update_operation(self, orm, models):
        @orm.type(models.Post)
        class PostNode(strawberry.relay.Node):
            id: strawberry.relay.NodeID[int]

        with pytest.raises(ValueError, match="cannot include Post"):
            orm.mutations.update_node_input(
                models=[models.Post], project={"post": {"_meta": {"create": True}}}
            )

    def test_an_upsert_alone_still_enables_its_update_branch(self):
        policy = RelationPolicy(explicit_ops=True, upsert_where=("email",))
        assert policy.update_enabled
        assert not RelationPolicy(explicit_ops=True).update_enabled
