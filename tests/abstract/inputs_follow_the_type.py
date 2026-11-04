"""Filters, ordering, group-by and aggregates cover what the type shows.

They are generated from the model, which knows every column, while the type
chooses which of them a caller may read. Left as they were, a column the type
hides could still be filtered on, which reads it one ``startsWith`` probe at a
time; ordered by; returned verbatim as a group key; or read exactly through
``min`` and ``max``. So every input generated for a model covers only the
columns and relations its types show, read as they are.

Backends supply ``orm`` and ``models`` (``User`` and ``Post``, with
``User.posts``). Nothing here runs a query: the schema is the evidence.
"""

import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


def fields(schema, type_name):
    """GraphQL field names of *type_name*, or an empty set if it is unreachable."""
    graphql_type = schema._schema.type_map.get(type_name)
    return set(graphql_type.fields) if graphql_type is not None else set()


def _users_schema(orm, UserNode, **schema_kwargs):
    @strawberry.type
    class Query:
        users: ORMListConnection[UserNode] = orm.connection()

    return orm.schema(query=Query, **schema_kwargs)


def _lists_schema(orm, *types, filters=None, **schema_kwargs):
    """A schema listing each of *types* from the root."""
    annotations = {f"list{i}": list[t] for i, t in enumerate(types)}
    namespace = {
        "__annotations__": annotations,
        **{name: orm.field.eager(filters=filters) for name in annotations},
    }
    query = strawberry.type(type("Query", (), namespace))
    return orm.schema(query=query, **schema_kwargs)


class AbstractTestInputsFollowTheType:
    def _node(self, orm, User):
        @orm.type(
            User,
            filters=orm.filter(User),
            order=orm.order(User),
            group=orm.group(User),
        )
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

        return UserNode

    def test_inputs_cover_only_the_columns_the_type_shows(self, orm, models):
        schema = _users_schema(orm, self._node(orm, models.User))
        for name in ("UserField", "UserOrderField", "UserGroupByField"):
            assert fields(schema, name) == {"id", "name"}, name

    def test_aggregates_and_group_keys_cover_them_too(self, orm, models):
        """``min``/``max`` return exact values and a group key echoes one."""
        schema = _users_schema(orm, self._node(orm, models.User))
        assert fields(schema, "UserGroupKey") == {"id", "name"}
        for name in ("UserMinAggregates", "UserMaxAggregates"):
            assert "createdAt" not in fields(schema, name), name

    def test_a_hidden_column_cannot_be_filtered_on(self, orm, models):
        schema = _users_schema(orm, self._node(orm, models.User))
        result = schema.execute_sync(
            '{ users(filter: {field: {email: {startsWith: "a"}}}) '
            "{ edges { node { name } } } }"
        )
        assert result.errors, "a hidden column was accepted as a filter"
        assert "email" in result.errors[0].message

    def test_a_relation_the_type_does_not_show_cannot_be_traversed(self, orm, models):
        orm.filter(models.Post)
        orm.order(models.Post)
        schema = _users_schema(orm, self._node(orm, models.User))
        assert "object" not in fields(schema, "UserFilter")
        assert "object" not in fields(schema, "UserOrder")

    def test_traversal_covers_the_related_types_fields(self, orm, models):
        @orm.type(models.Post, filters=orm.filter(models.Post))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        @orm.type(models.User, filters=orm.filter(models.User))
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: list[PostNode]

        schema = _users_schema(orm, UserNode)
        assert fields(schema, "UserFilterObject") == {"posts"}
        assert fields(schema, "PostField") == {"id", "title"}

    def _admin_and_public(self, orm, models):
        """A User filter attached to a type showing ``email``, and a Post type
        whose ``author`` is a User type that does not show it."""

        @orm.type(models.User, name="UserAdmin", filters=orm.filter(models.User))
        class UserAdmin(relay.Node):
            id: relay.NodeID[int]
            name: auto
            email: auto

        @orm.type(models.User, name="UserPublic")
        class UserPublic(relay.Node):
            id: relay.NodeID[int]
            name: auto

        @orm.type(models.Post, filters=orm.filter(models.Post))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto
            author: UserPublic

        return UserAdmin, PostNode

    def test_traversal_covers_the_type_the_relation_returns(self, orm, models):
        """Through ``post.author`` the filter serves ``UserPublic``, whatever
        type it is attached to elsewhere, so ``email`` is not offered there."""
        UserAdmin, PostNode = self._admin_and_public(orm, models)
        public = _lists_schema(orm, PostNode)
        assert fields(public, "PostFilterObject") == {"author"}
        assert fields(public, "UserField") == {"id", "name"}

    def test_each_schema_covers_the_types_it_reaches(self, orm, models):
        """An admin schema that never reaches ``UserPublic`` keeps ``email``;
        one reaching both covers what both show."""
        UserAdmin, PostNode = self._admin_and_public(orm, models)
        admin = _lists_schema(orm, UserAdmin)
        both = _lists_schema(orm, UserAdmin, PostNode)
        assert fields(admin, "UserField") == {"id", "name", "email"}
        assert fields(both, "UserField") == {"id", "name"}

    def test_a_type_declared_later_leaves_a_built_schema_alone(self, orm, models):
        """A built schema keeps its inputs, and every field it offers is still
        on the input class, so a value sent for it is not dropped."""
        users = orm.filter(models.User)

        @orm.type(models.User, name="UserFull", filters=users)
        class UserFull(relay.Node):
            id: relay.NodeID[int]
            name: auto

        schema = _lists_schema(orm, UserFull)

        @orm.type(models.User, name="UserBare", filters=users)
        class UserBare(relay.Node):
            id: relay.NodeID[int]

        offered = fields(schema, "UserField")
        definition = users._field_type.__strawberry_definition__
        assert offered == {"id", "name"}
        assert offered <= {
            field.graphql_name or field.name for field in definition.fields
        }

    def test_a_node_may_name_a_type_declared_later(self, orm, models):
        """Registering a type does not resolve its annotations, which may still
        name a type further down the module."""

        @orm.type(models.Post)
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            author: "UserDeclaredLater"  # noqa: F821

        assert PostNode in orm.backend._shown_by_type

    def test_a_field_with_its_own_resolver_does_not_open_its_column(self, orm, models):
        """The field shows what the resolver returns, not the column."""

        @orm.type(models.User, filters=orm.filter(models.User))
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

            @strawberry.field
            def email(self) -> str:
                return "hidden"

        assert "email" not in fields(_users_schema(orm, UserNode), "UserField")

    def test_an_input_built_after_the_type_is_narrowed_too(self, orm, models):
        @orm.type(models.User)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

        later = orm.filter(models.User)
        schema = _lists_schema(orm, UserNode, filters=later)
        assert fields(schema, "UserField") == {"id", "name"}

    def test_an_input_two_types_share_covers_what_both_show(self, orm, models):
        shared = orm.filter(models.User)

        @orm.type(models.User, name="UserFull", filters=shared)
        class UserFull:
            id: auto
            name: auto
            email: auto

        @orm.type(models.User, name="UserBrief", filters=shared)
        class UserBrief:
            id: auto
            name: auto

        schema = _lists_schema(orm, UserFull, UserBrief)
        assert fields(schema, "UserField") == {"id", "name"}

    def test_an_input_follows_its_own_type_not_a_sibling(self, orm, models):
        """Another type of the same model, using another input, narrows nothing."""
        mine = orm.filter(models.User)

        @orm.type(models.User, name="UserWithEmail", filters=mine)
        class UserWithEmail:
            id: auto
            name: auto
            email: auto

        @orm.type(models.User, name="UserWithoutEmail")
        class UserWithoutEmail:
            id: auto
            name: auto

        schema = _lists_schema(orm, UserWithEmail, UserWithoutEmail)
        assert fields(schema, "UserField") == {"id", "name", "email"}

    def test_a_node_resolving_its_own_id_does_not_show_the_column(self, orm, models):
        """Only a ``NodeID`` serves the column as the id; this node computes one."""

        @orm.type(
            models.User,
            filters=orm.filter(models.User),
            order=orm.order(models.User),
            group=orm.group(models.User),
        )
        class UserNode(relay.Node):
            name: auto

            @classmethod
            def resolve_id(cls, root, *, info):
                return root.name  # pragma: no cover - never resolved

        schema = _users_schema(orm, UserNode)
        assert fields(schema, "UserField") == {"name"}
        # name is neither numeric nor comparable, so no sub-aggregate is left.
        assert fields(schema, "UserAggregates") == {"count"}

    def test_an_input_left_with_nothing_is_refused(self, orm, models):
        import pytest

        @orm.type(models.User, order=orm.order(models.User))
        class UserNode:
            @strawberry.field
            def label(self) -> str:
                return "x"  # pragma: no cover - never resolved

        with pytest.raises(ValueError, match="left with nothing to use"):
            _lists_schema(orm, UserNode)

    def test_grouping_needs_a_column_to_group_by(self, orm, models):
        import pytest

        from strawberry_orm import group_field

        @orm.group_type(models.User)
        class ByDomain:
            @group_field
            def domain(self, query, value, info):
                return query  # pragma: no cover - never queried

        @orm.type(models.User, group=ByDomain)
        class UserNode(relay.Node):
            @classmethod
            def resolve_id(cls, root, *, info):
                return "x"  # pragma: no cover - never resolved

            @strawberry.field
            def label(self) -> str:
                return "x"  # pragma: no cover - never resolved

        with pytest.raises(ValueError, match="cannot be grouped"):
            _users_schema(orm, UserNode)

    def test_a_shared_input_is_in_the_schemas_both_types_show_it_in(self, orm, models):
        shared = orm.filter(models.User)

        @orm.type(models.User, name="Everywhere", filters=shared)
        class Everywhere(relay.Node):
            id: relay.NodeID[int]
            email: auto = orm.field.eager(tags=["internal", "partner"])

        @orm.type(models.User, name="PartnersOnly", filters=shared)
        class PartnersOnly(relay.Node):
            id: relay.NodeID[int]
            email: auto = orm.field.eager(tags="partner")

        @orm.type(models.User, name="Untagged", filters=shared)
        class Untagged(relay.Node):
            id: relay.NodeID[int]
            email: auto

        everyone = (Everywhere, PartnersOnly, Untagged)
        partner = _lists_schema(orm, *everyone, tags="partner")
        internal = _lists_schema(orm, *everyone, tags="internal")
        assert fields(partner, "UserField") == {"id", "email"}
        assert fields(internal, "UserField") == {"id"}

    def test_a_tagged_field_takes_its_filter_with_it(self, orm, models):
        """A schema that leaves the field out leaves out filtering on it too."""

        @orm.type(models.User, filters=orm.filter(models.User))
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            email: auto = orm.field.eager(tags="internal")

        public = _users_schema(orm, UserNode, tags="public")
        internal = _users_schema(orm, UserNode, tags="internal")
        assert fields(public, "UserField") == {"id", "name"}
        assert fields(internal, "UserField") == {"id", "name", "email"}
