"""Guards on the windowed connection pass, exercised directly.

Each of these decides between one query for every parent and a correct but
slower fall back to one per parent. They are reached through combinations the
backend suites do not naturally produce, so they are driven here instead.
"""

import types

import pytest

from strawberry_orm.batching import (
    _CONNECTION_KEY,
    _UNBATCHABLE,
    RelationConnectionExtension,
    _already_loaded,
    _Bail,
    extensions_include_relation_connections,
    page_attr,
    take_page,
)


class _Parent:
    def __init__(self, pk):
        self.pk = pk


class _Backend:
    """Enough of a backend for the guards; each test bends one answer."""

    def __init__(self, *, pks=(1, 2), rows=None, totals=None, awaitable=False):
        self._pks = list(pks)
        self._rows = rows if rows is not None else {}
        self._totals = totals if totals is not None else {}
        self._awaitable = awaitable

    def instance_pk(self, parent):
        return parent.pk

    def relation_base_query(self, spec, pks, info):
        return "query"

    def batch_group_items(self, *a, **kw):
        if self._awaitable:

            async def _later():
                return self._rows

            return _later()
        return self._rows

    def group_counts(self, *a, **kw):
        return self._totals


class _Edge:
    CURSOR_PREFIX = "arrayconnection"


class _Connection:
    @classmethod
    def _edge_type(cls):
        return _Edge


def _info(path="users.posts"):
    """A resolve info carrying a path and a connection field to read it off."""
    from graphql.pyutils import Path
    from strawberry.relay.fields import ConnectionExtension

    parts = path.split(".")
    node_path = None
    for part in parts:
        node_path = Path(node_path, part, None)
    extension = ConnectionExtension(max_results=100)
    extension.connection_type = _Connection
    field = types.SimpleNamespace(
        extensions={
            "strawberry-definition": types.SimpleNamespace(extensions=[extension])
        }
    )
    return types.SimpleNamespace(
        path=node_path,
        field_name=parts[-1],
        parent_type=types.SimpleNamespace(fields={parts[-1]: field}),
    )


def _extension(backend):
    cls = RelationConnectionExtension.configure(backend, store=None)
    ext = cls()
    ext.execution_context = types.SimpleNamespace()
    return ext


class TestSiblings:
    def test_a_root_field_has_no_parents_to_batch_across(self):
        ext = _extension(_Backend())
        assert ext._siblings(ext.execution_context, _Parent(1), "users") is None

    def test_one_parent_is_not_worth_a_window(self):
        ext = _extension(_Backend())
        parent = _Parent(1)
        ext.execution_context._orm_batch_parents = {"users": [parent]}
        assert ext._siblings(ext.execution_context, parent, "users.posts") is None

    def test_a_parent_from_another_branch_is_not_batched_with_these(self):
        """Same path, different parent list: rewriting across them would be wrong."""
        ext = _extension(_Backend())
        stored = [_Parent(1), _Parent(2)]
        ext.execution_context._orm_batch_parents = {"users": stored}
        assert ext._siblings(ext.execution_context, _Parent(9), "users.posts") is None

    def test_no_parents_recorded_at_all(self):
        ext = _extension(_Backend())
        assert ext._siblings(ext.execution_context, _Parent(1), "users.posts") is None


class TestFetchGuards:
    def test_a_parent_without_a_key_cannot_be_grouped(self):
        ext = _extension(_Backend())
        with pytest.raises(_Bail):
            ext._fetch_pages(ext._backend, object(), [_Parent(None)], _info())

    def test_no_page_size_means_nothing_to_window(self):
        ext = _extension(_Backend())
        with pytest.raises(_Bail):
            ext._fetch_pages(ext._backend, object(), [_Parent(1)], _info())

    async def test_an_awaitable_page_is_awaited_not_refused(self):
        """An async backend windows too; the reads are just awaited."""
        backend = _Backend(awaitable=True, rows={("1",): ["row"]}, totals={1: 4})
        ext = _extension(backend)
        spec = types.SimpleNamespace(key_field="author_id", related_model=object)
        pending = ext._fetch_pages(
            backend, spec, [_Parent(1)], _info(), kwargs={"first": 1}
        )
        pages = await pending
        assert list(pages.values())[0].orm_total_count == 4

    async def test_siblings_share_one_read_rather_than_one_each(self):
        """Cache the coroutine and each sibling starts the N+1 this collapses."""
        reads = {"n": 0}

        class _Counting(_Backend):
            def batch_group_items(self, *a, **kw):
                async def _later():
                    reads["n"] += 1
                    return {("1",): ["row"], ("2",): ["row"]}

                return _later()

        import asyncio

        backend = _Counting(totals={1: 1, 2: 1})
        backend.relation_connection_spec = lambda info: types.SimpleNamespace(
            key_field="author_id", related_model=object, field_name="posts"
        )
        ext = _extension(backend)
        parents = [_Parent(1), _Parent(2)]
        ext.execution_context._orm_batch_parents = {"users": parents}
        info = _info()
        handed = []

        async def _next(root, info, *a, **k):
            handed.append(take_page(root, "posts", info))
            return root

        await asyncio.gather(*(ext.resolve(_next, p, info, first=1) for p in parents))
        assert reads["n"] == 1, "each sibling started its own read"
        assert all(handed), "a sibling was not handed its page"
        assert all(not hasattr(p, page_attr("posts")) for p in parents), (
            "a page outlived the field"
        )

    def test_a_field_that_is_not_a_connection_is_not_windowed(self):
        ext = _extension(_Backend())
        info = types.SimpleNamespace(
            path=_info().path,
            field_name="posts",
            parent_type=types.SimpleNamespace(fields={}),
        )
        with pytest.raises(_Bail, match="not a connection"):
            ext._fetch_pages(
                ext._backend, object(), [_Parent(1)], info, kwargs={"first": 1}
            )

    def test_a_cursor_before_the_first_row_is_not_windowed(self):
        """Python slices a negative position from the list's end; a window cannot."""
        import base64

        ext = _extension(_Backend())
        cursor = base64.b64encode(b"arrayconnection:-5").decode()
        with pytest.raises(_Bail, match="before the first row"):
            ext._fetch_pages(
                ext._backend,
                object(),
                [_Parent(1)],
                _info(),
                kwargs={"first": 1, "after": cursor},
            )

    def test_the_window_starts_at_the_cursor(self):
        """Rows before after= are not read: a forged cursor would read them all."""
        seen = {}

        class _Recording(_Backend):
            def batch_group_items(self, *a, **kw):
                seen["offset"] = kw["per_group_offset"]
                seen["limit"] = kw["per_group_limit"]
                return {("1",): ["row"]}

        backend = _Recording(totals={1: 7})
        ext = _extension(backend)
        spec = types.SimpleNamespace(key_field="author_id", related_model=object)
        import base64

        cursor = base64.b64encode(b"arrayconnection:2").decode()
        pages = ext._fetch_pages(
            backend, spec, [_Parent(1)], _info(), kwargs={"first": 3, "after": cursor}
        )
        # The page plus the row that answers hasNextPage, from just past it.
        assert seen == {"offset": 3, "limit": 3 + 1}
        page = list(pages.values())[0]
        assert (page.orm_offset, page.orm_total_count) == (3, 7)


class TestResolveGuards:
    def test_without_an_execution_context_there_is_nowhere_to_cache(self):
        """Nothing to share between siblings, so each resolves on its own."""
        backend = _Backend()
        ext = _extension(backend)
        ext.execution_context = None
        backend.relation_connection_spec = lambda info: object()

        called = []
        ext.resolve(lambda *a, **k: called.append(1), _Parent(1), _info())
        assert called == [1]

    def test_a_path_that_already_failed_is_not_retried(self):
        """One parent proving it unbatchable spares the rest the attempt."""
        backend = _Backend()
        ext = _extension(backend)
        backend.relation_connection_spec = lambda info: object()
        setattr(ext.execution_context, _CONNECTION_KEY, {"users.posts": _UNBATCHABLE})

        called = []
        ext.resolve(lambda *a, **k: called.append(1), _Parent(1), _info())
        assert called == [1]


def test_the_pass_is_installed_only_once():
    assert extensions_include_relation_connections([RelationConnectionExtension])
    configured = RelationConnectionExtension.configure(_Backend(), store=None)
    assert extensions_include_relation_connections([configured])
    assert not extensions_include_relation_connections([object])

    # A configured pass that is not a subclass is recognised by its name.
    named = type("RelationConnectionExtension_Fake", (), {})
    assert extensions_include_relation_connections([named])


def test_the_page_is_left_where_the_resolver_looks_for_it():
    assert page_attr("posts").endswith("posts")


def test_a_field_with_no_discoverable_name_is_not_treated_as_loaded():
    """Without a name there is nothing to probe, so nothing is assumed."""
    nameless = types.SimpleNamespace(path=None, python_name=None)
    assert _already_loaded(object(), nameless) is False
