"""A field the library is asked to build carries everything it was declared with
(Django)."""

from types import SimpleNamespace

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.declared_field_options import (
    AbstractTestDeclaredFieldOptions,
    Allow,
)
from tests.backends.django.models import Comment, Post, User


@pytest.fixture
def make_orm():
    def _make(**options):
        return StrawberryORM.for_django(
            warn_missing_scope=False, **{"lazy_resolution": "off", **options}
        )

    return _make


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


@pytest.fixture
def narrow():
    return SimpleNamespace(
        only=lambda query, model, field, value: query.filter(**{field: value}),
        exclude=lambda query, model, field, value: query.exclude(**{field: value}),
    )


@pytest.fixture
def held(seed):
    async def _held(model, *ids):
        return [row async for row in model.objects.filter(id__in=ids).order_by("id")]

    return _held


@pytest.fixture
def run(seed):
    async def _run(schema, query, context):
        return await schema.execute(query, context_value=dict(context))

    return _run


class TestDeclaredFieldOptions(AbstractTestDeclaredFieldOptions):
    pass


def _pinned_schema(lazy_resolution):
    orm = StrawberryORM.for_django(lazy_resolution=lazy_resolution)

    @orm.type(User, name="RdUser")
    class UserType:
        name: auto
        email: auto

        @classmethod
        def scope_rows(cls, query, info):
            return query

    @orm.type(Comment, name="RdComment")
    class CommentType:
        body: auto

        @classmethod
        def scope_rows(cls, query, info):
            return query

    @orm.type(Post, name="RdPost")
    class PostType:
        title: auto
        comments: list[CommentType]
        author: UserType | None = orm.field.eager(
            scope=lambda query, info: query.filter(name="Alice"),
            permission_classes=[Allow],
        )

        @classmethod
        def scope_rows(cls, query, info):
            return query

    @strawberry.type
    class Query:
        @strawberry.field
        def pinned(self, info: strawberry.Info) -> PostType:
            return info.context["pinned"]

    return orm.schema(query=Query)


PINNED = {
    "once": "{ b: pinned { title author { name email } } }",
    # The first read loads the comments, so the second meets a row the
    # planner treats as loaded and does not load the author for.
    "twice": "{ a: pinned { comments { body } } b: pinned { title author { name email } } }",
}


@pytest.mark.parametrize("lazy_resolution", ["off", "warn"])
@pytest.mark.parametrize("read", list(PINNED))
@pytest.mark.filterwarnings("ignore::UserWarning")
def test_one_row_held_by_the_app_keeps_its_scoped_author_hidden(
    seed, read, lazy_resolution
):
    result = _pinned_schema(lazy_resolution).execute_sync(
        PINNED[read], context_value={"pinned": Post.objects.get(title="Draft Post")}
    )
    assert result.errors is None, result.errors
    assert result.data["b"] == {"title": "Draft Post", "author": None}
