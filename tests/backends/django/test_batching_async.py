"""Async batching behaviour for the Django backend."""

import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_async import AbstractTestBatchingAsync
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


class TestBatchingAsync(AbstractTestBatchingAsync):
    def schema_for(self, *, batching, async_resolver=False):
        orm = StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=False,
            batch_relations=batching,
        )

        @orm.type(DjPost)
        class PT:
            id: auto
            title: auto

        if async_resolver:

            @orm.type(DjUser)
            class UT:
                id: auto
                name: auto

                @strawberry.field
                async def posts(self) -> list[PT]:
                    from asgiref.sync import sync_to_async

                    return await sync_to_async(  # type: ignore[return-value]
                        lambda: list(DjPost.objects.filter(author=self).order_by("id")),
                        thread_sensitive=True,
                    )()

        else:

            @orm.type(DjUser)
            class UT:
                id: auto
                name: auto

                @strawberry.field
                def posts(self) -> list[PT]:
                    return DjPost.objects.filter(author=self).order_by("id")  # type: ignore[return-value]

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    async def execute_async(self, schema, query):
        return await schema.execute(query)

    def execute_sync(self, schema, query):
        return schema.execute_sync(query)
