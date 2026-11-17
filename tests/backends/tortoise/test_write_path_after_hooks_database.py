import pytest
import pytest_asyncio
from tortoise import Tortoise

from tests.abstract.write_path_after_hooks_database import (
    AbstractTestAfterHooksWaitForTheDatabase,
    AbstractTestAfterHooksWaitForTheKeys,
    refusing_triggers,
)
from tests.backends.tortoise.test_write_path_authorization import (  # noqa: F401
    writes,
)


@pytest_asyncio.fixture
async def refusing(writes, blog):  # noqa: F811
    connection = Tortoise.get_connection("default")
    for statement in refusing_triggers("post_tag"):
        await connection.execute_script(statement)


@pytest.fixture
def keys_enforced():
    """Tortoise turns SQLite's foreign keys on, and checks them as each statement runs."""


class TestAfterHooksWaitForTheDatabase(AbstractTestAfterHooksWaitForTheDatabase):
    pass


class TestAfterHooksWaitForTheKeys(AbstractTestAfterHooksWaitForTheKeys):
    pass
