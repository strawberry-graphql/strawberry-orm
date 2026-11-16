"""A one-to-one relation is a relation everywhere a client can name it (Django)."""

from types import SimpleNamespace

import pytest
from django.db import connection

from strawberry_orm import StrawberryORM
from tests.abstract.one_to_one_relations import (
    AbstractTestForwardOneToOne,
    AbstractTestOneToOneNodeInputs,
    AbstractTestReverseOneToOne,
)
from tests.backends.django.one_to_one_models import Member, Profile


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(setup_tables):
    existing = set(connection.introspection.table_names())
    with connection.schema_editor() as editor:
        for model in (Member, Profile):
            if model._meta.db_table not in existing:
                editor.create_model(model)
    yield SimpleNamespace(Member=Member, Profile=Profile)
    Profile.objects.all().delete()
    Member.objects.all().delete()


@pytest.fixture
def narrow():
    return SimpleNamespace(
        exclude=lambda query, model, field, value: query.exclude(**{field: value}),
    )


@pytest.fixture
def run(models):
    ann = Member.objects.create(id=1, name="Ann")
    ben = Member.objects.create(id=2, name="Ben")
    Member.objects.create(id=3, name="Cy")
    Profile.objects.create(id=1, bio="Ann's", is_public=True, member=ann)
    Profile.objects.create(id=2, bio="Ben's", is_public=False, member=ben)

    async def _run(schema, query, variables=None):
        return await schema.execute(query, variable_values=variables, context_value={})

    return _run


class TestForwardOneToOne(AbstractTestForwardOneToOne):
    pass


class TestReverseOneToOne(AbstractTestReverseOneToOne):
    pass


class TestOneToOneNodeInputs(AbstractTestOneToOneNodeInputs):
    pass
