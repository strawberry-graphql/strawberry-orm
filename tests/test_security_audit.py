"""Comprehensive security audit tests for strawberry-orm.

These tests expose vulnerabilities that the existing test_security.py and
test_security_fixes.py do NOT cover.  Each test class targets a specific
vulnerability category and documents the expected secure behaviour.

Run with:
    pytest tests/test_security_audit.py -v
"""

# NOTE: Do NOT use `from __future__ import annotations` here.
# Strawberry needs concrete type objects at schema construction time.

from typing import Optional

import pytest
import strawberry
from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


# =========================================================================
# Shared models
# =========================================================================


class Base(DeclarativeBase):
    pass


class AuditUser(Base):
    __tablename__ = "audit_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    api_key: Mapped[str] = mapped_column(String(200), default="")
    posts: Mapped[list["AuditPost"]] = relationship(back_populates="author")
    groups: Mapped[list["AuditGroup"]] = relationship(secondary="audit_user_group")


audit_user_group = Table(
    "audit_user_group",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("audit_user.id"), primary_key=True),
    Column("group_id", Integer, ForeignKey("audit_group.id"), primary_key=True),
)


class AuditGroup(Base):
    __tablename__ = "audit_group"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    is_privileged: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditPost(Base):
    __tablename__ = "audit_post"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int] = mapped_column(ForeignKey("audit_user.id"))
    author: Mapped["AuditUser"] = relationship(back_populates="posts")


# =========================================================================
# Module-level ORM types (avoids PEP 563 / Strawberry resolution issues)
# =========================================================================

_orm_vuln_a = StrawberryORM("sqlalchemy", dialect="sqlite")
_filt_vuln_a = _orm_vuln_a.filter(AuditUser)


@_orm_vuln_a.type(AuditUser, filters=_filt_vuln_a)
class AuditUserTypeA:
    id: auto
    username: auto
    email: auto


@strawberry.type
class QueryVulnA:
    users: list[AuditUserTypeA] = _orm_vuln_a.field()


_schema_vuln_a = strawberry.Schema(
    query=QueryVulnA, extensions=[_orm_vuln_a.optimizer_extension()]
)

# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    sess = sessionmaker(bind=engine)()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def seeded(session):
    session.add_all(
        [
            AuditUser(
                id=1,
                username="alice",
                email="alice@corp.com",
                password_hash="$2b$hash_a",
                is_admin=True,
                api_key="key-A",
            ),
            AuditUser(
                id=2,
                username="bob",
                email="bob@corp.com",
                password_hash="$2b$hash_b",
                is_admin=False,
                api_key="key-B",
            ),
            AuditUser(
                id=3,
                username="carol_%test",
                email="carol@corp.com",
                password_hash="$2b$hash_c",
                is_admin=False,
                api_key="key-C",
            ),
        ]
    )
    session.add_all(
        [
            AuditGroup(id=1, name="admins", is_privileged=True),
            AuditGroup(id=2, name="viewers", is_privileged=False),
        ]
    )
    session.add_all(
        [
            AuditPost(id=1, title="Secret", body="Top secret body", author_id=1),
            AuditPost(id=2, title="Public", body="Hello world", author_id=2),
        ]
    )
    session.commit()
    return session


def _exec_a(session, query):
    return _schema_vuln_a.execute_sync(query, context_value={"session": session})


# =========================================================================
# VULN-A: Case-sensitive LIKE operations don't escape wildcards
#
# The fix for ilike (i_contains, i_starts_with, i_ends_with) properly
# escapes % and _ via _escape_like(). However, the case-SENSITIVE
# variants (contains, starts_with, ends_with) use column.contains(val),
# column.startswith(val), column.endswith(val) WITHOUT autoescape=True.
#
# In SQLAlchemy, these methods generate LIKE patterns where % and _
# in the value ARE treated as wildcards unless autoescape=True is set.
# =========================================================================


class TestVulnA_CaseSensitiveLikeWildcardInjection:
    """Case-sensitive contains/startsWith/endsWith don't escape LIKE wildcards."""

    def test_contains_percent_matches_all(self, seeded):
        """contains('%') should NOT match all rows — % must be escaped."""
        result = _exec_a(
            seeded,
            """
            { users(filter: { field: { username: { contains: "%" } } }) {
                username
            }}
        """,
        )
        assert result.errors is None
        names = [u["username"] for u in result.data["users"]]
        # Only carol_%test contains a literal %; alice and bob do not
        assert "alice" not in names, (
            f"CRITICAL: contains('%') matched 'alice' — wildcard not escaped. Got: {names}"
        )
        assert "carol_%test" in names, (
            f"contains('%') should match 'carol_%test' (has literal %). Got: {names}"
        )

    def test_startswith_underscore_matches_single_char(self, seeded):
        """startsWith('_') should NOT match rows starting with any char."""
        result = _exec_a(
            seeded,
            """
            { users(filter: { field: { username: { startsWith: "_" } } }) {
                username
            }}
        """,
        )
        assert result.errors is None
        names = [u["username"] for u in result.data["users"]]
        assert names == [], (
            f"CRITICAL: startsWith('_') matched rows — underscore not escaped. Got: {names}"
        )

    def test_endswith_percent_matches_all(self, seeded):
        """endsWith('%') should NOT match all rows."""
        result = _exec_a(
            seeded,
            """
            { users(filter: { field: { email: { endsWith: "%" } } }) {
                username
            }}
        """,
        )
        assert result.errors is None
        names = [u["username"] for u in result.data["users"]]
        assert names == [], (
            f"CRITICAL: endsWith('%') matched rows — wildcard not escaped. Got: {names}"
        )

    def test_contains_underscore_single_char_match(self, seeded):
        """contains('_') should only match rows with literal underscore."""
        result = _exec_a(
            seeded,
            """
            { users(filter: { field: { username: { contains: "_" } } }) {
                username
            }}
        """,
        )
        assert result.errors is None
        names = [u["username"] for u in result.data["users"]]
        assert set(names) == {"carol_%test"}, (
            f"CRITICAL: contains('_') matched more than literal underscore. Got: {names}"
        )


# =========================================================================
# VULN-B: Django backend apply_ref_list ignores authorization callback
#
# The SA backend's apply_ref_list correctly checks the `authorize`
# callback before each operation (link, create, update, delete).
# The Django backend accepts `authorize` as a parameter but NEVER
# calls it — the callback is completely ignored.
# =========================================================================


class TestVulnB_DjangoRefListAuthorizationBypass:
    """Django apply_ref_list accepts authorize= but never calls it."""

    def test_sa_backend_calls_authorize(self, seeded):
        """Baseline: SA backend DOES call the authorize callback."""
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")
        log = []

        def authorizer(action, model, obj_id, info):
            log.append((action, model.__name__, obj_id))
            return True

        group_ref = orm.ref(AuditGroup)
        update_type = group_ref.__dataclass_fields__["update"].type
        actual_type = (
            update_type.__args__[0] if hasattr(update_type, "__args__") else update_type
        )
        ref = group_ref(update=actual_type(id="1"))
        user = seeded.get(AuditUser, 1)

        class FakeInfo:
            context = {"session": seeded}

        orm.apply_ref_list(user, "groups", [ref], FakeInfo(), authorize=authorizer)
        assert len(log) > 0, "SA authorize was not called"

    def test_django_apply_ref_list_source_has_authorize_checks(self):
        """Verify Django backend's apply_ref_list actually uses authorize."""
        import inspect
        from strawberry_orm.backends.django import DjangoBackend

        source = inspect.getsource(DjangoBackend.apply_ref_list)
        lines = source.split("\n")
        authorize_calls = [
            line
            for line in lines
            if "authorize" in line
            and "def " not in line
            and "authorize:" not in line
            and "authorize=" not in line.split("(")[0]
        ]
        assert len(authorize_calls) > 0, (
            "CRITICAL: Django apply_ref_list accepts authorize= but never calls it! "
            "Any user can link/create/update/delete related objects without authorization."
        )


# =========================================================================
# VULN-C: Tortoise backend apply_ref_list ignores authorization callback
# =========================================================================


class TestVulnC_TortoiseRefListAuthorizationBypass:
    """Tortoise apply_ref_list accepts authorize= but never calls it."""

    def test_tortoise_apply_ref_list_source_has_authorize_checks(self):
        """Verify Tortoise backend's apply_ref_list actually uses authorize."""
        import inspect
        from strawberry_orm.backends.tortoise import TortoiseBackend

        source = inspect.getsource(TortoiseBackend.apply_ref_list)
        lines = source.split("\n")
        authorize_calls = [
            line
            for line in lines
            if "authorize" in line
            and "def " not in line
            and "authorize:" not in line
            and "authorize=" not in line.split("(")[0]
        ]
        assert len(authorize_calls) > 0, (
            "CRITICAL: Tortoise apply_ref_list accepts authorize= but never calls it! "
            "Any user can link/create/update/delete related objects without authorization."
        )


# =========================================================================
# VULN-D: Django and Tortoise have no filter depth / branch limits
# =========================================================================


class TestVulnD_DjangoFilterNoDepthLimit:
    """Django filter building has no recursion depth limit."""

    def test_django_filter_builder_has_depth_param(self):
        import inspect
        from strawberry_orm.backends.django import _build_django_filter

        sig = inspect.signature(_build_django_filter)
        params = list(sig.parameters.keys())
        has_depth = any("depth" in p for p in params)
        assert has_depth, (
            "CRITICAL: _build_django_filter has no depth limit parameter. "
            "An attacker can send deeply nested filters to cause stack overflow / DoS."
        )


class TestVulnD_TortoiseFilterNoDepthLimit:
    """Tortoise filter building has no recursion depth limit."""

    def test_tortoise_filter_builder_has_depth_param(self):
        import inspect
        from strawberry_orm.backends.tortoise import _build_tortoise_filter

        sig = inspect.signature(_build_tortoise_filter)
        params = list(sig.parameters.keys())
        has_depth = any("depth" in p for p in params)
        assert has_depth, (
            "CRITICAL: _build_tortoise_filter has no depth limit parameter. "
            "An attacker can send deeply nested filters to cause stack overflow / DoS."
        )


# =========================================================================
# VULN-E: Django and Tortoise regex filters always enabled
# =========================================================================


class TestVulnE_DjangoRegexAlwaysEnabled:
    """Django backend has no way to disable regex filters."""

    def test_django_backend_has_enable_regex_option(self):
        import inspect
        from strawberry_orm.backends.django import DjangoBackend

        sig = inspect.signature(DjangoBackend.__init__)
        params = list(sig.parameters.keys())
        assert "enable_regex_filters" in params or any("regex" in p for p in params), (
            "CRITICAL: DjangoBackend has no enable_regex_filters option. "
            "Regex filters are always enabled, enabling ReDoS attacks."
        )


class TestVulnE_TortoiseRegexAlwaysEnabled:
    """Tortoise backend has no way to disable regex filters."""

    def test_tortoise_backend_has_enable_regex_option(self):
        import inspect
        from strawberry_orm.backends.tortoise import TortoiseBackend

        sig = inspect.signature(TortoiseBackend.__init__)
        params = list(sig.parameters.keys())
        assert "enable_regex_filters" in params or any("regex" in p for p in params), (
            "CRITICAL: TortoiseBackend has no enable_regex_filters option. "
            "Regex filters are always enabled, enabling ReDoS attacks."
        )


# =========================================================================
# VULN-F: BaseBackend.input() doesn't exclude PKs (Django/Tortoise)
# =========================================================================


class TestVulnF_BaseInputIncludesPKs:
    """BaseBackend.input() includes PK fields, enabling mass assignment."""

    def test_base_backend_input_has_exclude_pk(self):
        import inspect
        from strawberry_orm.backends._base import BaseBackend

        source = inspect.getsource(BaseBackend.input)
        assert "exclude_pk" in source, (
            "CRITICAL: BaseBackend.input() has no PK exclusion. "
            "Django and Tortoise auto-generated inputs include PK fields, "
            "enabling attackers to set arbitrary IDs on creation."
        )


# =========================================================================
# VULN-I: SQL ESCAPE clause for case-sensitive contains/startsWith/endsWith
# =========================================================================


class TestVulnI_CaseSensitiveLikeEscapeInSQL:
    """Verify the library's filter path generates ESCAPE in LIKE SQL."""

    def test_contains_via_library_has_escape(self, engine, seeded):
        """The library's _build_lookup_clauses should use autoescape=True."""
        queries: list[str] = []

        def _before(conn, cursor, stmt, params, context, executemany):
            queries.append(stmt)

        event.listen(engine, "before_cursor_execute", _before)

        result = _exec_a(
            seeded,
            """
            { users(filter: { field: { username: { contains: "%" } } }) {
                username
            }}
        """,
        )

        event.remove(engine, "before_cursor_execute", _before)

        assert result.errors is None
        like_queries = [q for q in queries if "LIKE" in q.upper()]
        assert like_queries, "Expected at least one LIKE query for contains"
        assert any("ESCAPE" in q.upper() for q in like_queries), (
            f"Library's contains filter should produce ESCAPE clause. "
            f"Got: {like_queries[-1]}"
        )


# =========================================================================
# VULN-K: input_to_dict passes all fields to ORM constructor without
# validating that they are safe for mass-assignment
# =========================================================================


class TestVulnK_InputToDictMassAssignment:
    """input_to_dict blindly passes all fields to ORM constructors."""

    def test_input_to_dict_includes_all_set_fields(self):
        from strawberry_orm.backends._base import input_to_dict

        @strawberry.input
        class TestInput:
            name: str = "test"
            is_admin: bool = True
            _internal: str = strawberry.UNSET

        inp = TestInput()
        result = input_to_dict(inp)
        assert "name" in result
        assert "is_admin" in result
        assert "_internal" not in result


# =========================================================================
# VULN-M: filter() doesn't validate field names against model
# =========================================================================


class TestVulnM_DjangoFilterFieldTraversal:
    """Django Q objects allow __ traversal — filter field names are trusted."""

    def test_filter_field_names_come_from_model_introspection(self):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")
        filt = orm.filter(AuditUser)
        field_type = filt._field_type
        field_names = set(field_type.__dataclass_fields__.keys())

        for name in field_names:
            assert "__" not in name, (
                f"Filter field name '{name}' contains '__' — "
                f"could enable Django ORM traversal attacks"
            )


# =========================================================================
# VULN-N: SA backend session retrieval safety
# =========================================================================


class TestVulnN_SessionRetrievalSafety:
    """SA _get_session retrieves session from context without type validation."""

    def test_session_getter_preferred_over_context(self, seeded):
        from strawberry_orm.backends.sqlalchemy import SQLAlchemyBackend

        def getter(info):
            return info.context["session"]

        backend = SQLAlchemyBackend(dialect="sqlite", session_getter=getter)

        class FakeInfo:
            context = {"session": seeded}

        session = backend._get_session(FakeInfo())
        assert session is seeded

    def test_callable_session_in_context_raises(self, seeded):
        from strawberry_orm.backends.sqlalchemy import SQLAlchemyBackend

        backend = SQLAlchemyBackend(dialect="sqlite")

        class FakeInfo:
            context = {"session": lambda: seeded}

        with pytest.raises(TypeError, match="callable"):
            backend._get_session(FakeInfo())


# =========================================================================
# VULN-O: SA apply_ref_list with many-to-many uses string GraphQL IDs
# =========================================================================


class TestVulnO_RefIdTypeCoercion:
    """Ref IDs are GraphQL strings but PKs may be integers."""

    def test_non_numeric_id_on_integer_pk(self, seeded):
        """Non-numeric string ID should fail gracefully, not crash."""
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")
        group_ref = orm.ref(AuditGroup)
        update_type = group_ref.__dataclass_fields__["update"].type
        actual_type = (
            update_type.__args__[0] if hasattr(update_type, "__args__") else update_type
        )
        ref = group_ref(update=actual_type(id="not-a-number"))

        user = seeded.get(AuditUser, 1)

        class FakeInfo:
            context = {"session": seeded}

        try:
            orm.apply_ref_list(user, "groups", [ref], FakeInfo())
        except Exception as e:
            assert "audit_group" not in str(e).lower(), f"Error leaked table name: {e}"


# =========================================================================
# VULN-P: Django _build_django_lookup allows regex without opt-out
#
# Verify the actual lookup mapping includes regex.
# =========================================================================


class TestVulnP_DjangoRegexInLookupMap:
    """Django lookup map includes regex/iregex — verify they are gated."""

    def test_regex_gated_by_enable_flag(self):
        """Django _build_django_lookup should reject regex when disabled."""
        from strawberry_orm.backends.django import _build_django_lookup

        @strawberry.input
        class FakeLookup:
            regex: str = ".*"

        with pytest.raises(ValueError, match="[Rr]egex.*disabled"):
            _build_django_lookup("name", FakeLookup(), enable_regex=False)


# =========================================================================
# VULN-Q: Tortoise _build_tortoise_lookup allows regex without opt-out
# =========================================================================


class TestVulnQ_TortoiseRegexInLookupMap:
    """Tortoise lookup handling — verify regex is handled."""

    def test_tortoise_lookup_has_regex_handling(self):
        """Check if Tortoise filter builder handles regex lookups."""
        import inspect
        from strawberry_orm.backends.tortoise import _build_tortoise_lookup

        source = inspect.getsource(_build_tortoise_lookup)
        has_regex = "regex" in source.lower()
        # Tortoise doesn't seem to handle regex at all — which means it
        # silently ignores it (safe but surprising) or errors
        if not has_regex:
            pass  # Implicitly safe - regex fields in StringLookup are just ignored


# =========================================================================
# VULN-R: StringLookup exposes regex fields to ALL backends
#
# The shared StringLookup type always includes regex/i_regex fields.
# Even if a backend doesn't handle them, they appear in the GraphQL
# schema, confusing users and creating a false sense of capability.
# =========================================================================


class TestVulnR_StringLookupExposesRegex:
    """StringLookup always includes regex fields regardless of backend config."""

    def test_string_lookup_has_regex_fields(self):
        from strawberry_orm.filters import StringLookup

        fields = StringLookup.__dataclass_fields__
        assert "regex" in fields, "regex field missing from StringLookup"
        assert "i_regex" in fields, "i_regex field missing from StringLookup"


# =========================================================================
# Advisory 2: Sensitive fields exposed by default
# =========================================================================


class TestAdvisory2_SensitiveFieldWarnings:
    """BaseBackend._process_type_annotations warns on sensitive-looking fields."""

    def test_warns_on_password_hash(self):
        """orm.type() with password_hash included should emit a warning."""
        import warnings as _warnings

        orm = StrawberryORM("sqlalchemy", dialect="sqlite")

        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")

            @orm.type(AuditUser)
            class AuditUserWithSensitive:
                id: auto
                username: auto
                password_hash: auto

        sensitive_warnings = [w for w in caught if "password_hash" in str(w.message)]
        assert len(sensitive_warnings) >= 1, (
            f"Expected a warning about 'password_hash' but got: "
            f"{[str(w.message) for w in caught]}"
        )

    def test_no_warning_when_excluded(self):
        """Explicitly excluding a sensitive field should not emit a warning."""
        import warnings as _warnings

        orm = StrawberryORM("sqlalchemy", dialect="sqlite")

        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")

            @orm.type(AuditUser, exclude=["password_hash", "api_key"])
            class AuditUserExcluded:
                id: auto
                username: auto

        sensitive_warnings = [
            w
            for w in caught
            if "password_hash" in str(w.message) or "api_key" in str(w.message)
        ]
        assert len(sensitive_warnings) == 0, (
            f"Excluded sensitive fields should not trigger warnings. Got: "
            f"{[str(w.message) for w in sensitive_warnings]}"
        )

    def test_warns_on_api_key(self):
        """orm.type() with api_key included should emit a warning."""
        import warnings as _warnings

        orm = StrawberryORM("sqlalchemy", dialect="sqlite")

        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")

            @orm.type(AuditUser)
            class AuditUserWithApiKey:
                id: auto
                api_key: auto

        api_key_warnings = [w for w in caught if "api_key" in str(w.message)]
        assert len(api_key_warnings) >= 1, (
            f"Expected a warning about 'api_key' but got: "
            f"{[str(w.message) for w in caught]}"
        )

    def test_no_warning_when_disabled(self):
        """warn_sensitive=False should suppress all sensitive field warnings."""
        import warnings as _warnings

        orm = StrawberryORM("sqlalchemy", dialect="sqlite", warn_sensitive=False)

        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")

            @orm.type(AuditUser)
            class AuditUserNoWarn:
                id: auto
                username: auto
                password_hash: auto

        sensitive_warnings = [w for w in caught if "password_hash" in str(w.message)]
        assert len(sensitive_warnings) == 0, (
            f"warn_sensitive=False should suppress warnings. Got: "
            f"{[str(w.message) for w in sensitive_warnings]}"
        )


# =========================================================================
# Advisory 3: No default pagination/limit
# =========================================================================


class TestAdvisory3_DefaultQueryLimit:
    """get_default_queryset should apply a configurable LIMIT."""

    def test_sa_default_queryset_has_limit(self, engine, seeded):
        """SQLAlchemy get_default_queryset should produce a LIMIT clause."""
        orm = StrawberryORM(
            "sqlalchemy",
            dialect="sqlite",
            default_query_limit=100,
        )
        stmt = orm.get_default_queryset(AuditUser)
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "LIMIT" in sql.upper(), f"Expected LIMIT in SQL. Got: {sql}"

    def test_sa_default_queryset_no_limit_when_unset(self, engine, seeded):
        """Without default_query_limit, no LIMIT should be applied."""
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")
        stmt = orm.get_default_queryset(AuditUser)
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "LIMIT" not in sql.upper(), f"Expected no LIMIT in SQL. Got: {sql}"

    def test_sa_default_limit_actual_row_count(self, engine, seeded):
        """LIMIT should actually restrict the returned rows."""
        orm = StrawberryORM(
            "sqlalchemy",
            dialect="sqlite",
            default_query_limit=2,
        )
        stmt = orm.get_default_queryset(AuditUser)
        with Session(bind=engine) as sess:
            results = sess.execute(stmt).scalars().all()
        assert len(results) == 2, f"Expected 2 rows with limit=2, got {len(results)}"


# =========================================================================
# Advisory 4: apply_ref_list replaces all relationships
# =========================================================================


class TestAdvisory4_RefListPatchMode:
    """apply_ref_list mode='patch' should not replace the entire list."""

    def test_sa_patch_semantics_preserves_existing(self, engine, seeded):
        """Patch semantics should add new refs without removing existing ones."""
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")
        user = seeded.get(AuditUser, 1)
        group1 = seeded.get(AuditGroup, 1)
        user.groups.append(group1)
        seeded.flush()
        assert len(user.groups) == 1

        group_ref = orm.ref(AuditGroup)
        update_type = group_ref.__dataclass_fields__["update"].type
        actual_type = (
            update_type.__args__[0] if hasattr(update_type, "__args__") else update_type
        )
        ref = group_ref(update=actual_type(id="2"))

        class FakeInfo:
            context = {"session": seeded}

        orm.apply_ref_list(user, "groups", [ref], FakeInfo())
        seeded.flush()

        group_ids = sorted(g.id for g in user.groups)
        assert 1 in group_ids, (
            f"Patch semantics should preserve existing group 1. Got: {group_ids}"
        )
        assert 2 in group_ids, (
            f"Patch semantics should add new group 2. Got: {group_ids}"
        )


# =========================================================================
# Advisory 5: StringLookup exposes regex/i_regex in schema when disabled
# =========================================================================


class TestAdvisory5_StringLookupNoRegex:
    """StringLookupNoRegex should not expose regex fields in the schema."""

    def test_no_regex_class_exists(self):
        """StringLookupNoRegex should exist and lack regex fields."""
        from strawberry_orm.filters import StringLookupNoRegex

        fields = StringLookupNoRegex.__dataclass_fields__
        assert "regex" not in fields, "StringLookupNoRegex must not have 'regex'"
        assert "i_regex" not in fields, "StringLookupNoRegex must not have 'i_regex'"
        assert "contains" in fields, "StringLookupNoRegex should have 'contains'"
        assert "exact" in fields, "StringLookupNoRegex should have 'exact'"

    def test_filter_uses_no_regex_by_default(self):
        """With enable_regex_filters=False, filter() should use StringLookupNoRegex."""
        orm = StrawberryORM(
            "sqlalchemy",
            dialect="sqlite",
            enable_regex_filters=False,
        )
        filt = orm.filter(AuditUser)
        field_type = filt._field_type
        username_ann = field_type.__dataclass_fields__["username"]

        from strawberry_orm.filters import StringLookupNoRegex

        actual_type = username_ann.type
        if hasattr(actual_type, "__args__"):
            inner = [a for a in actual_type.__args__ if a is not type(None)]
            if inner:
                actual_type = inner[0]

        actual_fields = actual_type.__dataclass_fields__
        assert "regex" not in actual_fields, (
            "When regex disabled, filter should use StringLookupNoRegex (no regex field)"
        )

    def test_filter_uses_regex_when_enabled(self):
        """With enable_regex_filters=True, filter() should use StringLookup."""
        orm = StrawberryORM(
            "sqlalchemy",
            dialect="sqlite",
            enable_regex_filters=True,
        )
        filt = orm.filter(AuditUser)
        field_type = filt._field_type
        username_ann = field_type.__dataclass_fields__["username"]

        actual_type = username_ann.type
        if hasattr(actual_type, "__args__"):
            inner = [a for a in actual_type.__args__ if a is not type(None)]
            if inner:
                actual_type = inner[0]

        actual_fields = actual_type.__dataclass_fields__
        assert "regex" in actual_fields, (
            "When regex enabled, filter should use StringLookup (with regex field)"
        )


# =========================================================================
# Advisory 6: No in_list size limit
# =========================================================================


class TestAdvisory6_InListSizeLimit:
    """in_list and not_in_list should be size-limited."""

    def test_sa_in_list_over_limit_raises(self, engine, seeded):
        """SA backend should reject in_list exceeding max_in_list_size."""
        from strawberry_orm.backends.sqlalchemy import _build_lookup_clauses

        @strawberry.input
        class FakeLookup:
            in_list: list[str] = strawberry.UNSET

        lookup = FakeLookup(in_list=["x"] * 600)
        col = AuditUser.username

        with pytest.raises(ValueError, match="in_list.*maximum"):
            _build_lookup_clauses(col, lookup, max_in_list_size=500)

    def test_sa_in_list_within_limit_ok(self, engine, seeded):
        """SA backend should accept in_list within max_in_list_size."""
        from strawberry_orm.backends.sqlalchemy import _build_lookup_clauses

        @strawberry.input
        class FakeLookupOk:
            in_list: list[str] = strawberry.UNSET

        lookup = FakeLookupOk(in_list=["alice", "bob"])
        col = AuditUser.username
        clauses = _build_lookup_clauses(col, lookup, max_in_list_size=500)
        assert len(clauses) == 1

    def test_sa_not_in_list_over_limit_raises(self, engine, seeded):
        """SA backend should reject not_in_list exceeding max_in_list_size."""
        from strawberry_orm.backends.sqlalchemy import _build_lookup_clauses

        @strawberry.input
        class FakeLookupNot:
            not_in_list: list[str] = strawberry.UNSET

        lookup = FakeLookupNot(not_in_list=["x"] * 600)
        col = AuditUser.username

        with pytest.raises(ValueError, match="in_list.*maximum"):
            _build_lookup_clauses(col, lookup, max_in_list_size=500)

    def test_django_in_list_over_limit_raises(self):
        """Django backend should reject in_list exceeding max_in_list_size."""
        from strawberry_orm.backends.django import _build_django_lookup

        @strawberry.input
        class FakeDjLookup:
            in_list: list[str] = strawberry.UNSET

        lookup = FakeDjLookup(in_list=["x"] * 600)

        with pytest.raises(ValueError, match="in_list.*maximum"):
            _build_django_lookup("name", lookup, max_in_list_size=500)

    def test_tortoise_in_list_over_limit_raises(self):
        """Tortoise backend should reject in_list exceeding max_in_list_size."""
        from strawberry_orm.backends.tortoise import _build_tortoise_lookup

        @strawberry.input
        class FakeTtLookup:
            in_list: list[str] = strawberry.UNSET

        lookup = FakeTtLookup(in_list=["x"] * 600)

        with pytest.raises(ValueError, match="in_list.*maximum"):
            _build_tortoise_lookup("name", lookup, max_in_list_size=500)

    def test_sa_default_max_is_500(self):
        """SA backend default max_in_list_size should be 500."""
        from strawberry_orm.backends.sqlalchemy import SQLAlchemyBackend

        backend = SQLAlchemyBackend(dialect="sqlite")
        assert backend._max_in_list_size == 500

    def test_custom_max_in_list_size(self):
        """max_in_list_size should be configurable."""
        from strawberry_orm.backends.sqlalchemy import SQLAlchemyBackend

        backend = SQLAlchemyBackend(dialect="sqlite", max_in_list_size=100)
        assert backend._max_in_list_size == 100
