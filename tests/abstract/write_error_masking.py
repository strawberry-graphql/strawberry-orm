"""A write that fails tells the client one plain message, whatever failed.

A write runs the ORM's statements through the repo: it looks rows up, then
inserts, updates, links, unlinks and deletes them. The ORM's and the driver's
errors name tables, columns and the model's class, and quote the values bound.
Masking them by type let through each type the list left out - Django's
``ValidationError`` and ``ObjectDoesNotExist``, Tortoise's ``FieldError``, a
driver's own errors, a plain ``ValueError`` from a field preparing a value. So
whatever the ORM raises while it builds or runs a statement, the client is
told "The database refused the write" (or "Invalid filter expression" for a
lookup, or "Database connection failed"), and the original is kept as the
cause, for the server log. So is an ORM error a repo hook raises, as it
queries the database itself.

Each error is raised by every statement the write runs, or by its inserts,
updates and deletes alone, so the lookups before them succeed and the write
fails at the statement that changes a row. Only a backend's own error handling
tells its families apart, and every write reaches it the same way, so each
family is raised on one write, and each write is given the backend's
``TYPICAL`` families and the plain Python errors (see ``read_error_masking``).
And each write that names a row is given a lone surrogate, which a client's
JSON variables carry and the driver cannot encode: SQLite's raises a plain
``UnicodeEncodeError``, which every backend passed on as it was.

Each backend supplies a ``writes`` fixture, a
:class:`~tests.abstract.write_path_authorization.WriteDb`, and on the test
class:

- ``FAMILIES``: ``{name: exception class}``, the ORM's and the drivers' errors,
  each built with one message;
- ``TYPICAL``: the names of two families, a database error and one of the
  ORM's own;
- ``failing(writes, error, verbs)``, a context manager under which every
  statement starting with one of *verbs* raises *error*.
"""

from typing import Any

import pytest

from strawberry_orm import AbstractRepo
from tests.abstract.read_error_masking import LEAK, RUNNING, causes
from tests.abstract.write_path_authorization import (
    POST_WITH_AUTHOR,
    Blog,
    TagCreate,
    TagUpdate,
    allow,
    item,
    member,
)

PLAIN = {
    "The database refused the write",
    "Invalid filter expression",
    "Database connection failed",
}

#: The statements each moment of a write fails at.
MOMENTS = {
    "every statement": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "the statement changing a row": ("INSERT", "UPDATE", "DELETE"),
}

WRITES = (
    "ref create",
    "ref update",
    "ref link",
    "ref unlink",
    "ref delete",
    "upsert insert",
    "upsert update",
    "nested update",
)

#: The write each family is raised on.
EVERY_FAMILY_ON = "upsert update"

#: The writes that name the row they change.
NAMING = ("ref create", "ref update", "upsert insert", "upsert update", "nested update")

TAG_UPSERT = {
    "_meta": {
        "create": ["name"],
        "update": ["name"],
        "upsert": {"where": ["name"], "onMatch": "UPDATE"},
    }
}


def raising_repo(error: BaseException) -> type:
    """Raises *error* from every check, the way a repo querying the database might."""

    class Raising(AbstractRepo):
        def _raise(self, *args: Any, **kwargs: Any) -> Any:
            raise error

        can_create = can_update = can_delete = can_link = can_unlink = _raise
        can_upsert = _raise

    return Raising


class AbstractTestWriteErrorMasking(Blog):
    FAMILIES: dict = {}
    TYPICAL: tuple = ()

    def pytest_generate_tests(self, metafunc):
        if "family" in metafunc.fixturenames:
            metafunc.parametrize("family", sorted(self.FAMILIES))
        if "typical" in metafunc.fixturenames:
            metafunc.parametrize("typical", self.TYPICAL)
        if "running" in metafunc.fixturenames:
            metafunc.parametrize("running", sorted({*self.TYPICAL, *RUNNING}))

    @staticmethod
    async def state(writes: Any, blog: Any) -> Any:
        m = writes.m
        return (
            [(tag.id, tag.name) for tag in await writes.rows(m.Tag)],
            [tag.id for tag in await writes.related(blog.hello, "tags")],
            [(post.id, post.title) for post in await writes.rows(m.Post)],
            [(user.id, user.name) for user in await writes.rows(m.User)],
        )

    @staticmethod
    async def attempt(
        writes: Any, blog: Any, orm: Any, write: str, name: str = "pwned"
    ) -> None:
        """Make *write*, giving the row it changes *name*, where it names one."""
        m = writes.m
        if write.startswith("ref "):
            ref = orm.ref(
                m.Tag, create=TagCreate, update=TagUpdate, unlink=True, delete=True
            )
            refs = {
                "ref create": [item(ref, "create", name=name)],
                "ref update": [item(ref, "update", id="1", name=name)],
                "ref link": [item(ref, "update", id="2")],
                "ref unlink": [item(ref, "unlink", id="1")],
                "ref delete": [item(ref, "delete", id="1")],
            }[write]
            await writes.refs(orm, blog.hello, "tags", refs, authorize=allow)
            return
        if write == "nested update":
            upsert = orm.mutations.upsert_node_input(
                model=m.Post, project=POST_WITH_AUTHOR, name="MaskedPostUpsert"
            )
            link_type = member(upsert, "update", "author")
            author = member(link_type, "update")(id="1", name=name)
            payload = upsert(
                where=member(upsert, "where")(title="Hello"),
                update=member(upsert, "update")(author=link_type(update=author)),
            )
        else:
            upsert = orm.mutations.upsert_node_input(
                model=m.Tag, project=TAG_UPSERT, name="MaskedTagUpsert"
            )
            found = name if write == "upsert insert" else "python"
            payload = upsert(
                where=member(upsert, "where")(name=found),
                create=member(upsert, "create")(name=found),
                update=member(upsert, "update")(name=name),
            )
        await writes.upsert(orm, payload, authorize=allow)

    @staticmethod
    def assert_plain(raised: BaseException, error: BaseException) -> None:
        assert isinstance(raised, ValueError | ConnectionError), repr(raised)
        assert str(raised) in PLAIN, str(raised)
        assert LEAK not in str(raised)
        assert error in causes(raised), repr(raised)

    async def failing_statement(
        self, writes: Any, blog: Any, error: BaseException, write: str, moment: str
    ) -> None:
        orm = writes.new_orm()
        before = await self.state(writes, blog)
        with (
            self.failing(writes, error, MOMENTS[moment]),
            pytest.raises(Exception) as raised,
        ):
            await self.attempt(writes, blog, orm, write)
        self.assert_plain(raised.value, error)
        assert await self.state(writes, blog) == before

    async def failing_hook(
        self, writes: Any, blog: Any, error: BaseException, write: str
    ) -> None:
        repo = raising_repo(error)
        m = writes.m
        orm = writes.new_orm(repos={m.Tag: repo, m.Post: repo, m.User: repo})
        before = await self.state(writes, blog)
        with pytest.raises(Exception) as raised:
            await self.attempt(writes, blog, orm, write)
        self.assert_plain(raised.value, error)
        assert await self.state(writes, blog) == before

    @pytest.mark.parametrize("moment", list(MOMENTS))
    @pytest.mark.parametrize("write", WRITES)
    async def test_a_statement_failing_as_the_write_runs_it(
        self, writes, blog, write, moment, running
    ):
        error = {**self.FAMILIES, **RUNNING}[running](LEAK)
        await self.failing_statement(writes, blog, error, write, moment)

    @pytest.mark.parametrize("moment", list(MOMENTS))
    async def test_each_family_failing_as_a_write_runs(
        self, writes, blog, moment, family
    ):
        error = self.FAMILIES[family](LEAK)
        await self.failing_statement(writes, blog, error, EVERY_FAMILY_ON, moment)

    @pytest.mark.parametrize("write", WRITES)
    async def test_a_repo_hook_raising_an_orm_error(self, writes, blog, write, typical):
        await self.failing_hook(writes, blog, self.FAMILIES[typical](LEAK), write)

    async def test_each_family_a_repo_hook_raises(self, writes, blog, family):
        error = self.FAMILIES[family](LEAK)
        await self.failing_hook(writes, blog, error, EVERY_FAMILY_ON)

    @pytest.mark.parametrize("write", NAMING)
    async def test_a_value_the_driver_cannot_bind(self, writes, blog, write):
        """A lone surrogate, which JSON variables carry and UTF-8 cannot."""
        before = await self.state(writes, blog)
        with pytest.raises(Exception) as raised:
            await self.attempt(writes, blog, writes.new_orm(), write, name="\ud800")
        assert isinstance(raised.value, ValueError), repr(raised.value)
        assert str(raised.value) in PLAIN, str(raised.value)
        assert await self.state(writes, blog) == before

    @pytest.mark.parametrize("write", WRITES)
    async def test_a_write_that_does_not_fail_is_made(self, writes, blog, write):
        before = await self.state(writes, blog)
        await self.attempt(writes, blog, writes.new_orm(), write)
        assert await self.state(writes, blog) != before
