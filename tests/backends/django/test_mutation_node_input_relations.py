from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.mutation_node_input_relations import (
    AbstractTestExcludedRelations,
    AbstractTestPlainInputKeys,
    AbstractTestSensitiveRelations,
)
from tests.backends.django.account_models import Account, Role
from tests.backends.django.models import Comment, Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(
        User=User, Post=Post, Comment=Comment, Tag=Tag, Account=Account, Role=Role
    )


class TestExcludedRelations(AbstractTestExcludedRelations):
    pass


class TestSensitiveRelations(AbstractTestSensitiveRelations):
    pass


class TestPlainInputKeys(AbstractTestPlainInputKeys):
    pass
