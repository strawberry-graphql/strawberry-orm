"""Abstract tests for singular (``T | None``) fields.

Returning a query object from a singular field keeps it on the optimizer path;
without this the only option was ``.first()``, which materializes and disables
optimization for everything below.
"""

import strawberry

from strawberry_orm.types import auto


class AbstractTestSingularFields:
    """Subclasses provide ``single_post_schema`` and ``execute``."""

    def test_singular_field_unwraps_the_single_row(self, orm, seed, caplog):
        schema = self.single_post_schema(orm)
        result = self.execute(schema, "{ post(id: 1) { title } }")

        assert result.errors is None
        assert result.data["post"] == {"title": "Hello World"}

    def test_singular_field_eager_loads_nested_relations(self, orm, seed):
        schema = self.single_post_schema(orm)
        result = self.execute(schema, "{ post(id: 1) { title author { name } } }")

        assert result.errors is None
        assert result.data["post"]["author"]["name"] == "Alice"

    def test_singular_field_returns_null_when_missing(self, orm, seed):
        schema = self.single_post_schema(orm)
        result = self.execute(schema, "{ post(id: 999) { title } }")

        assert result.errors is None
        assert result.data["post"] is None

    def test_list_fields_are_not_unwrapped(self, orm, seed):
        schema = self.single_post_schema(orm)
        result = self.execute(schema, "{ posts { title } }")

        assert result.errors is None
        assert len(result.data["posts"]) == 4


def build_types(orm, User, Post):
    @orm.type(User)
    class UT:
        id: auto
        name: auto

    @orm.type(Post)
    class PT:
        id: auto
        title: auto
        author: UT

    return PT


def build_query(orm, PT, single_resolver, list_resolver):
    @strawberry.type
    class Query:
        post: PT | None = strawberry.field(resolver=single_resolver)
        posts: list[PT] = strawberry.field(resolver=list_resolver)

    return Query
