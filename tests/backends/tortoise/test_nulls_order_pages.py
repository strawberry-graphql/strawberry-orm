"""A page of rows ordered with their nulls first or last holds the right rows.

Tortoise cannot order nulls first or last itself. Left to Python, that order
came after a page had been cut by the rest of it and the primary key, so the
page held the wrong rows. Paging through a connection, or a root list cut to
``default_query_limit``, reads what one unpaged read does.
"""

import pytest
import pytest_asyncio
import strawberry
from strawberry import relay
from tortoise import Tortoise, fields
from tortoise.models import Model

from strawberry_orm import StrawberryORM, auto
from strawberry_orm.relay import ORMListConnection


class RaShelf(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "ra_shelf"


class RaBook(Model):
    id = fields.IntField(primary_key=True)
    rank = fields.IntField(null=True)
    shelf = fields.ForeignKeyField("ra_nulls_order.RaShelf", related_name="books")

    class Meta:
        table = "ra_book"


RANKS = [3, None, 1, None, 2, 5, None, 4]
LABELS = ["b", None, "a"]
DIRECTIONS = [
    "ASC_NULLS_FIRST",
    "ASC_NULLS_LAST",
    "DESC_NULLS_FIRST",
    "DESC_NULLS_LAST",
]


@pytest_asyncio.fixture
async def books():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"ra_nulls_order": [__name__]}
    )
    await Tortoise.generate_schemas()
    shelves = [
        await RaShelf.create(id=number, label=label)
        for number, label in enumerate(LABELS, 1)
    ]
    for number, rank in enumerate(RANKS, 1):
        await RaBook.create(id=number, rank=rank, shelf=shelves[number % 3])
    yield
    await Tortoise.close_connections()


def build_schema(**options):
    orm = StrawberryORM.for_tortoise(
        lazy_resolution="off", warn_missing_scope=False, **options
    )

    @orm.type(RaShelf, order=orm.order(RaShelf))
    class ShelfType:
        id: auto
        label: auto

    @orm.type(RaBook, order=orm.order(RaBook))
    class BookNode(relay.Node):
        id: relay.NodeID[int]
        rank: auto
        shelf: ShelfType

        @strawberry.field
        def number(self) -> int:
            return self.pk

    @strawberry.type
    class Query:
        books: list[BookNode] = orm.field.eager()
        pages: ORMListConnection[BookNode] = orm.connection()

    return orm.schema(query=Query)


def expected(direction, values):
    """Book numbers in *direction* over *values*, ties in number order."""
    numbers = range(1, len(values) + 1)
    nulls = [n for n in numbers if values[n - 1] is None]
    rest = sorted(
        (n for n in numbers if values[n - 1] is not None),
        key=lambda n: values[n - 1],
        reverse=direction.startswith("DESC"),
    )
    return nulls + rest if direction.endswith("NULLS_FIRST") else rest + nulls


def by_rank(direction):
    return f"[{{field: {{rank: {direction}}}}}, {{field: {{id: ASC}}}}]"


def by_shelf_label(direction):
    return (
        f"[{{object: {{shelf: {{field: {{label: {direction}}}}}}}}}, "
        f"{{field: {{id: ASC}}}}]"
    )


ORDERS = [
    pytest.param(by_rank, RANKS, id="column"),
    pytest.param(
        by_shelf_label,
        [LABELS[number % 3] for number in range(1, len(RANKS) + 1)],
        id="relation",
    ),
]


async def execute(schema, query):
    result = await schema.execute(query)
    assert result.errors is None, result.errors
    return result.data


async def read_pages(schema, order, size):
    numbers, after = [], None
    while True:
        cursor = f', after: "{after}"' if after else ""
        data = await execute(
            schema,
            f"{{ pages(first: {size}{cursor}, order: {order}) {{"
            f" edges {{ node {{ number }} }} pageInfo {{ hasNextPage endCursor }} }} }}",
        )
        page = data["pages"]
        numbers.append([edge["node"]["number"] for edge in page["edges"]])
        if not page["pageInfo"]["hasNextPage"]:
            return numbers
        after = page["pageInfo"]["endCursor"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("order", "values"), ORDERS)
@pytest.mark.parametrize("direction", DIRECTIONS)
async def test_connection_pages_read_as_one_read(books, order, values, direction):
    schema = build_schema()
    data = await execute(schema, f"{{ books(order: {order(direction)}) {{ number }} }}")
    read = [book["number"] for book in data["books"]]
    assert read == expected(direction, values)

    pages = await read_pages(schema, order(direction), 3)
    assert pages == [read[:3], read[3:6], read[6:]]


@pytest.mark.asyncio
@pytest.mark.parametrize(("order", "values"), ORDERS)
@pytest.mark.parametrize("direction", DIRECTIONS)
async def test_a_limited_list_holds_the_first_rows(books, order, values, direction):
    schema = build_schema(default_query_limit=3)
    data = await execute(schema, f"{{ books(order: {order(direction)}) {{ number }} }}")
    assert [book["number"] for book in data["books"]] == expected(direction, values)[:3]
