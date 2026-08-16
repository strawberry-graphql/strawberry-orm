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
    _Bail,
    extensions_include_relation_connections,
    page_attr,
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


def _info(path="users.posts", args=None):
    """A resolve info carrying just a path and the field's arguments."""
    node = types.SimpleNamespace(arguments=args or ())
    parts = path.split(".")
    node_path = None
    for part in parts:
        node_path = types.SimpleNamespace(key=part, prev=node_path)
    return types.SimpleNamespace(
        path=node_path,
        _raw_info=types.SimpleNamespace(field_nodes=[node]),
        field_nodes=[node],
    )


def _extension(backend):
    cls = RelationConnectionExtension.configure(backend, store=None)
    ext = cls()
    ext.execution_context = types.SimpleNamespace()
    return ext


def _arg(name, value, kind="int"):
    from graphql.language import IntValueNode, NameNode, StringValueNode

    node = (
        IntValueNode(value=str(value))
        if kind == "int"
        else StringValueNode(value=str(value))
    )
    return types.SimpleNamespace(name=NameNode(value=name), value=node)


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

    def test_an_awaitable_page_falls_back(self):
        """Async backends resolve per parent rather than block here."""
        backend = _Backend(awaitable=True)
        ext = _extension(backend)
        spec = types.SimpleNamespace(key_field="author_id", related_model=object)
        info = _info(args=[_arg("first", 1)])
        with pytest.raises(_Bail):
            ext._fetch_pages(backend, spec, [_Parent(1)], info)

    def test_a_cursor_widens_the_window(self):
        """after= is applied by Relay, so the window has to reach past it."""
        seen = {}

        class _Recording(_Backend):
            def batch_group_items(self, *a, **kw):
                seen["limit"] = kw["per_group_limit"]
                return {("1",): ["row"]}

        backend = _Recording(totals={1: 7})
        ext = _extension(backend)
        spec = types.SimpleNamespace(key_field="author_id", related_model=object)
        import base64

        cursor = base64.b64encode(b"arrayconnection:2").decode()
        info = _info(args=[_arg("first", 3), _arg("after", cursor, kind="string")])

        pages = ext._fetch_pages(backend, spec, [_Parent(1)], info)
        assert seen["limit"] == 3 + 3 + 1, "the cursor offset was not included"
        assert list(pages.values())[0].orm_total_count == 7


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
