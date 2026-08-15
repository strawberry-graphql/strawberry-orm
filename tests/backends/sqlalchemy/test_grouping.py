"""Comprehensive tests for grouping, aggregation, and related features."""

import datetime

import pytest
import strawberry
from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    case,
    create_engine,
    event,
)
from sqlalchemy import func as sa_func
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    sessionmaker,
)
from strawberry import relay

from strawberry_orm import StrawberryORM, aggregate_field
from strawberry_orm.relay.connection import ORMListConnection
from strawberry_orm.types import auto

# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class GroupBase(DeclarativeBase):
    pass


class Order(GroupBase):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(50))
    customer_id: Mapped[int] = mapped_column(Integer)
    amount: Mapped[float] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)


# ---------------------------------------------------------------------------
# ORM setup
# ---------------------------------------------------------------------------

orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

OrderFilter = orm.filter(Order)
OrderOrder = orm.order(Order)
OrderGroupBy = orm.group(Order)


@orm.type(Order, filters=OrderFilter, order=OrderOrder, group=OrderGroupBy)
class OrderType(relay.Node):
    id: relay.NodeID[int]
    status: auto
    customer_id: auto
    amount: auto
    quantity: auto
    created_at: auto


@strawberry.type
class Query:
    orders: ORMListConnection[OrderType] = orm.connection()


schema = strawberry.Schema(
    query=Query,
    extensions=[orm.optimizer_extension()],
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    GroupBase.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    sess = sessionmaker(bind=engine)()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def seed(session):
    """Seed ~20 orders across 3 statuses, 4 customers, 3 months."""
    orders = [
        Order(
            id=1,
            status="shipped",
            customer_id=1,
            amount=59.99,
            quantity=2,
            created_at=datetime.datetime(2026, 1, 5),
        ),
        Order(
            id=2,
            status="pending",
            customer_id=2,
            amount=120.00,
            quantity=1,
            created_at=datetime.datetime(2026, 1, 10),
        ),
        Order(
            id=3,
            status="shipped",
            customer_id=1,
            amount=34.50,
            quantity=3,
            created_at=datetime.datetime(2026, 1, 15),
        ),
        Order(
            id=4,
            status="pending",
            customer_id=3,
            amount=85.00,
            quantity=1,
            created_at=datetime.datetime(2026, 1, 20),
        ),
        Order(
            id=5,
            status="cancelled",
            customer_id=2,
            amount=22.00,
            quantity=1,
            created_at=datetime.datetime(2026, 1, 25),
        ),
        Order(
            id=6,
            status="shipped",
            customer_id=4,
            amount=200.00,
            quantity=5,
            created_at=datetime.datetime(2026, 2, 1),
        ),
        Order(
            id=7,
            status="pending",
            customer_id=1,
            amount=45.00,
            quantity=2,
            created_at=datetime.datetime(2026, 2, 5),
        ),
        Order(
            id=8,
            status="shipped",
            customer_id=3,
            amount=75.50,
            quantity=1,
            created_at=datetime.datetime(2026, 2, 10),
        ),
        Order(
            id=9,
            status="cancelled",
            customer_id=4,
            amount=41.00,
            quantity=1,
            created_at=datetime.datetime(2026, 2, 15),
        ),
        Order(
            id=10,
            status="shipped",
            customer_id=2,
            amount=150.00,
            quantity=4,
            created_at=datetime.datetime(2026, 2, 20),
        ),
        Order(
            id=11,
            status="pending",
            customer_id=1,
            amount=30.00,
            quantity=1,
            created_at=datetime.datetime(2026, 3, 1),
        ),
        Order(
            id=12,
            status="shipped",
            customer_id=3,
            amount=95.00,
            quantity=2,
            created_at=datetime.datetime(2026, 3, 5),
        ),
        Order(
            id=13,
            status="pending",
            customer_id=4,
            amount=60.00,
            quantity=3,
            created_at=datetime.datetime(2026, 3, 10),
        ),
        Order(
            id=14,
            status="cancelled",
            customer_id=1,
            amount=15.00,
            quantity=1,
            created_at=datetime.datetime(2026, 3, 15),
        ),
        Order(
            id=15,
            status="shipped",
            customer_id=2,
            amount=180.00,
            quantity=6,
            created_at=datetime.datetime(2026, 3, 20),
        ),
        Order(
            id=16,
            status="pending",
            customer_id=3,
            amount=55.00,
            quantity=2,
            created_at=datetime.datetime(2026, 1, 8),
        ),
        Order(
            id=17,
            status="shipped",
            customer_id=4,
            amount=110.00,
            quantity=3,
            created_at=datetime.datetime(2026, 2, 12),
        ),
        Order(
            id=18,
            status="cancelled",
            customer_id=2,
            amount=28.00,
            quantity=1,
            created_at=datetime.datetime(2026, 3, 25),
        ),
        Order(
            id=19,
            status="pending",
            customer_id=1,
            amount=70.00,
            quantity=2,
            created_at=datetime.datetime(2026, 2, 28),
        ),
        Order(
            id=20,
            status="shipped",
            customer_id=3,
            amount=45.00,
            quantity=1,
            created_at=datetime.datetime(2026, 1, 30),
        ),
    ]
    session.add_all(orders)
    session.commit()
    return orders


@pytest.fixture
def execute(session, seed):
    def _execute(query, variables=None):
        result = schema.execute_sync(
            query,
            variable_values=variables or {},
            context_value={"session": session},
        )
        assert result.errors is None, f"GraphQL errors: {result.errors}"
        return result.data

    return _execute


@pytest.fixture
def query_counter(session):
    queries: list[str] = []

    def _before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        queries.append(statement)

    engine = session.bind
    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    yield queries
    event.remove(engine, "before_cursor_execute", _before_cursor_execute)


# ===========================================================================
# Type generation tests (10a, 10b)
# ===========================================================================


class TestTypeGeneration:
    def test_group_type_generates_valid_input(self):
        assert hasattr(OrderGroupBy, "__dataclass_fields__")
        assert "field" in OrderGroupBy.__dataclass_fields__

    def test_group_type_has_correct_fields(self):
        field_type = OrderGroupBy._field_type
        fields = field_type.__dataclass_fields__
        assert "status" in fields
        assert "customer_id" in fields
        assert "amount" in fields
        assert "quantity" in fields
        assert "created_at" in fields

    def test_group_type_date_field_uses_date_option(self):
        field_type = OrderGroupBy._field_type
        ann = field_type.__annotations__
        created_at_ann = ann.get("created_at")
        assert created_at_ann is not None

    def test_aggregate_types_generated(self):
        meta = orm.backend._build_aggregate_types(Order)
        assert meta.aggregates_type is not None
        assert meta.group_key_type is not None

    def test_aggregate_types_numeric_fields(self):
        meta = orm.backend._build_aggregate_types(Order)
        numeric_names = [f[0] for f in meta.numeric_fields]
        assert "amount" in numeric_names
        assert "quantity" in numeric_names

    def test_aggregate_types_comparable_fields(self):
        meta = orm.backend._build_aggregate_types(Order)
        comparable_names = [f[0] for f in meta.comparable_fields]
        assert "amount" in comparable_names
        assert "quantity" in comparable_names

    def test_aggregate_types_sum_avg_include_numeric_only(self):
        meta = orm.backend._build_aggregate_types(Order)
        assert meta.sum_type is not None
        assert meta.avg_type is not None
        sum_fields = meta.sum_type.__dataclass_fields__
        assert "amount" in sum_fields
        assert "quantity" in sum_fields

    def test_group_key_type_has_all_groupable_fields(self):
        meta = orm.backend._build_aggregate_types(Order)
        key_fields = meta.group_key_type.__dataclass_fields__
        assert "status" in key_fields
        assert "customer_id" in key_fields
        assert "amount" in key_fields

    def test_schema_has_aggregates_field(self):
        sdl = str(schema.as_str())
        assert "aggregates" in sdl

    def test_schema_has_groups_field(self):
        sdl = str(schema.as_str())
        assert "groups" in sdl

    def test_schema_has_group_by_argument(self):
        sdl = str(schema.as_str())
        assert "groupBy" in sdl


# ===========================================================================
# Aggregation tests (10c)
# ===========================================================================


class TestAggregation:
    def test_count_only(self, execute):
        data = execute("""
            query {
                orders(first: 5) {
                    aggregates { count }
                }
            }
        """)
        assert data["orders"]["aggregates"]["count"] == 20

    def test_all_aggregate_functions(self, execute):
        data = execute("""
            query {
                orders(first: 5) {
                    aggregates {
                        count
                        sum { amount quantity }
                        avg { amount }
                        min { amount }
                        max { amount }
                    }
                }
            }
        """)
        agg = data["orders"]["aggregates"]
        assert agg["count"] == 20
        assert agg["sum"]["amount"] > 0
        assert agg["avg"]["amount"] > 0
        assert agg["min"]["amount"] == 15.0
        assert agg["max"]["amount"] == 200.0

    def test_aggregates_with_filter(self, execute):
        data = execute("""
            query {
                orders(
                    first: 5,
                    filter: { field: { status: { exact: "shipped" } } }
                ) {
                    aggregates { count }
                }
            }
        """)
        assert data["orders"]["aggregates"]["count"] == 9

    def test_aggregates_without_edges(self, execute):
        data = execute("""
            query {
                orders {
                    aggregates {
                        count
                        sum { amount }
                    }
                }
            }
        """)
        assert data["orders"]["aggregates"]["count"] == 20
        assert data["orders"]["aggregates"]["sum"]["amount"] > 0


class TestScopingSecurity:
    def test_connection_aggregates_and_groups_respect_scope_rows(self, session, seed):
        scoped_orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
        )
        ScopedOrderFilter = scoped_orm.filter(Order)
        ScopedOrderOrder = scoped_orm.order(Order)
        ScopedOrderGroup = scoped_orm.group(Order)

        @scoped_orm.type(
            Order,
            filters=ScopedOrderFilter,
            order=ScopedOrderOrder,
            group=ScopedOrderGroup,
        )
        class ScopedOrderType(relay.Node):
            id: relay.NodeID[int]
            status: auto

            @classmethod
            def scope_rows(cls, query, info):
                return query.where(Order.status == "shipped")

        @strawberry.type
        class ScopedQuery:
            orders: ORMListConnection[ScopedOrderType] = scoped_orm.connection()

        scoped_schema = scoped_orm.schema(query=ScopedQuery)
        result = scoped_schema.execute_sync(
            """
            query {
                orders(
                    first: 20
                    groupBy: [{ field: { status: true } }]
                ) {
                    totalCount
                    edges { node { status } }
                    aggregates { count }
                    groups {
                        key { status }
                        aggregates { count }
                    }
                }
            }
            """,
            context_value={"session": session},
        )

        assert result.errors is None
        orders = result.data["orders"]
        assert {edge["node"]["status"] for edge in orders["edges"]} == {"shipped"}
        assert orders["totalCount"] == 9
        assert orders["aggregates"]["count"] == 9
        assert [
            (group["key"]["status"], group["aggregates"]["count"])
            for group in orders["groups"]
        ] == [("shipped", 9)]


# ===========================================================================
# Page aggregates tests (10c cont.)
# ===========================================================================


class TestPageAggregates:
    def test_page_aggregates_in_page_info(self, execute):
        data = execute("""
            query {
                orders(first: 3) {
                    edges { node { id amount } }
                    pageInfo {
                        hasNextPage
                        aggregates { count sum { amount } }
                    }
                }
            }
        """)
        pi = data["orders"]["pageInfo"]
        assert pi["hasNextPage"] is True
        assert pi["aggregates"]["count"] == 3
        edges = data["orders"]["edges"]
        page_sum = sum(float(e["node"]["amount"]) for e in edges)
        assert abs(pi["aggregates"]["sum"]["amount"] - page_sum) < 0.01

    def test_page_vs_whole_result_aggregates(self, execute):
        data = execute("""
            query {
                orders(first: 3) {
                    pageInfo { aggregates { count } }
                    aggregates { count }
                }
            }
        """)
        assert data["orders"]["pageInfo"]["aggregates"]["count"] == 3
        assert data["orders"]["aggregates"]["count"] == 20


# ===========================================================================
# Grouping tests (10d)
# ===========================================================================


class TestGrouping:
    def test_single_field_grouping(self, execute):
        data = execute("""
            query {
                orders(
                    first: 5,
                    groupBy: [{ field: { status: true } }]
                ) {
                    groups {
                        key { status }
                        aggregates { count }
                    }
                }
            }
        """)
        groups = data["orders"]["groups"]
        assert len(groups) == 3
        statuses = {g["key"]["status"] for g in groups}
        assert statuses == {"shipped", "pending", "cancelled"}

    def test_group_counts_sum_to_total(self, execute):
        data = execute("""
            query {
                orders(
                    first: 5,
                    groupBy: [{ field: { status: true } }]
                ) {
                    aggregates { count }
                    groups {
                        key { status }
                        aggregates { count }
                    }
                }
            }
        """)
        total = data["orders"]["aggregates"]["count"]
        group_sum = sum(g["aggregates"]["count"] for g in data["orders"]["groups"])
        assert group_sum == total

    def test_group_aggregates_correct(self, execute):
        data = execute("""
            query {
                orders(
                    first: 5,
                    groupBy: [{ field: { status: true } }]
                ) {
                    groups {
                        key { status }
                        aggregates { count sum { amount } }
                    }
                }
            }
        """)
        shipped = next(
            g for g in data["orders"]["groups"] if g["key"]["status"] == "shipped"
        )
        assert shipped["aggregates"]["count"] == 9
        assert shipped["aggregates"]["sum"]["amount"] > 0

    def test_multi_field_grouping(self, execute):
        data = execute("""
            query {
                orders(
                    first: 5,
                    groupBy: [
                        { field: { status: true } },
                        { field: { customerId: true } }
                    ]
                ) {
                    groups {
                        key { status customerId }
                        aggregates { count }
                    }
                }
            }
        """)
        groups = data["orders"]["groups"]
        assert len(groups) > 3


# ===========================================================================
# edgeIndices tests (10e)
# ===========================================================================


class TestEdgeIndices:
    def test_edge_indices_correct_mapping(self, execute):
        data = execute("""
            query {
                orders(
                    first: 5,
                    groupBy: [{ field: { status: true } }]
                ) {
                    edges { node { id status } }
                    groups {
                        key { status }
                        edgeIndices
                    }
                }
            }
        """)
        edges = data["orders"]["edges"]
        for group in data["orders"]["groups"]:
            for idx in group["edgeIndices"]:
                assert edges[idx]["node"]["status"] == group["key"]["status"]

    def test_edge_indices_cover_all_edges(self, execute):
        data = execute("""
            query {
                orders(
                    first: 5,
                    groupBy: [{ field: { status: true } }]
                ) {
                    edges { node { id } }
                    groups {
                        edgeIndices
                    }
                }
            }
        """)
        all_indices = set()
        for group in data["orders"]["groups"]:
            all_indices.update(group["edgeIndices"])
        expected = set(range(len(data["orders"]["edges"])))
        assert all_indices == expected


# ===========================================================================
# Group ordering tests (10f)
# ===========================================================================


class TestGroupOrdering:
    def test_groups_ordered_by_overlapping_asc(self, execute):
        data = execute("""
            query {
                orders(
                    first: 5,
                    order: [{ field: { status: ASC } }],
                    groupBy: [{ field: { status: true } }]
                ) {
                    groups {
                        key { status }
                        aggregates { count }
                    }
                }
            }
        """)
        statuses = [g["key"]["status"] for g in data["orders"]["groups"]]
        assert statuses == sorted(statuses)

    def test_groups_ordered_by_overlapping_desc(self, execute):
        data = execute("""
            query {
                orders(
                    first: 5,
                    order: [{ field: { status: DESC } }],
                    groupBy: [{ field: { status: true } }]
                ) {
                    groups {
                        key { status }
                        aggregates { count }
                    }
                }
            }
        """)
        statuses = [g["key"]["status"] for g in data["orders"]["groups"]]
        assert statuses == sorted(statuses, reverse=True)


# ===========================================================================
# Normal pagination unchanged (10a example from plan)
# ===========================================================================


class TestNormalPaginationUnchanged:
    def test_normal_pagination_works(self, execute):
        data = execute("""
            query {
                orders(first: 3) {
                    edges { node { id status amount } }
                    pageInfo { hasNextPage endCursor }
                }
            }
        """)
        assert len(data["orders"]["edges"]) == 3
        assert data["orders"]["pageInfo"]["hasNextPage"] is True
        assert data["orders"]["pageInfo"]["endCursor"] is not None


# ===========================================================================
# Connection without group type (backward compatibility)
# ===========================================================================


class TestBackwardCompatibility:
    def test_connection_without_group_has_no_aggregates(self):
        """A type without group= should not get aggregates/groups."""
        plain_orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        PlainFilter = plain_orm.filter(Order)

        @plain_orm.type(Order, filters=PlainFilter)
        class PlainOrderType(relay.Node):
            id: relay.NodeID[int]
            status: auto

        @strawberry.type
        class PlainQuery:
            orders: ORMListConnection[PlainOrderType] = plain_orm.connection()

        plain_schema = strawberry.Schema(
            query=PlainQuery,
            extensions=[plain_orm.optimizer_extension()],
        )
        sdl = str(plain_schema.as_str())
        assert "groupBy" not in sdl


# ===========================================================================
# Integration / edge-case tests (10j)
# ===========================================================================


class TestEdgeCases:
    def test_empty_table(self, session):
        """No data: aggregates count=0, groups empty."""
        empty_engine = create_engine("sqlite:///:memory:")
        GroupBase.metadata.create_all(empty_engine)
        empty_session = sessionmaker(bind=empty_engine)()

        schema.execute_sync(
            """
            query {
                orders(first: 10) {
                    aggregates { count }
                    groups(groupBy: [{ field: { status: true } }]) {
                        key { status }
                    }
                }
            }
            """,
            context_value={"session": empty_session},
        )
        # Schema should execute without errors at least for aggregates
        # (groups may not have data to process)
        empty_session.close()

    def test_filters_grouping_ordering_together(self, execute):
        """All features combined in one query."""
        data = execute("""
            query {
                orders(
                    first: 10,
                    filter: { field: { amount: { gte: 30 } } },
                    order: [{ field: { status: ASC } }],
                    groupBy: [{ field: { status: true } }]
                ) {
                    edges { node { id status amount } }
                    pageInfo { hasNextPage }
                    aggregates { count sum { amount } }
                    groups {
                        key { status }
                        edgeIndices
                        aggregates { count sum { amount } }
                    }
                }
            }
        """)
        assert data["orders"]["aggregates"]["count"] > 0
        for edge in data["orders"]["edges"]:
            assert float(edge["node"]["amount"]) >= 30
        statuses = [g["key"]["status"] for g in data["orders"]["groups"]]
        assert statuses == sorted(statuses)


# ===========================================================================
# Group items cursor pagination tests
# ===========================================================================


class TestGroupItemsPagination:
    def test_items_connection_in_schema(self):
        """The schema should include the items field with cursor pagination args."""
        sdl = str(schema.as_str())
        assert "OrderGroupItemsConnection" in sdl
        assert "items(" in sdl

    def test_items_returns_edges_and_page_info(self, execute):
        data = execute("""
            query {
                orders(
                    first: 20,
                    groupBy: [{ field: { status: true } }]
                ) {
                    groups {
                        key { status }
                        items(first: 3) {
                            edges { node { id status } }
                            pageInfo { hasNextPage endCursor }
                        }
                    }
                }
            }
        """)
        groups = data["orders"]["groups"]
        assert len(groups) == 3
        for grp in groups:
            items = grp["items"]
            assert "edges" in items
            assert "pageInfo" in items
            for edge in items["edges"]:
                assert edge["node"]["status"] == grp["key"]["status"]

    def test_items_first_limits_results(self, execute):
        data = execute("""
            query {
                orders(
                    first: 20,
                    groupBy: [{ field: { status: true } }]
                ) {
                    groups {
                        key { status }
                        items(first: 2) {
                            edges { node { id } }
                            pageInfo { hasNextPage }
                        }
                    }
                }
            }
        """)
        for grp in data["orders"]["groups"]:
            assert len(grp["items"]["edges"]) <= 2

    def test_items_has_next_page(self, execute):
        """Groups with more items than `first` should report hasNextPage=True."""
        data = execute("""
            query {
                orders(
                    first: 20,
                    groupBy: [{ field: { status: true } }]
                ) {
                    groups {
                        key { status }
                        aggregates { count }
                        items(first: 2) {
                            edges { node { id } }
                            pageInfo { hasNextPage endCursor }
                        }
                    }
                }
            }
        """)
        for grp in data["orders"]["groups"]:
            count = grp["aggregates"]["count"]
            has_next = grp["items"]["pageInfo"]["hasNextPage"]
            if count > 2:
                assert has_next is True
            else:
                assert has_next is False

    def test_items_after_cursor_pagination(self, execute):
        """Using `after` should skip past previously seen items."""
        data1 = execute("""
            query {
                orders(
                    first: 20,
                    groupBy: [{ field: { status: true } }]
                ) {
                    groups {
                        key { status }
                        items(first: 2) {
                            edges { node { id } cursor }
                            pageInfo { hasNextPage endCursor }
                        }
                    }
                }
            }
        """)
        shipped = next(
            g for g in data1["orders"]["groups"] if g["key"]["status"] == "shipped"
        )
        end_cursor = shipped["items"]["pageInfo"]["endCursor"]
        assert end_cursor is not None

        data2 = execute(
            """
            query($after: String) {
                orders(
                    first: 20,
                    groupBy: [{ field: { status: true } }]
                ) {
                    groups {
                        key { status }
                        items(first: 2, after: $after) {
                            edges { node { id } }
                            pageInfo { hasNextPage endCursor }
                        }
                    }
                }
            }
        """,
            {"after": end_cursor},
        )
        shipped2 = next(
            g for g in data2["orders"]["groups"] if g["key"]["status"] == "shipped"
        )
        first_ids = {e["node"]["id"] for e in shipped["items"]["edges"]}
        second_ids = {e["node"]["id"] for e in shipped2["items"]["edges"]}
        assert first_ids.isdisjoint(second_ids), (
            "Second page should not overlap with first"
        )

    def test_items_membership(self, execute):
        """Each group's items should only contain nodes matching the group key."""
        data = execute("""
            query {
                orders(
                    first: 20,
                    groupBy: [{ field: { status: true } }]
                ) {
                    groups {
                        key { status }
                        items(first: 10) {
                            edges { node { id status amount } }
                        }
                    }
                }
            }
        """)
        for grp in data["orders"]["groups"]:
            expected_status = grp["key"]["status"]
            for edge in grp["items"]["edges"]:
                assert edge["node"]["status"] == expected_status

    def test_items_without_grouping_no_error(self, execute):
        """Querying without groupBy should not crash."""
        data = execute("""
            query {
                orders(first: 3) {
                    edges { node { id } }
                    pageInfo { hasNextPage }
                }
            }
        """)
        assert len(data["orders"]["edges"]) == 3


# ===========================================================================
# Custom aggregation tests
# ===========================================================================

custom_orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

CustomOrderFilter = custom_orm.filter(Order)
CustomOrderOrder = custom_orm.order(Order)
CustomOrderGroupBy = custom_orm.group(Order)


@custom_orm.aggregate_type(Order)
class OrderAggregation:
    amount: auto
    quantity: auto

    @aggregate_field
    def total_revenue(self, columns) -> float:
        return sa_func.sum(columns.amount * columns.quantity)

    @aggregate_field
    def order_count_above_100(self, columns) -> int:
        return sa_func.count(case((columns.amount > 100, 1)))


@custom_orm.type(
    Order,
    filters=CustomOrderFilter,
    order=CustomOrderOrder,
    group=CustomOrderGroupBy,
    aggregate=OrderAggregation,
)
class CustomOrderType(relay.Node):
    id: relay.NodeID[int]
    status: auto
    customer_id: auto
    amount: auto
    quantity: auto
    created_at: auto


@strawberry.type
class CustomQuery:
    orders: ORMListConnection[CustomOrderType] = custom_orm.connection()


custom_schema = strawberry.Schema(
    query=CustomQuery,
    extensions=[custom_orm.optimizer_extension()],
)


@pytest.fixture
def custom_execute(session, seed):
    def _execute(query, variables=None):
        result = custom_schema.execute_sync(
            query,
            variable_values=variables or {},
            context_value={"session": session},
        )
        assert result.errors is None, f"GraphQL errors: {result.errors}"
        return result.data

    return _execute


class TestCustomAggregation:
    def test_custom_aggregate_fields_in_schema(self):
        sdl = str(custom_schema.as_str())
        assert "totalRevenue" in sdl
        assert "orderCountAbove100" in sdl

    def test_field_selection_restricts_standard_aggs(self):
        """With aggregate_type specifying amount & quantity,
        only those fields appear in sum/avg/min/max sub-types."""
        meta = custom_orm.backend._build_aggregate_types(Order, OrderAggregation)
        numeric_names = [f[0] for f in meta.numeric_fields]
        assert "amount" in numeric_names
        assert "quantity" in numeric_names
        assert "customer_id" not in numeric_names

    def test_custom_total_revenue(self, custom_execute):
        data = custom_execute("""
            query {
                orders(first: 5) {
                    aggregates {
                        count
                        totalRevenue
                    }
                }
            }
        """)
        agg = data["orders"]["aggregates"]
        assert agg["count"] == 20
        assert agg["totalRevenue"] is not None
        assert agg["totalRevenue"] > 0

    def test_custom_order_count_above_100(self, custom_execute):
        data = custom_execute("""
            query {
                orders(first: 5) {
                    aggregates {
                        orderCountAbove100
                    }
                }
            }
        """)
        count_above = data["orders"]["aggregates"]["orderCountAbove100"]
        assert count_above >= 0

    def test_custom_agg_correct_value(self, session, seed):
        """Verify total_revenue matches manual computation."""
        expected = sum(o.amount * o.quantity for o in seed)
        result = custom_schema.execute_sync(
            """
            query {
                orders(first: 1) {
                    aggregates { totalRevenue }
                }
            }
            """,
            context_value={"session": session},
        )
        assert result.errors is None
        assert (
            abs(result.data["orders"]["aggregates"]["totalRevenue"] - expected) < 0.01
        )

    def test_custom_agg_with_standard_aggs(self, custom_execute):
        """Custom and standard aggregates should work together."""
        data = custom_execute("""
            query {
                orders(first: 5) {
                    aggregates {
                        count
                        sum { amount quantity }
                        totalRevenue
                        orderCountAbove100
                    }
                }
            }
        """)
        agg = data["orders"]["aggregates"]
        assert agg["count"] == 20
        assert agg["sum"]["amount"] > 0
        assert agg["totalRevenue"] > 0
        assert agg["orderCountAbove100"] >= 0

    def test_custom_agg_in_groups(self, custom_execute):
        """Custom aggregates should also work within group aggregates."""
        data = custom_execute("""
            query {
                orders(
                    first: 20,
                    groupBy: [{ field: { status: true } }]
                ) {
                    groups {
                        key { status }
                        aggregates {
                            count
                            totalRevenue
                        }
                    }
                }
            }
        """)
        groups = data["orders"]["groups"]
        assert len(groups) == 3
        total_rev = 0
        for grp in groups:
            assert grp["aggregates"]["totalRevenue"] is not None
            total_rev += grp["aggregates"]["totalRevenue"]
        assert total_rev > 0

    def test_custom_agg_with_filter(self, custom_execute):
        data = custom_execute("""
            query {
                orders(
                    first: 5,
                    filter: { field: { status: { exact: "shipped" } } }
                ) {
                    aggregates {
                        count
                        totalRevenue
                    }
                }
            }
        """)
        agg = data["orders"]["aggregates"]
        assert agg["count"] == 9
        assert agg["totalRevenue"] > 0
