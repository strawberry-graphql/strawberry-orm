"""Every read a client can page, and every root list, is cut by the database.

A connection reads one page (plus the row answering ``hasNextPage``), held to
``first``/``last``, ``max_results`` and ``default_query_limit``, and a root
list reads at most ``default_query_limit`` rows, however the field is declared
and whatever the schema runs with: the optimizer on or off, relation batching
on or off, ``lazy_resolution`` watching or not, sync or async. The rows stay
the ones ``scope_rows`` lets through, and no statement checks the scope again
with one bound parameter per row read.

A nested relation list returns all of its parent's visible rows, and a root
list whose resolver takes no filter or order returns every visible row of the
query it returns, as documented; those are checked for their rows only.

Backends supply:

``make_orm(**options)``     an ORM with ``default_query_limit=5`` and *options*
``Owner``, ``Item``         the models; an item has ``name``, ``bucket``,
                            ``hidden`` and ``owner`` (``Owner.items``)
``hide(query)``             *query* without the hidden items
``all_items()``             a query over every item
``owner_items(owner)``      a query over *owner*'s items
``seeded``                  three owners of ten items each; items 2 and 15
                            hidden
``capture()``               context manager: ``.loaded``, the ``Item`` rows
                            built, and ``.params``, the parameters each
                            statement bound
``run(schema, query)``      awaitable result of running *query*
"""

import base64
from collections.abc import Iterable

import pytest
import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto

HIDDEN = {2, 15}
LIMIT = 5
#: No statement binds one parameter per row of the table.
MAX_PARAMS = 12

CONFIGS = {
    "optimizer": {"optimizer": True},
    "optimizer, no batching": {"optimizer": True, "batch_relations": False},
    "optimizer, lazy_resolution warn": {"optimizer": True, "lazy_resolution": "warn"},
    "no optimizer": {"optimizer": False},
    "no optimizer, lazy_resolution warn": {
        "optimizer": False,
        "lazy_resolution": "warn",
    },
}


def cursor(offset: int) -> str:
    return base64.b64encode(f"arrayconnection:{offset}".encode()).decode()


def name(item_id: int) -> str:
    return f"i{item_id:02}"


def visible(ids: Iterable[int]) -> list[int]:
    return [item_id for item_id in ids if item_id not in HIDDEN]


def names(ids: Iterable[int]) -> list[str]:
    return [name(item_id) for item_id in ids]


def edges(ids: Iterable[int]) -> list[dict]:
    return [{"node": {"name": name(item_id)}} for item_id in ids]


EVERY = visible(range(1, 31))
BUCKET_0 = [item_id for item_id in EVERY if item_id % 3 == 0]
OWNED = {owner: visible(range(owner * 10 - 9, owner * 10 + 1)) for owner in (1, 2, 3)}

PAGE = "{ totalCount edges { node { name } } }"
LIST = "{ name }"
FILTERED = "filter: {field: {bucket: {exact: 0}}}, order: [{field: {name: DESC}}]"


def _page(field: str, ids: list[int], total: int = len(EVERY)) -> dict:
    return {field: {"totalCount": total, "edges": edges(ids)}}


def _owner_pages(field: str) -> dict:
    return {
        "owners": [
            {
                "name": f"o{owner}",
                field: {"totalCount": len(ids), "edges": edges(ids[:3])},
            }
            for owner, ids in OWNED.items()
        ]
    }


#: ``(query, expected data, most Item rows the read may build)``. ``None``
#: for a read documented to return every row it is asked for. Rows a
#: resolver's field reads itself are checked against ``scope_rows`` once more
#: without the optimizer, which Tortoise does by reading them again.
KINDS = {
    "root connection": (
        f"{{ itemPage(first: 3) {PAGE} }}",
        _page("itemPage", EVERY[:3]),
        4,
    ),
    "root connection, no first": (
        "{ itemPage { edges { node { name } } } }",
        {"itemPage": {"edges": edges(EVERY[:LIMIT])}},
        LIMIT + 1,
    ),
    "root connection, first over the limit": (
        "{ itemPage(first: 50) { edges { node { name } } } }",
        {"itemPage": {"edges": edges(EVERY[:LIMIT])}},
        LIMIT + 1,
    ),
    "root connection, after": (
        f'{{ itemPage(first: 2, after: "{cursor(2)}") {PAGE} }}',
        _page("itemPage", EVERY[3:5]),
        3,
    ),
    "root connection, before": (
        f'{{ itemPage(before: "{cursor(20)}") {{ edges {{ node {{ name }} }} }} }}',
        {"itemPage": {"edges": edges(EVERY[:LIMIT])}},
        LIMIT + 1,
    ),
    "root connection, last": (
        f"{{ itemPage(last: 2) {PAGE} }}",
        _page("itemPage", EVERY[-2:]),
        3,
    ),
    "root connection, filtered and ordered": (
        f"{{ itemPage(first: 3, {FILTERED}) {PAGE} }}",
        _page("itemPage", BUCKET_0[::-1][:3], len(BUCKET_0)),
        4,
    ),
    "root connection with a resolver": (
        f"{{ itemPageResolved(first: 3) {PAGE} }}",
        _page("itemPageResolved", EVERY[:3]),
        4,
    ),
    "root connection.lazy": (
        f"{{ itemPageLazy(first: 3) {PAGE} }}",
        _page("itemPageLazy", EVERY[:3]),
        4,
    ),
    "root relay.connection": (
        f"{{ itemPageRelay(first: 3) {PAGE} }}",
        _page("itemPageRelay", EVERY[:3]),
        4,
    ),
    "root relay.connection, no first": (
        "{ itemPageRelay { edges { node { name } } } }",
        {"itemPageRelay": {"edges": edges(EVERY[:LIMIT])}},
        LIMIT + 1,
    ),
    "relation connection under a list": (
        f"{{ owners {{ name items(first: 3) {PAGE} }} }}",
        _owner_pages("items"),
        3 * 4,
    ),
    "relation connection.lazy under a list": (
        f"{{ owners {{ name lazyItems(first: 3) {PAGE} }} }}",
        _owner_pages("lazyItems"),
        3 * 4,
    ),
    "connection on a plain type under a list": (
        "{ shelves { items(first: 3) { edges { node { name } } } } }",
        {"shelves": [{"items": {"edges": edges(EVERY[:3])}}] * 2},
        2 * 4,
    ),
    "group items": (
        "{ itemPage(first: 1, groupBy: [{field: {bucket: true}}]) "
        "{ groups { key { bucket } items(first: 2) { edges { node { name } } } } } }",
        {
            "itemPage": {
                "groups": [
                    {
                        "key": {"bucket": str(bucket)},
                        "items": {
                            "edges": edges(
                                [i for i in EVERY if i % 3 == bucket][:2],
                            )
                        },
                    }
                    for bucket in (0, 1, 2)
                ]
            }
        },
        2 + 3 * 3,
    ),
    "root list": (
        f"{{ itemList {LIST} }}",
        {"itemList": [{"name": n} for n in names(EVERY[:LIMIT])]},
        LIMIT,
    ),
    "root list, filtered and ordered": (
        f"{{ itemList({FILTERED}) {LIST} }}",
        {"itemList": [{"name": n} for n in names(BUCKET_0[::-1][:LIMIT])]},
        LIMIT,
    ),
    "root list, eager resolver": (
        f"{{ itemListEager {LIST} }}",
        {"itemListEager": [{"name": n} for n in names(EVERY[:LIMIT])]},
        2 * LIMIT,
    ),
    "root list, lazy resolver (documented as returned)": (
        f"{{ itemListLazy {LIST} }}",
        {"itemListLazy": [{"name": n} for n in names(EVERY)]},
        None,
    ),
    "root list, lazy resolver, filtered and ordered": (
        f"{{ itemListFiltered({FILTERED}) {LIST} }}",
        {"itemListFiltered": [{"name": n} for n in names(BUCKET_0[::-1][:LIMIT])]},
        2 * LIMIT,
    ),
    "root list, strawberry.field (documented as returned)": (
        f"{{ itemListPlain {LIST} }}",
        {"itemListPlain": [{"name": n} for n in names(EVERY)]},
        None,
    ),
    "relation list (documented whole)": (
        f"{{ ownerRows {{ name items {LIST} }} }}",
        {
            "ownerRows": [
                {"name": f"o{owner}", "items": [{"name": n} for n in names(ids)]}
                for owner, ids in OWNED.items()
            ]
        },
        None,
    ),
}


def build(orm, Owner, Item, hide, all_items, owner_items, *, optimizer):
    ItemFilter = orm.filter(Item)
    ItemOrder = orm.order(Item)

    @orm.type(Item, filters=ItemFilter, order=ItemOrder, group=orm.group(Item))
    class UbItemNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        bucket: auto

        @classmethod
        def scope_rows(cls, query, info):
            return hide(query)

    def resolve_owner_items(self, info: strawberry.Info) -> Iterable[UbItemNode]:
        return owner_items(self)

    @orm.type(Owner)
    class UbOwnerNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        items: ORMListConnection[UbItemNode] = orm.connection.eager()
        lazy_items: ORMListConnection[UbItemNode] = orm.connection.lazy(
            resolver=resolve_owner_items
        )

    @orm.type(Owner)
    class UbOwnerRow:
        id: auto
        name: auto
        items: list[UbItemNode]

    @strawberry.type
    class UbShelf:
        items: ORMListConnection[UbItemNode] = orm.connection.eager()

    def every_item(self, info: strawberry.Info) -> Iterable[UbItemNode]:
        return all_items()

    @strawberry.type
    class Query:
        item_page: ORMListConnection[UbItemNode] = orm.connection.eager()
        item_page_lazy: ORMListConnection[UbItemNode] = orm.connection.lazy(
            resolver=every_item
        )
        item_list: list[UbItemNode] = orm.field.eager()
        owners: list[UbOwnerNode] = orm.field.eager()
        owner_rows: list[UbOwnerRow] = orm.field.eager()

        @orm.connection(ORMListConnection[UbItemNode])
        def item_page_resolved(self, info: strawberry.Info) -> Iterable[UbItemNode]:
            return all_items()

        @relay.connection(ORMListConnection[UbItemNode])
        def item_page_relay(self, info: strawberry.Info) -> Iterable[UbItemNode]:
            return all_items()

        @orm.field.eager()
        def item_list_eager(self, info: strawberry.Info) -> list[UbItemNode]:
            return all_items()

        @orm.field.lazy
        def item_list_lazy(self, info: strawberry.Info) -> list[UbItemNode]:
            return all_items()

        @orm.field.lazy(filters=ItemFilter, order=ItemOrder)
        def item_list_filtered(self, info: strawberry.Info) -> list[UbItemNode]:
            return all_items()

        @strawberry.field
        def item_list_plain(self, info: strawberry.Info) -> list[UbItemNode]:
            return all_items()

        @strawberry.field
        def shelves(self) -> list[UbShelf]:
            return [UbShelf(), UbShelf()]

    return orm.schema(query=Query, optimizer=optimizer)


class AbstractTestBoundedReads:
    @pytest.mark.parametrize("config", list(CONFIGS))
    @pytest.mark.parametrize("kind", list(KINDS))
    async def test_a_read_is_cut_to_its_page_and_scoped(
        self,
        kind,
        config,
        make_orm,
        Owner,
        Item,
        hide,
        all_items,
        owner_items,
        seeded,
        capture,
        run,
    ):
        options = dict(CONFIGS[config])
        optimizer = options.pop("optimizer")
        schema = build(
            make_orm(**options),
            Owner,
            Item,
            hide,
            all_items,
            owner_items,
            optimizer=optimizer,
        )
        query, expected, most = KINDS[kind]
        with capture() as seen:
            result = await run(schema, query)
        assert result.errors is None, result.errors
        assert result.data == expected, result.data
        if most is not None:
            assert seen.loaded <= most, seen.loaded
            assert max(seen.params, default=0) <= MAX_PARAMS, seen.params
