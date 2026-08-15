"""Batching edge cases for the Django backend."""

import strawberry
from django.db import connection
from django.test.utils import CaptureQueriesContext

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_edge_cases import AbstractTestBatchingEdgeCases
from tests.backends.django.custom_pk_fixtures import *  # noqa: F401,F403
from tests.backends.django.models import Book as DjBook
from tests.backends.django.models import Comment as DjComment
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import Publisher as DjPublisher
from tests.backends.django.models import Tag as DjTag
from tests.backends.django.models import User as DjUser


def _orm(batching):
    return StrawberryORM.for_django(
        lazy_resolution="off",
        warn_missing_scope=False,
        batch_relations=batching,
    )


class TestBatchingEdgeCases(AbstractTestBatchingEdgeCases):
    def args_schema(self, *, batching):
        orm = _orm(batching)

        @orm.type(DjPost)
        class PT:
            id: auto
            title: auto

        @orm.type(DjUser)
        class UT:
            id: auto
            name: auto

            @strawberry.field
            def posts_matching(self, title: str) -> list[PT]:
                return DjPost.objects.filter(author=self, title=title)  # type: ignore[return-value]

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def custom_pk_schema(self, *, batching):
        orm = _orm(batching)

        @orm.type(DjBook)
        class BT:
            id: auto
            title: auto

        @orm.type(DjPublisher)
        class PubT:
            publisher_code: auto
            name: auto

            @strawberry.field
            def books(self) -> list[BT]:
                return DjBook.objects.filter(publisher=self).order_by("id")  # type: ignore[return-value]

        @strawberry.type
        class Query:
            publishers: list[PubT] = orm.field.auto()

        return orm.schema(query=Query)

    def self_ref_schema(self, *, batching):
        orm = _orm(batching)

        @orm.type(DjComment)
        class CT:
            id: auto
            body: auto

            @strawberry.field
            def replies(self) -> list["CommentNode"]:  # noqa: F405
                return DjComment.objects.filter(parent=self).order_by("id")  # type: ignore[return-value]

        # Strawberry resolves the forward reference against this module, so the
        # locally built class has to be reachable from module scope.
        globals()["CommentNode"] = CT

        @strawberry.type
        class Query:
            comments: list[CT] = orm.field.auto()

        return orm.schema(query=Query)

    def join_schema(self, *, batching):
        orm = _orm(batching)

        @orm.type(DjPost)
        class PT:
            id: auto
            title: auto

        @orm.type(DjUser)
        class UT:
            id: auto
            name: auto

            @strawberry.field
            def tagged_posts(self) -> list[PT]:
                # Reaches Post through the tag join, so no key column is on the
                # row to group by.
                return DjPost.objects.filter(  # type: ignore[return-value]
                    tags__posts__author=self
                ).distinct()

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def share_a_tag_across_authors(self):
        python = DjTag.objects.get(name="python")
        bob = DjUser.objects.get(name="Bob")
        shared = DjPost.objects.create(
            title="Bob shares python", body="b", is_published=True, author=bob
        )
        shared.tags.add(python)

    def two_path_schema(self, *, batching):
        return self._plain_schema(batching)

    def duplicate_schema(self, *, batching):
        orm = _orm(batching)
        PT, UT = self._user_post_types(orm)

        @strawberry.type
        class Query:
            @strawberry.field
            def duplicated_users(self) -> list[UT]:
                users = list(DjUser.objects.all().order_by("id"))
                return users + users[:1]  # type: ignore[return-value]

        return orm.schema(query=Query)

    def empty_parents_schema(self, *, batching):
        orm = _orm(batching)
        PT, UT = self._user_post_types(orm)

        @strawberry.type
        class Query:
            @strawberry.field
            def users(self) -> list[UT]:
                return DjUser.objects.none()  # type: ignore[return-value]

        return orm.schema(query=Query)

    def _user_post_types(self, orm):
        @orm.type(DjPost)
        class PT:
            id: auto
            title: auto

        @orm.type(DjUser)
        class UT:
            id: auto
            name: auto

            @strawberry.field
            def posts(self) -> list[PT]:
                return DjPost.objects.filter(author=self).order_by("id")  # type: ignore[return-value]

        return PT, UT

    def _plain_schema(self, batching):
        orm = _orm(batching)
        PT, UT = self._user_post_types(orm)

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def execute(self, schema, query):
        return schema.execute_sync(query)

    def count_queries(self, schema, query):
        with CaptureQueriesContext(connection) as ctx:
            schema.execute_sync(query)
        return len(ctx)
