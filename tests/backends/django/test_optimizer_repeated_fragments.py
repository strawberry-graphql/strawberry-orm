from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.db import connection

from strawberry_orm import StrawberryORM
from tests.abstract.optimizer_repeated_fragments import AbstractTestRepeatedFragments
from tests.backends.django.models import Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Tag=Tag)


class TestRepeatedFragments(AbstractTestRepeatedFragments):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        def _run():
            statements: list[str] = []

            def count(execute, sql, params, many, context):
                statements.append(sql)
                return execute(sql, params, many, context)

            # Connections are per thread, so the wrapper goes on the one the
            # query runs in.
            with connection.execute_wrapper(count):
                result = schema.execute_sync(query)
            return result, len(statements)

        result, self.query_count = await sync_to_async(_run, thread_sensitive=True)()
        return result
