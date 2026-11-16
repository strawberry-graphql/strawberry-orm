from types import SimpleNamespace

import pytest
import pytest_asyncio
from tortoise import Tortoise

from strawberry_orm import StrawberryORM
from tests.abstract.mutation_node_input_relations import (
    AbstractTestExcludedRelations,
    AbstractTestSensitiveRelations,
)
from tests.backends.tortoise.account_models import Account, Role
from tests.backends.tortoise.models import Comment, Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest_asyncio.fixture
async def models():
    """The models, once ``Tortoise.init`` has wired their relations."""
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={
            "models": [
                "tests.backends.tortoise.models",
                "tests.backends.tortoise.account_models",
            ]
        },
    )
    yield SimpleNamespace(
        User=User, Post=Post, Comment=Comment, Tag=Tag, Account=Account, Role=Role
    )
    await Tortoise.close_connections()


class TestExcludedRelations(AbstractTestExcludedRelations):
    pass


class TestSensitiveRelations(AbstractTestSensitiveRelations):
    pass
