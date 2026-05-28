"""Shared tests for optimizer + lazy-resolution under Relay connections."""

import warnings
from collections.abc import Callable, Iterable
from typing import Any

import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto

UsersQueryFactory = Callable[[type, Any], Any]

CONNECTION_QUERY = """
{
  usersConnection(first: 10) {
    edges {
      node {
        posts {
          title
        }
      }
    }
  }
}
"""

CONNECTION_DEEP_QUERY = """
{
  usersConnection(first: 10) {
    edges {
      node {
        posts {
          tags {
            name
          }
        }
      }
    }
  }
}
"""

CONNECTION_PAGE_INFO_QUERY = """
{
  usersConnection(first: 10) {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      cursor
    }
  }
}
"""

CONNECTION_INLINE_FRAGMENT_QUERY = """
{
  usersConnection(first: 10) {
    edges {
      node {
        posts {
          ... on PostType {
            tags {
              name
            }
          }
        }
      }
    }
  }
}
"""

CONNECTION_FRAGMENT_SPREAD_QUERY = """
fragment PostTagFields on PostType {
  tags {
    name
  }
}
{
  usersConnection(first: 10) {
    edges {
      node {
        posts {
          ...PostTagFields
        }
      }
    }
  }
}
"""

LIST_QUERY = "{ users { posts { title } } }"

LIST_DEEP_QUERY = "{ users { posts { tags { name } } } }"


def _lazy_warnings(
    caught: list[warnings.WarningMessage],
) -> list[warnings.WarningMessage]:
    return [
        w
        for w in caught
        if issubclass(w.category, UserWarning)
        and "Unoptimized relation loads detected" in str(w.message)
    ]


def _sql_text(queries: list[str]) -> str:
    return " ".join(q.lower() for q in queries)


def _build_schema(
    orm: Any,
    User: type,
    Post: type,
    users_query: UsersQueryFactory,
    *,
    use_connection: bool,
    async_resolvers: bool = False,
    extensions: list[Any] | None = None,
    post_type: Any | None = None,
) -> strawberry.Schema:
    if post_type is None:

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

        post_type = PostType

    @orm.type(User)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        posts: list[post_type]

    @strawberry.type
    class Query:
        if use_connection:
            if async_resolvers:

                @orm.connection(ORMListConnection[UserNode])
                async def users_connection(
                    self, info: strawberry.types.Info
                ) -> Iterable[UserNode]:
                    return users_query(User, info)  # type: ignore[return-value]

            else:

                @orm.connection(ORMListConnection[UserNode])
                def users_connection(
                    self, info: strawberry.types.Info
                ) -> Iterable[UserNode]:
                    return users_query(User, info)  # type: ignore[return-value]

        elif async_resolvers:

            @strawberry.field
            async def users(self, info: strawberry.types.Info) -> list[UserNode]:
                return users_query(User, info)  # type: ignore[return-value]

        else:

            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UserNode]:
                return users_query(User, info)  # type: ignore[return-value]

    schema_extensions = extensions
    if schema_extensions is None:
        schema_extensions = [
            orm.optimizer_extension(),
            orm.lazy_resolution_extension(mode="warn"),
        ]

    return strawberry.Schema(query=Query, extensions=schema_extensions)


def _build_schema_with_tags(
    orm: Any,
    User: type,
    Post: type,
    Tag: type,
    users_query: UsersQueryFactory,
    **kwargs: Any,
) -> strawberry.Schema:
    @orm.type(Tag)
    class TagType:
        id: auto
        name: auto

    @orm.type(Post)
    class PostType:
        id: auto
        title: auto
        tags: list[TagType]

    return _build_schema(
        orm,
        User,
        Post,
        users_query,
        post_type=PostType,
        **kwargs,
    )


class _RelayConnectionOptimizerExpectations:
    expected_edge_count = 3
    max_connection_queries = 2
    max_connection_deep_queries = 3
    max_connection_query_delta_over_list = 2
    require_sql_mentions_posts = True
    require_sql_mentions_tags = True
    require_nested_tag_prefetch = True
    async_resolvers = False


def _assert_connection_posts_ok(
    expectations: _RelayConnectionOptimizerExpectations,
    result: Any,
    queries: list[str],
    caught: list[warnings.WarningMessage],
) -> None:
    assert result.errors is None
    assert (
        len(result.data["usersConnection"]["edges"]) == expectations.expected_edge_count
    )
    assert len(queries) <= expectations.max_connection_queries, len(queries)
    if expectations.require_sql_mentions_posts:
        assert "post" in _sql_text(queries)
    assert _lazy_warnings(caught) == []


def _assert_connection_deep_ok(
    expectations: _RelayConnectionOptimizerExpectations,
    result: Any,
    queries: list[str],
    caught: list[warnings.WarningMessage],
) -> None:
    assert result.errors is None
    assert (
        len(result.data["usersConnection"]["edges"]) == expectations.expected_edge_count
    )
    assert len(queries) <= expectations.max_connection_deep_queries, len(queries)
    if expectations.require_sql_mentions_tags:
        assert "tag" in _sql_text(queries)
    if expectations.require_nested_tag_prefetch:
        assert _lazy_warnings(caught) == []


def _assert_connection_tags_query_ok(
    expectations: _RelayConnectionOptimizerExpectations,
    result: Any,
    queries: list[str],
    caught: list[warnings.WarningMessage],
) -> None:
    assert result.errors is None
    assert len(queries) <= expectations.max_connection_deep_queries, len(queries)
    if expectations.require_sql_mentions_tags:
        assert "tag" in _sql_text(queries)
    if expectations.require_nested_tag_prefetch:
        assert _lazy_warnings(caught) == []


class AbstractTestRelayConnectionOptimizerLazyWarningSync(
    _RelayConnectionOptimizerExpectations
):
    def test_connection_nested_posts_prefetched_without_lazy_warning(
        self,
        orm,
        seed,
        User,
        Post,
        users_query,
        schema_execute_with_queries,
    ):
        """``edges { node { posts } }`` should prefetch like a plain list field."""
        schema = _build_schema(
            orm,
            User,
            Post,
            users_query,
            use_connection=True,
            async_resolvers=getattr(self, "async_resolvers", False),
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result, queries = schema_execute_with_queries(schema, CONNECTION_QUERY)

        _assert_connection_posts_ok(self, result, queries, caught)

    def test_list_nested_posts_does_not_warn_when_optimizer_prefetches(
        self,
        orm,
        seed,
        User,
        Post,
        users_query,
        schema_execute,
    ):
        """Same nested ``posts`` under a list field is optimized — no warning (control)."""
        schema = _build_schema(
            orm,
            User,
            Post,
            users_query,
            use_connection=False,
            async_resolvers=getattr(self, "async_resolvers", False),
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result = schema_execute(schema, LIST_QUERY)

        assert result.errors is None
        assert _lazy_warnings(caught) == []

    def test_connection_and_list_query_counts_stay_bounded(
        self,
        orm,
        seed,
        User,
        Post,
        users_query,
        schema_execute_with_queries,
    ):
        """Relay pagination overhead should stay small versus a plain list field."""
        conn_schema = _build_schema(
            orm,
            User,
            Post,
            users_query,
            use_connection=True,
            async_resolvers=getattr(self, "async_resolvers", False),
        )
        list_schema = _build_schema(
            orm,
            User,
            Post,
            users_query,
            use_connection=False,
            async_resolvers=getattr(self, "async_resolvers", False),
        )
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always", category=UserWarning)
            _, conn_queries = schema_execute_with_queries(conn_schema, CONNECTION_QUERY)
            _, list_queries = schema_execute_with_queries(list_schema, LIST_QUERY)

        assert len(list_queries) <= self.max_connection_queries, len(list_queries)
        assert len(conn_queries) <= self.max_connection_queries, len(conn_queries)
        assert (
            len(conn_queries)
            <= len(list_queries) + self.max_connection_query_delta_over_list
        )

    def test_connection_deeply_nested_tags_without_lazy_warning(
        self,
        orm,
        seed,
        User,
        Post,
        Tag,
        users_query,
        schema_execute_with_queries,
    ):
        """Three levels under ``edges { node { ... } }`` should still prefetch."""
        schema = _build_schema_with_tags(
            orm,
            User,
            Post,
            Tag,
            users_query,
            use_connection=True,
            async_resolvers=getattr(self, "async_resolvers", False),
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result, queries = schema_execute_with_queries(schema, CONNECTION_DEEP_QUERY)

        _assert_connection_deep_ok(self, result, queries, caught)

    def test_list_deeply_nested_tags_matches_connection_budget(
        self,
        orm,
        seed,
        User,
        Post,
        Tag,
        users_query,
        schema_execute_with_queries,
    ):
        """Deep list selections should use a similar query budget as the connection."""
        conn_schema = _build_schema_with_tags(
            orm,
            User,
            Post,
            Tag,
            users_query,
            use_connection=True,
            async_resolvers=getattr(self, "async_resolvers", False),
        )
        list_schema = _build_schema_with_tags(
            orm,
            User,
            Post,
            Tag,
            users_query,
            use_connection=False,
            async_resolvers=getattr(self, "async_resolvers", False),
        )
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always", category=UserWarning)
            _, conn_queries = schema_execute_with_queries(
                conn_schema, CONNECTION_DEEP_QUERY
            )
            _, list_queries = schema_execute_with_queries(list_schema, LIST_DEEP_QUERY)

        assert len(list_queries) <= self.max_connection_deep_queries, len(list_queries)
        assert len(conn_queries) <= self.max_connection_deep_queries, len(conn_queries)

    def test_connection_inline_fragment_prefetches_nested_relation(
        self,
        orm,
        seed,
        User,
        Post,
        Tag,
        users_query,
        schema_execute_with_queries,
    ):
        """Inline fragments under ``node`` should apply the same prefetches."""
        schema = _build_schema_with_tags(
            orm,
            User,
            Post,
            Tag,
            users_query,
            use_connection=True,
            async_resolvers=getattr(self, "async_resolvers", False),
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result, queries = schema_execute_with_queries(
                schema, CONNECTION_INLINE_FRAGMENT_QUERY
            )

        _assert_connection_tags_query_ok(self, result, queries, caught)

    def test_connection_fragment_spread_prefetches_nested_relation(
        self,
        orm,
        seed,
        User,
        Post,
        Tag,
        users_query,
        schema_execute_with_queries,
    ):
        """Named fragment spreads under ``node`` should apply the same prefetches."""
        schema = _build_schema_with_tags(
            orm,
            User,
            Post,
            Tag,
            users_query,
            use_connection=True,
            async_resolvers=getattr(self, "async_resolvers", False),
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result, queries = schema_execute_with_queries(
                schema, CONNECTION_FRAGMENT_SPREAD_QUERY
            )

        _assert_connection_tags_query_ok(self, result, queries, caught)

    def test_connection_page_info_only_does_not_touch_posts(
        self,
        orm,
        seed,
        User,
        Post,
        users_query,
        schema_execute_with_queries,
    ):
        """Selecting only pagination metadata must not load ``posts`` relations."""
        schema = _build_schema(
            orm,
            User,
            Post,
            users_query,
            use_connection=True,
            async_resolvers=getattr(self, "async_resolvers", False),
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result, queries = schema_execute_with_queries(
                schema, CONNECTION_PAGE_INFO_QUERY
            )

        assert result.errors is None
        assert len(queries) <= 1, len(queries)
        if self.require_sql_mentions_posts:
            assert "post" not in _sql_text(queries)
        assert _lazy_warnings(caught) == []

    def test_connection_without_optimizer_emits_lazy_warning(
        self,
        orm,
        seed,
        User,
        Post,
        users_query,
        schema_execute,
    ):
        """Without the optimizer extension, nested ``posts`` should warn."""
        schema = _build_schema(
            orm,
            User,
            Post,
            users_query,
            use_connection=True,
            async_resolvers=getattr(self, "async_resolvers", False),
            extensions=[orm.lazy_resolution_extension(mode="warn")],
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result = schema_execute(schema, CONNECTION_QUERY)

        assert result.errors is None
        lazy = _lazy_warnings(caught)
        assert len(lazy) >= 1
        assert any(
            "User.posts" in str(w.message) or "UserNode.posts" in str(w.message)
            for w in lazy
        )


class AbstractTestRelayConnectionOptimizerLazyWarningAsync(
    _RelayConnectionOptimizerExpectations
):
    async_resolvers = True
    max_connection_queries = 5
    max_connection_deep_queries = 12
    max_connection_query_delta_over_list = 3
    require_nested_tag_prefetch = False

    async def test_connection_nested_posts_prefetched_without_lazy_warning(
        self,
        orm,
        seed,
        User,
        Post,
        users_query,
        schema_execute_with_queries,
    ):
        schema = _build_schema(
            orm,
            User,
            Post,
            users_query,
            use_connection=True,
            async_resolvers=self.async_resolvers,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result, queries = await schema_execute_with_queries(
                schema, CONNECTION_QUERY
            )
        _assert_connection_posts_ok(self, result, queries, caught)

    async def test_list_nested_posts_does_not_warn_when_optimizer_prefetches(
        self,
        orm,
        seed,
        User,
        Post,
        users_query,
        schema_execute,
    ):
        schema = _build_schema(
            orm,
            User,
            Post,
            users_query,
            use_connection=False,
            async_resolvers=self.async_resolvers,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result = await schema_execute(schema, LIST_QUERY)
        assert result.errors is None
        assert _lazy_warnings(caught) == []

    async def test_connection_and_list_query_counts_stay_bounded(
        self,
        orm,
        seed,
        User,
        Post,
        users_query,
        schema_execute_with_queries,
    ):
        conn_schema = _build_schema(
            orm,
            User,
            Post,
            users_query,
            use_connection=True,
            async_resolvers=self.async_resolvers,
        )
        list_schema = _build_schema(
            orm,
            User,
            Post,
            users_query,
            use_connection=False,
            async_resolvers=self.async_resolvers,
        )
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always", category=UserWarning)
            _, conn_queries = await schema_execute_with_queries(
                conn_schema, CONNECTION_QUERY
            )
            _, list_queries = await schema_execute_with_queries(list_schema, LIST_QUERY)
        assert len(list_queries) <= self.max_connection_queries, len(list_queries)
        assert len(conn_queries) <= self.max_connection_queries, len(conn_queries)
        assert (
            len(conn_queries)
            <= len(list_queries) + self.max_connection_query_delta_over_list
        )

    async def test_connection_deeply_nested_tags_without_lazy_warning(
        self,
        orm,
        seed,
        User,
        Post,
        Tag,
        users_query,
        schema_execute_with_queries,
    ):
        schema = _build_schema_with_tags(
            orm,
            User,
            Post,
            Tag,
            users_query,
            use_connection=True,
            async_resolvers=self.async_resolvers,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result, queries = await schema_execute_with_queries(
                schema, CONNECTION_DEEP_QUERY
            )
        _assert_connection_deep_ok(self, result, queries, caught)

    async def test_list_deeply_nested_tags_matches_connection_budget(
        self,
        orm,
        seed,
        User,
        Post,
        Tag,
        users_query,
        schema_execute_with_queries,
    ):
        conn_schema = _build_schema_with_tags(
            orm,
            User,
            Post,
            Tag,
            users_query,
            use_connection=True,
            async_resolvers=self.async_resolvers,
        )
        list_schema = _build_schema_with_tags(
            orm,
            User,
            Post,
            Tag,
            users_query,
            use_connection=False,
            async_resolvers=self.async_resolvers,
        )
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always", category=UserWarning)
            _, conn_queries = await schema_execute_with_queries(
                conn_schema, CONNECTION_DEEP_QUERY
            )
            _, list_queries = await schema_execute_with_queries(
                list_schema, LIST_DEEP_QUERY
            )
        assert len(list_queries) <= self.max_connection_deep_queries, len(list_queries)
        assert len(conn_queries) <= self.max_connection_deep_queries, len(conn_queries)

    async def test_connection_inline_fragment_prefetches_nested_relation(
        self,
        orm,
        seed,
        User,
        Post,
        Tag,
        users_query,
        schema_execute_with_queries,
    ):
        schema = _build_schema_with_tags(
            orm,
            User,
            Post,
            Tag,
            users_query,
            use_connection=True,
            async_resolvers=self.async_resolvers,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result, queries = await schema_execute_with_queries(
                schema, CONNECTION_INLINE_FRAGMENT_QUERY
            )
        _assert_connection_tags_query_ok(self, result, queries, caught)

    async def test_connection_fragment_spread_prefetches_nested_relation(
        self,
        orm,
        seed,
        User,
        Post,
        Tag,
        users_query,
        schema_execute_with_queries,
    ):
        schema = _build_schema_with_tags(
            orm,
            User,
            Post,
            Tag,
            users_query,
            use_connection=True,
            async_resolvers=self.async_resolvers,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result, queries = await schema_execute_with_queries(
                schema, CONNECTION_FRAGMENT_SPREAD_QUERY
            )
        _assert_connection_tags_query_ok(self, result, queries, caught)

    async def test_connection_page_info_only_does_not_touch_posts(
        self,
        orm,
        seed,
        User,
        Post,
        users_query,
        schema_execute_with_queries,
    ):
        schema = _build_schema(
            orm,
            User,
            Post,
            users_query,
            use_connection=True,
            async_resolvers=self.async_resolvers,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result, queries = await schema_execute_with_queries(
                schema, CONNECTION_PAGE_INFO_QUERY
            )
        assert result.errors is None
        assert len(queries) <= 1, len(queries)
        if self.require_sql_mentions_posts:
            assert "post" not in _sql_text(queries)
        assert _lazy_warnings(caught) == []

    async def test_connection_without_optimizer_emits_lazy_warning(
        self,
        orm,
        seed,
        User,
        Post,
        users_query,
        schema_execute,
    ):
        schema = _build_schema(
            orm,
            User,
            Post,
            users_query,
            use_connection=True,
            async_resolvers=self.async_resolvers,
            extensions=[orm.lazy_resolution_extension(mode="warn")],
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result = await schema_execute(schema, CONNECTION_QUERY)
        assert result.errors is None
        lazy = _lazy_warnings(caught)
        assert len(lazy) >= 1
        assert any(
            "User.posts" in str(w.message) or "UserNode.posts" in str(w.message)
            for w in lazy
        )


class AbstractTestRelayConnectionOptimizerUnitSync:
    def test_apply_optimizer_hints_walks_relay_connection_selection(
        self, seed, User, apply_optimizer_hints_relay_connection
    ):
        users = apply_optimizer_hints_relay_connection(User)
        assert len(users) >= 1


class AbstractTestRelayConnectionOptimizerUnitAsync:
    async def test_apply_optimizer_hints_walks_relay_connection_selection(
        self, seed, User, apply_optimizer_hints_relay_connection
    ):
        users = await apply_optimizer_hints_relay_connection(User)
        assert len(users) >= 1
