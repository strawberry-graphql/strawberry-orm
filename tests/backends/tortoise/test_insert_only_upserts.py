import logging
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from tortoise import Tortoise

from strawberry_orm import StrawberryORM
from tests.abstract.insert_only_upserts import (
    AbstractTestApplyRefList,
    AbstractTestBooks,
    AbstractTestFindOrCreate,
    AbstractTestForwardLinks,
    AbstractTestHandBuiltPayloads,
    AbstractTestHooks,
    AbstractTestOnMatch,
    AbstractTestUpdateOnMatch,
    AbstractTestValidation,
    UpsertDb,
    build_schema,
)
from tests.backends.tortoise import insert_only_models, models

_built: SimpleNamespace | None = None


def _schema() -> SimpleNamespace:
    """Build the schema once; Tortoise only knows its relations after ``init``."""
    global _built
    if _built is None:
        _built = build_schema(
            StrawberryORM.for_tortoise(
                lazy_resolution="off", warn_missing_scope=False, warn_sensitive=False
            ),
            SimpleNamespace(
                Person=insert_only_models.Person,
                Address=insert_only_models.Address,
                Event=insert_only_models.Event,
                Group=insert_only_models.Group,
                User=models.User,
                Post=models.Post,
                Tag=models.Tag,
                Publisher=models.Publisher,
                Book=models.Book,
            ),
        )
    return _built


class TortoiseDb(UpsertDb):
    async def create(self, model: type, **values: Any) -> Any:
        return await model.create(**values)

    async def add_related(self, instance: Any, name: str, *rows: Any) -> None:
        await getattr(instance, name).add(*rows)

    async def rows(self, model: type) -> list[Any]:
        return await model.all().order_by(model._meta.pk_attr)

    async def reload(self, instance: Any) -> Any:
        return await type(instance).get(pk=instance.pk)

    async def related(self, instance: Any, name: str) -> list[Any]:
        return await getattr(instance, name).all()


class _Recorder(logging.Handler):
    def __init__(self, statements: list[str]) -> None:
        super().__init__(logging.DEBUG)
        self.statements = statements

    def emit(self, record: logging.LogRecord) -> None:
        self.statements.append(str(record.args[0] if record.args else record.msg))


@pytest_asyncio.fixture
async def db():
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={
            "models": [
                "tests.backends.tortoise.models",
                "tests.backends.tortoise.insert_only_models",
            ]
        },
    )
    await Tortoise.generate_schemas()
    harness = TortoiseDb(_schema(), async_hooks=True)
    logger = logging.getLogger("tortoise.db_client")
    level = logger.level
    recorder = _Recorder(harness.statements)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(recorder)
    yield harness
    logger.removeHandler(recorder)
    logger.setLevel(level)
    await Tortoise.close_connections()


class TestFindOrCreate(AbstractTestFindOrCreate):
    pass


class TestOnMatch(AbstractTestOnMatch):
    pass


class TestForwardLinks(AbstractTestForwardLinks):
    pass


class TestValidation(AbstractTestValidation):
    pass


class TestHandBuiltPayloads(AbstractTestHandBuiltPayloads):
    pass


class TestHooks(AbstractTestHooks):
    pass


class TestApplyRefList(AbstractTestApplyRefList):
    pass


class TestUpdateOnMatch(AbstractTestUpdateOnMatch):
    pass


class TestBooks(AbstractTestBooks):
    pass
