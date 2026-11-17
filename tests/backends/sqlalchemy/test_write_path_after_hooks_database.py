import pytest_asyncio
from sqlalchemy import text

from tests.abstract.write_path_after_hooks_database import (
    AbstractTestAfterHooksWaitForTheDatabase,
    AbstractTestAfterHooksWaitForTheKeys,
    refusing_triggers,
)
from tests.abstract.write_path_authorization import resolve
from tests.backends.sqlalchemy.test_write_path_authorization import (  # noqa: F401
    writes,
)


async def _execute(db, statements):
    for statement in statements:
        await resolve(db.session.execute(text(statement)))
    await resolve(db.session.commit())


@pytest_asyncio.fixture
async def refusing(writes, blog):  # noqa: F811
    await _execute(writes, refusing_triggers("post_tag"))


@pytest_asyncio.fixture
async def keys_enforced(writes, blog):  # noqa: F811
    # SQLite enforces foreign keys only once asked to, outside a transaction.
    await _execute(writes, ["PRAGMA foreign_keys = ON"])


class TestAfterHooksWaitForTheDatabase(AbstractTestAfterHooksWaitForTheDatabase):
    pass


class TestAfterHooksWaitForTheKeys(AbstractTestAfterHooksWaitForTheKeys):
    pass
