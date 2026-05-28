"""Focused backend-adapter coverage for exact helper branches."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import strawberry
from sqlalchemy import select, text

from strawberry_orm import Ordering
from strawberry_orm.backends._base import AggregateMeta
from strawberry_orm.backends.sqlalchemy import (
    SQLAlchemyBackend,
    _build_lookup_clauses,
    _build_reference_lookup_clauses,
    _build_sa_field_clause,
    _build_sa_filter,
    _build_sa_order_field,
    _build_sa_order_from_input,
    _build_sa_ordering,
    _build_sa_reference_field_clause,
    _extract_overlapping_order,
    _extract_sa_group_columns,
)
from strawberry_orm.filters import (
    IntComparisonLookup,
    IntRangeInput,
    ReferenceLookup,
    StringLookup,
)
from strawberry_orm.types import DateGroupByInterval, DateGroupByOption, auto


@strawberry.input
class EmptyFilterGroup:
    all: list["EmptyFilterGroup"] | None = strawberry.UNSET
    any: list["EmptyFilterGroup"] | None = strawberry.UNSET
    one_of: list["EmptyFilterGroup"] | None = strawberry.UNSET
    not_: "EmptyFilterGroup | None" = strawberry.UNSET
    field: object | None = strawberry.UNSET


@strawberry.input
class InvalidFieldInput:
    missing: object | None = strawberry.UNSET


@strawberry.input
class RegexLookup:
    regex: str | None = strawberry.UNSET
    i_regex: str | None = strawberry.UNSET


@strawberry.input
class InvalidOrderInput:
    missing: Ordering | None = strawberry.UNSET


@strawberry.type
class _TestAggregates:
    count: int


@strawberry.type
class _TestGroupKey:
    author_id: str | None = None


@strawberry.input
class _AuthorGroupField:
    author_id: bool | None = True


@strawberry.input
class _AuthorGroupBy:
    field: _AuthorGroupField | None = strawberry.UNSET


@strawberry.input
class _CreatedGroupField:
    created_at: DateGroupByOption | None = strawberry.UNSET


@strawberry.input
class _CreatedGroupBy:
    field: _CreatedGroupField | None = strawberry.UNSET


@strawberry.input
class _AuthorOrderField:
    author_id: Ordering | None = Ordering.DESC


@strawberry.input
class _AuthorOrder:
    field: _AuthorOrderField | None = strawberry.UNSET


@strawberry.input
class _RefPostField:
    author_id: ReferenceLookup | None = strawberry.UNSET


@strawberry.input
class _FkPostField:
    author_id: IntComparisonLookup | None = strawberry.UNSET


@strawberry.input
class _BadRefField:
    author_id: StringLookup | None = strawberry.UNSET


def _aggregate_meta(model):
    return AggregateMeta(
        model=model,
        aggregates_type=_TestAggregates,
        group_key_type=_TestGroupKey,
    )


def _info_with_aggregates(session, path: str = "aggregates", **agg_fields):
    selections = []
    if agg_fields.get("count"):
        selections.append(SimpleNamespace(name="count", selections=[]))
    for func_name in ("sum", "avg", "min", "max"):
        names = agg_fields.get(func_name)
        if names is not None:
            sub = [SimpleNamespace(name=name) for name in names]
            selections.append(SimpleNamespace(name=func_name, selections=sub))

    parts = path.split(".")
    node = SimpleNamespace(name=parts[-1], selections=selections)
    for part in reversed(parts[:-1]):
        node = SimpleNamespace(name=part, selections=[node])
    return SimpleNamespace(
        selected_fields=[node],
        context={"session": session},
        field_nodes=[],
    )


class TestInternalBackendCoverage:
    def test_filter_helpers_handle_none_and_empty_groups(self, User):
        assert _build_sa_filter(None, User) == (None, None)
        assert _build_sa_filter(EmptyFilterGroup(all=[]), User) == (None, None)
        assert _build_sa_filter(EmptyFilterGroup(any=[]), User) == (None, None)
        assert _build_sa_filter(EmptyFilterGroup(one_of=[]), User) == (None, None)
        assert _build_sa_field_clause(InvalidFieldInput(), User) is None

        with pytest.raises(ValueError, match="maximum is 0"):
            _build_sa_filter(
                EmptyFilterGroup(all=[EmptyFilterGroup()]), User, max_branches=0
            )
        with pytest.raises(ValueError, match="maximum is 0"):
            _build_sa_filter(
                EmptyFilterGroup(one_of=[EmptyFilterGroup()]),
                User,
                max_branches=0,
            )
        with pytest.raises(ValueError, match="Regex filters are disabled"):
            _build_lookup_clauses(
                User.name, RegexLookup(regex="a.*"), enable_regex=False
            )
        with pytest.raises(ValueError, match="Regex filters are disabled"):
            _build_lookup_clauses(
                User.name,
                RegexLookup(i_regex="a.*"),
                enable_regex=False,
            )
        assert _build_sa_ordering(InvalidOrderInput(), User) == ([], [], None)

    def test_query_object_helpers_handle_fallback_values(self, sa_session):
        backend = SQLAlchemyBackend(dialect="sqlite")
        plain = ["a", "b"]
        assert backend.apply_optimizer_hints(None, plain, info=None) == plain

        class Info:
            context = {"session": sa_session}

        with pytest.raises(ValueError, match="Invalid filter expression"):
            backend._execute_stmt_sync(sa_session, text("select * from missing_table"))

    def test_internal_queryset_hooks_and_ordering_helpers(self, User):
        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._type_registry["UserType"] = User

        class QueryType:
            @classmethod
            def get_queryset(cls, stmt, info):
                return stmt.where(User.name == "Alice")

        backend._type_querysets[User] = QueryType.get_queryset
        backend._store.hints = {
            "UserType": {
                "name": SimpleNamespace(
                    load=lambda stmt: stmt.where(User.email.like("%example.com")),
                    only=None,
                    disable_optimization=False,
                )
            }
        }

        stmt = backend.get_default_queryset(User)
        scoped = backend._apply_nested_queryset(stmt, User, "name", User, info=None)
        text_value = str(scoped)
        assert '"user".name' in text_value
        assert '"user".email' in text_value

    def test_lookup_and_reference_builder_branches(self, User, Post):
        col = User.name
        assert len(_build_lookup_clauses(col, StringLookup(exact="Alice"))) == 1
        assert len(_build_lookup_clauses(col, StringLookup(contains="lic"))) == 1
        assert len(_build_lookup_clauses(col, StringLookup(i_contains="LIC"))) == 1
        assert len(_build_lookup_clauses(col, StringLookup(starts_with="Al"))) == 1
        assert len(_build_lookup_clauses(col, StringLookup(i_starts_with="al"))) == 1
        assert len(_build_lookup_clauses(col, StringLookup(ends_with="ce"))) == 1
        assert len(_build_lookup_clauses(col, StringLookup(i_ends_with="CE"))) == 1
        assert len(_build_lookup_clauses(col, StringLookup(is_null=False))) == 1
        assert len(_build_lookup_clauses(col, StringLookup(in_list=["Alice"]))) == 1
        assert len(_build_lookup_clauses(col, StringLookup(not_in_list=["Bob"]))) == 1
        assert (
            len(
                _build_lookup_clauses(
                    User.id,
                    IntComparisonLookup(range=IntRangeInput(start=1, end=3)),
                )
            )
            == 1
        )

        ref_col = User.id
        assert len(_build_reference_lookup_clauses(ref_col, ReferenceLookup(exact="1")))
        assert len(
            _build_reference_lookup_clauses(
                ref_col, ReferenceLookup(in_list=["1", "2"])
            )
        )
        assert len(
            _build_reference_lookup_clauses(ref_col, ReferenceLookup(not_in_list=["3"]))
        )
        assert len(_build_reference_lookup_clauses(ref_col, ReferenceLookup(neq="4")))
        assert len(
            _build_reference_lookup_clauses(ref_col, ReferenceLookup(is_null=True))
        )

        ref_clause = _build_sa_reference_field_clause(
            _RefPostField(author_id=ReferenceLookup(exact="1")),
            local_col=Post.author_id,
        )
        assert ref_clause is not None

        fk_clause = _build_sa_reference_field_clause(
            _FkPostField(author_id=IntComparisonLookup(exact=1)),
            local_col=Post.author_id,
        )
        assert fk_clause is not None

        with pytest.raises(TypeError, match="Expected ReferenceLookup"):
            _build_sa_reference_field_clause(
                _BadRefField(author_id=StringLookup(exact="x")),
                local_col=Post.author_id,
            )

    def test_grouping_helpers_cover_date_intervals(self, Post):
        subq = select(Post).subquery()
        for interval in DateGroupByInterval:
            group_by = _CreatedGroupBy(
                field=_CreatedGroupField(
                    created_at=DateGroupByOption(interval=interval)
                )
            )
            cols, key_fields = _extract_sa_group_columns([group_by], Post, subq)
            assert key_fields == ["created_at"]
            assert len(cols) == 1

    def test_apply_aggregation_grouping_and_batch_helpers(
        self, sa_session, seed, Post, User
    ):
        backend = SQLAlchemyBackend(dialect="sqlite")
        meta = _aggregate_meta(Post)
        query = select(Post)

        empty_info = _info_with_aggregates(sa_session)
        result = backend.apply_aggregation(query, empty_info, meta)
        assert result.count == 0

        count_info = _info_with_aggregates(sa_session, count=True)
        counted = backend.apply_aggregation(query, count_info, meta)
        assert counted.count == 4

        group_info = _info_with_aggregates(
            sa_session, path="groups.aggregates", count=True
        )
        groups = backend.apply_grouping(
            query,
            _AuthorGroupBy(field=_AuthorGroupField(author_id=True)),
            group_info,
            meta,
            order_input=_AuthorOrder(field=_AuthorOrderField(author_id=Ordering.DESC)),
        )
        assert len(groups) == 3
        assert all(group.key.author_id is not None for group in groups)

        @dataclass
        class ScopeKey:
            author_id: int | None = 1

        scoped = backend.scope_query_to_group(select(Post), ScopeKey())
        assert "author_id" in str(scoped)

        batch_info = SimpleNamespace(context={"session": sa_session})
        batched = backend.batch_group_items(
            select(Post),
            ["author_id"],
            batch_info,
            Post,
            per_group_limit=1,
            order_input=_AuthorOrder(field=_AuthorOrderField(author_id=Ordering.ASC)),
        )
        assert ("1",) in batched
        assert len(batched[("1",)]) == 1

    def test_apply_ref_list_respects_authorize(self, sa_session, seed, Post, Tag):
        backend = SQLAlchemyBackend(dialect="sqlite")
        post = sa_session.get(Post, 1)
        assert post is not None
        before = {tag.name for tag in post.tags}

        @strawberry.input
        class CreateTagInput:
            name: str

        ref = SimpleNamespace(
            create=CreateTagInput(name="blocked-tag"),
            update=strawberry.UNSET,
            unlink=strawberry.UNSET,
            delete=strawberry.UNSET,
        )
        info = SimpleNamespace(context={"session": sa_session})
        backend.apply_ref_list(
            post,
            "tags",
            [ref],
            info,
            authorize=lambda action, model, obj_id, _info: False,
        )
        sa_session.flush()
        assert {tag.name for tag in post.tags} == before

        @strawberry.input
        class UnlinkInput:
            id: strawberry.ID

        @strawberry.input
        class UpdateTagInput:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        backend.apply_ref_list(
            post,
            "tags",
            [
                SimpleNamespace(
                    create=strawberry.UNSET,
                    update=UpdateTagInput(id=strawberry.ID("1"), name="nope"),
                    unlink=strawberry.UNSET,
                    delete=strawberry.UNSET,
                ),
                SimpleNamespace(
                    create=strawberry.UNSET,
                    update=strawberry.UNSET,
                    unlink=UnlinkInput(id=strawberry.ID("1")),
                    delete=strawberry.UNSET,
                ),
            ],
            info,
            authorize=lambda action, model, obj_id, _info: False,
        )
        sa_session.flush()
        assert {tag.name for tag in post.tags} == before

    def test_apply_optimizer_hints_with_nested_selection(
        self, sa_session, seed, User, Post
    ):
        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._type_registry["UserType"] = User

        class QueryType:
            @classmethod
            def get_queryset(cls, stmt, info):
                return stmt.where(User.name != "missing")

        backend._type_querysets[Post] = QueryType.get_queryset
        backend._store.hints = {
            "UserType": {
                "posts": SimpleNamespace(
                    load=lambda stmt: stmt.where(Post.is_published.is_(True)),
                    only=["title"],
                    disable_optimization=False,
                )
            }
        }

        posts_sel = SimpleNamespace(
            name=SimpleNamespace(value="posts"),
            selection_set=SimpleNamespace(selections=[]),
        )
        field_node = SimpleNamespace(
            selection_set=SimpleNamespace(selections=[posts_sel])
        )
        info = SimpleNamespace(
            field_nodes=[field_node],
            context={"session": sa_session},
        )
        query = backend.get_default_queryset(User)
        users = backend.apply_optimizer_hints(backend._store, query, info)
        assert len(users) >= 1

    def test_apply_optimizer_hints_chained_nested_relationships(
        self, sa_session, seed, User, Post
    ):
        from tests.backends.sqlalchemy.models import Comment as SAComment

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._type_registry["PostType"] = Post
        backend._type_registry["UserType"] = User

        class PostQueryType:
            @classmethod
            def get_queryset(cls, stmt, info):
                return stmt.where(Post.is_published.is_(True))

        class CommentQueryType:
            @classmethod
            def get_queryset(cls, stmt, info):
                return stmt.where(SAComment.body != "")

        backend._type_querysets[Post] = PostQueryType.get_queryset
        backend._type_querysets[SAComment] = CommentQueryType.get_queryset
        backend._type_querysets[User] = PostQueryType.get_queryset
        backend._store.hints = {
            "UserType": {
                "name": SimpleNamespace(
                    load=["posts"],
                    only=["name", "email"],
                    disable_optimization=False,
                )
            },
            "PostType": {
                "comments": SimpleNamespace(
                    load=lambda stmt: stmt.where(SAComment.body.like("%")),
                    only=None,
                    disable_optimization=False,
                )
            },
        }

        comments_sel = SimpleNamespace(
            name=SimpleNamespace(value="comments"),
            selection_set=None,
        )
        author_sel = SimpleNamespace(
            name=SimpleNamespace(value="author"),
            selection_set=None,
        )
        posts_sel = SimpleNamespace(
            name=SimpleNamespace(value="posts"),
            selection_set=SimpleNamespace(selections=[comments_sel, author_sel]),
        )
        name_sel = SimpleNamespace(
            name=SimpleNamespace(value="name"),
            selection_set=None,
        )
        field_node = SimpleNamespace(
            selection_set=SimpleNamespace(selections=[posts_sel, name_sel])
        )
        info = SimpleNamespace(
            field_nodes=[field_node],
            context={"session": sa_session},
        )
        users = backend.apply_optimizer_hints(
            backend._store, backend.get_default_queryset(User), info
        )
        assert len(users) >= 1

    def test_optimizer_nested_loader_without_selection_set(
        self, sa_session, seed, User, Post
    ):
        from graphql.language.ast import FieldNode, NameNode, SelectionSetNode

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._type_registry["UserType"] = User
        backend._store.hints = {
            "UserType": {
                "posts": SimpleNamespace(
                    load=["comments"],
                    only=None,
                    disable_optimization=False,
                )
            }
        }
        posts_field = FieldNode(
            alias=None,
            name=NameNode(value="posts"),
            arguments=(),
            directives=(),
            selection_set=None,
        )
        field_node = FieldNode(
            alias=None,
            name=NameNode(value="users"),
            arguments=(),
            directives=(),
            selection_set=SelectionSetNode(selections=[posts_field]),
        )
        info = SimpleNamespace(
            field_nodes=[field_node],
            fragments={},
            context={"session": sa_session},
        )
        result = backend.apply_optimizer_hints(
            backend._store, backend.get_default_queryset(User), info
        )
        assert len(result) >= 1

    def test_apply_optimizer_hints_walks_graphql_inline_fragments(
        self, sa_session, seed, User, Post
    ):
        from graphql import parse

        backend = SQLAlchemyBackend(dialect="sqlite")
        doc = parse("{ users { posts { ... on PostType { title tags { name } } } } }")
        users_field = doc.definitions[0].selection_set.selections[0]
        info = SimpleNamespace(
            field_nodes=[users_field],
            fragments={},
            context={"session": sa_session},
        )
        users = backend.apply_optimizer_hints(None, select(User), info)
        assert len(users) >= 1

    def test_apply_optimizer_hints_chained_loader_with_criteria(
        self, sa_session, seed, User, Post
    ):
        from graphql.language.ast import FieldNode, NameNode, SelectionSetNode

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._type_registry["UserType"] = User

        class PostQueryType:
            @classmethod
            def get_queryset(cls, stmt, info):
                return stmt.where(Post.is_published.is_(True))

        class UserQueryType:
            @classmethod
            def get_queryset(cls, stmt, info):
                return stmt.where(User.name != "")

        backend._type_querysets[Post] = PostQueryType.get_queryset
        backend._type_querysets[User] = UserQueryType.get_queryset
        author_field = FieldNode(
            alias=None,
            name=NameNode(value="author"),
            arguments=(),
            directives=(),
            selection_set=None,
        )
        posts_field = FieldNode(
            alias=None,
            name=NameNode(value="posts"),
            arguments=(),
            directives=(),
            selection_set=SelectionSetNode(selections=[author_field]),
        )
        users_field = FieldNode(
            alias=None,
            name=NameNode(value="users"),
            arguments=(),
            directives=(),
            selection_set=SelectionSetNode(selections=[posts_field]),
        )
        info = SimpleNamespace(
            field_nodes=[users_field],
            fragments={},
            context={"session": sa_session},
        )
        users = backend.apply_optimizer_hints(
            None, backend.get_default_queryset(User), info
        )
        assert len(users) >= 1

    def test_apply_optimizer_hints_selectinload_with_criteria(
        self, sa_session, seed, User, Post, Tag
    ):
        from graphql.language.ast import FieldNode, NameNode, SelectionSetNode

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._type_registry["UserType"] = User

        class TagQueryType:
            @classmethod
            def get_queryset(cls, stmt, info):
                return stmt.where(Tag.name != "")

        backend._type_querysets[Tag] = TagQueryType.get_queryset
        tags_field = FieldNode(
            alias=None,
            name=NameNode(value="tags"),
            arguments=(),
            directives=(),
            selection_set=None,
        )
        posts_field = FieldNode(
            alias=None,
            name=NameNode(value="posts"),
            arguments=(),
            directives=(),
            selection_set=SelectionSetNode(selections=[tags_field]),
        )
        users_field = FieldNode(
            alias=None,
            name=NameNode(value="users"),
            arguments=(),
            directives=(),
            selection_set=SelectionSetNode(selections=[posts_field]),
        )
        info = SimpleNamespace(
            field_nodes=[users_field],
            fragments={},
            context={"session": sa_session},
        )
        users = backend.apply_optimizer_hints(
            None, backend.get_default_queryset(User), info
        )
        assert len(users) >= 1

    def test_build_sa_field_clause_skips_missing_column(self, Post):
        @strawberry.input
        class BadField:
            missing: StringLookup | None = strawberry.UNSET

        field = BadField()
        object.__setattr__(field, "missing", StringLookup(exact="x"))
        assert _build_sa_field_clause(field, Post) is None

    def test_build_sa_filter_m2m_is_null_raises(self, Post):
        @strawberry.input
        class TagsNull:
            is_null: bool | None = True

        @strawberry.input
        class PostObjectWithTags:
            tags: TagsNull | None = strawberry.UNSET

        @strawberry.input
        class PostFilterWithTags:
            object: PostObjectWithTags | None = strawberry.UNSET

        filt = PostFilterWithTags(
            object=PostObjectWithTags(tags=TagsNull(is_null=True))
        )
        with pytest.raises(ValueError, match="is_null is not supported"):
            _build_sa_filter(filt, Post)

    def test_build_sa_filter_reverse_many_relation(self, Post):
        from tests.backends.sqlalchemy.fixtures import CommentFilter

        @strawberry.input
        class PostObjectWithComments:
            comments: CommentFilter | None = strawberry.UNSET

        @strawberry.input
        class PostFilterWithComments:
            object: PostObjectWithComments | None = strawberry.UNSET

        filt = PostFilterWithComments(
            object=PostObjectWithComments(
                comments=CommentFilter(
                    field=CommentFilter._field_type(body=StringLookup(exact="c1"))
                )
            )
        )
        clause, _ = _build_sa_filter(filt, Post)
        assert clause is not None

    def test_build_sa_filter_custom_filter_field(self, Post, sa_session):
        from strawberry_orm import filter_field

        backend = SQLAlchemyBackend(dialect="sqlite")

        @backend.filter_type(Post)
        class CustomPostFilter:
            title: auto

            @filter_field
            def custom(self, value: str, query, info=None):
                return query.where(Post.title == value)

        filt = CustomPostFilter(custom="Hello")
        _, query = _build_sa_filter(filt, Post, query=select(Post))
        assert query is not None

    def test_build_reference_lookup_type_error(self, Post):
        with pytest.raises(TypeError, match="Expected ReferenceLookup"):
            _build_reference_lookup_clauses(Post.id, StringLookup(exact="1"))

    def test_extract_sa_group_skips_duplicate_columns(self, Post):
        @strawberry.input
        class GroupField:
            author_id: bool | None = True

        @strawberry.input
        class GroupEntry:
            field: GroupField | None = strawberry.UNSET

        subq = select(Post).subquery()
        cols, keys = _extract_sa_group_columns(
            [
                GroupEntry(field=GroupField(author_id=True)),
                GroupEntry(field=GroupField(author_id=True)),
            ],
            Post,
            subq,
        )
        assert keys == ["author_id"]
        assert len(cols) == 1

    def test_extract_overlapping_order_skips_missing_column(self, Post):
        @strawberry.input
        class OrderField:
            missing: Ordering | None = Ordering.ASC

        @strawberry.input
        class OrderEntry:
            field: OrderField | None = strawberry.UNSET

        subq = select(Post).subquery()
        clauses = _extract_overlapping_order(
            OrderEntry(field=OrderField(missing=Ordering.ASC)),
            {"missing"},
            Post,
            subq,
        )
        assert clauses == []

    def test_build_sa_order_from_input_pk_fallback(self, Post):
        @strawberry.input
        class EmptyField:
            pass

        @strawberry.input
        class EmptyOrder:
            field: EmptyField | None = strawberry.UNSET

        clauses = _build_sa_order_from_input(EmptyOrder(), Post)
        assert len(clauses) == 1

    def test_build_sa_order_field_skips_missing_column(self, Post):
        @strawberry.input
        class BadOrderField:
            missing: Ordering | None = Ordering.DESC

        assert _build_sa_order_field(BadOrderField(), Post) == []

    def test_build_sa_order_field_clauses_skips_missing_columns(self, Post):
        from strawberry_orm.backends.sqlalchemy import (
            _build_sa_order_field,
            _build_sa_order_field_clauses,
        )

        @strawberry.input
        class BadOrderField:
            missing: Ordering | None = Ordering.DESC

        @strawberry.input
        class BadOrderEntry:
            field: BadOrderField | None = strawberry.UNSET

        assert _build_sa_order_field_clauses(BadOrderEntry(), Post) == []
        assert _build_sa_order_field(BadOrderField(), Post) == []

        @strawberry.input
        class MixedOrderField:
            title: Ordering | None = None
            body: Ordering | None = Ordering.DESC

        @strawberry.input
        class MixedOrderEntry:
            field: MixedOrderField | None = strawberry.UNSET

        clauses = _build_sa_order_field_clauses(
            MixedOrderEntry(field=MixedOrderField(body=Ordering.DESC)),
            Post,
        )
        assert len(clauses) == 1

        @strawberry.input
        class OnlyMissingField:
            missing: Ordering | None = Ordering.ASC

        @strawberry.input
        class OnlyMissingEntry:
            field: OnlyMissingField | None = strawberry.UNSET

        assert (
            _build_sa_order_field_clauses(
                OnlyMissingEntry(field=OnlyMissingField(missing=Ordering.ASC)),
                Post,
            )
            == []
        )

    def test_extract_overlapping_order_skips_non_group_columns(self, Post):
        @strawberry.input
        class OrderField:
            title: Ordering | None = Ordering.ASC

        @strawberry.input
        class OrderEntry:
            field: OrderField | None = strawberry.UNSET

        subq = select(Post).subquery()
        assert (
            _extract_overlapping_order(
                OrderEntry(field=OrderField(title=Ordering.ASC)),
                {"author_id"},
                Post,
                subq,
            )
            == []
        )
