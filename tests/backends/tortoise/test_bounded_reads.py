"""Every page and root list is cut by the database (Tortoise)."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from strawberry_orm import StrawberryORM
from tests.abstract.bounded_reads import AbstractTestBoundedReads


class UbOwner(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)

    items: fields.ReverseRelation["UbItem"]

    class Meta:
        table = "ub_owner"


class UbItem(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)
    bucket = fields.IntField()
    hidden = fields.BooleanField(default=False)
    owner = fields.ForeignKeyField("ub_bounded_reads.UbOwner", related_name="items")

    class Meta:
        table = "ub_item"


@pytest_asyncio.fixture
async def seeded():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"ub_bounded_reads": [__name__]}
    )
    await Tortoise.generate_schemas()
    for i in (1, 2, 3):
        await UbOwner.create(id=i, name=f"o{i}")
    await UbItem.bulk_create(
        UbItem(
            id=i,
            name=f"i{i:02}",
            bucket=i % 3,
            hidden=i in (2, 15),
            owner_id=(i - 1) // 10 + 1,
        )
        for i in range(1, 31)
    )
    yield
    await Tortoise.close_connections()


@pytest.fixture
def make_orm():
    def _make(**options):
        options.setdefault("lazy_resolution", "off")
        return StrawberryORM.for_tortoise(
            warn_missing_scope=False, default_query_limit=5, **options
        )

    return _make


@pytest.fixture
def Owner():
    return UbOwner


@pytest.fixture
def Item():
    return UbItem


@pytest.fixture
def hide():
    return lambda query: query.filter(hidden=False)


@pytest.fixture
def all_items():
    return lambda: UbItem.all()


@pytest.fixture
def owner_items():
    return lambda owner: UbItem.filter(owner_id=owner.pk)


@pytest.fixture
def capture(monkeypatch):
    from tortoise import connections

    @contextmanager
    def _capture():
        seen = SimpleNamespace(loaded=0, params=[])
        client = connections.get("default")

        def recording(original):
            async def run(sql, values=None, *args, **kwargs):
                seen.params.append(len(values or ()))
                return await original(sql, values, *args, **kwargs)

            return run

        build = UbItem._init_from_db.__func__

        def counted(cls, **row):
            seen.loaded += 1
            return build(cls, **row)

        with monkeypatch.context() as patch:
            for name in ("execute_query", "execute_query_dict"):
                patch.setattr(client, name, recording(getattr(client, name)))
            patch.setattr(UbItem, "_init_from_db", classmethod(counted))
            yield seen

    return _capture


@pytest.fixture
def run():
    async def _run(schema, query):
        return await schema.execute(query)

    return _run


class TestBoundedReads(AbstractTestBoundedReads):
    pass
