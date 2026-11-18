"""A schema serves the same whatever was built from the same roots before it.

Strawberry applies a field's extensions to the field itself each time a schema
converts it, and an ORM's generated inputs are shared by every schema it
builds, so a schema built after another could serve what the other left
behind: a tagged argument an earlier schema's input mutation had turned into
an untagged input field reached a ``public`` schema built after it. Each
schema is printed built alone and built after each of the others.
"""

from typing import Annotated

import pytest
import strawberry
from strawberry.field_extensions import InputMutationExtension

from strawberry_orm import StrawberryORM, auto, tags
from tests.backends.sqlalchemy.models import Post, User

SELECTIONS = {"untagged": {}, "public": {"tags": "public"}}
ROOTS = ("users", "posts")
SCHEMAS = [f"{root} {selection}" for root in ROOTS for selection in SELECTIONS]


def _world():
    """Builders of every schema in :data:`SCHEMAS`, sharing one fresh ORM."""
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )
    user_filter, user_order = orm.filter(User), orm.order(User)

    @orm.type(User, filters=user_filter, order=user_order)
    class BoUser:
        id: auto
        name: auto
        email: auto = orm.field.eager(tags="internal")

    @orm.type(User, filters=user_filter, order=user_order)
    class BoAuthor:
        id: auto
        name: auto

    @orm.type(Post, filters=orm.filter(Post))
    class BoPost:
        id: auto
        title: auto
        author: BoAuthor

    @strawberry.type
    class Users:
        users: list[BoUser] = orm.field.eager()

    @strawberry.type
    class Posts:
        posts: list[BoPost] = orm.field.eager()
        authors: list[BoAuthor] = orm.field.eager()

    @strawberry.type
    class Rename:
        @strawberry.mutation(extensions=[InputMutationExtension()])
        def rename(
            self,
            name: str,
            force: Annotated[bool, strawberry.argument(metadata=tags("internal"))] = (
                False
            ),
        ) -> str:
            return name

    roots = {"users": Users, "posts": Posts}
    return {
        f"{root} {selection}": lambda root=root, selection=selection: orm.schema(
            query=roots[root], mutation=Rename, **SELECTIONS[selection]
        )
        for root in ROOTS
        for selection in SELECTIONS
    }


@pytest.mark.parametrize("earlier", SCHEMAS)
@pytest.mark.parametrize("schema", SCHEMAS)
def test_a_schema_is_the_same_built_after_another(schema, earlier):
    alone = _world()[schema]()
    builders = _world()
    builders[earlier]()
    assert str(builders[schema]()) == str(alone)
