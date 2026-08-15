"""Success-criteria tests for every security vulnerability.

Each test in this file asserts the DESIRED secure behaviour.
They are expected to **FAIL** against the current codebase and
**PASS** once the corresponding fix has been applied.

Run with:
    pytest tests/test_security_fixes.py -v
"""

import pytest
import strawberry
from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Table,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto

# =========================================================================
# Shared models (used by multiple test classes)
# =========================================================================


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "fix_account"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    api_key: Mapped[str] = mapped_column(String(200), default="")
    groups: Mapped[list["Role"]] = relationship(secondary="fix_user_role")


user_role_table = Table(
    "fix_user_role",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("fix_account.id"), primary_key=True),
    Column("role_id", Integer, ForeignKey("fix_role.id"), primary_key=True),
)


class Role(Base):
    __tablename__ = "fix_role"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)


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
            Account(
                id=1,
                username="alice",
                email="alice@corp.com",
                password_hash="$2b$12$hash_alice",
                is_admin=True,
                api_key="ak-secret-1",
            ),
            Account(
                id=2,
                username="bob",
                email="bob@corp.com",
                password_hash="$2b$12$hash_bob",
                is_admin=False,
                api_key="ak-secret-2",
            ),
            Account(
                id=3,
                username="a%dmin",
                email="a%dmin@corp.com",
                password_hash="$2b$12$hash_pct",
                is_admin=False,
                api_key="ak-pct",
            ),
        ]
    )
    session.add_all(
        [
            Role(id=1, name="admin", is_superuser=True),
            Role(id=2, name="viewer", is_superuser=False),
        ]
    )
    session.commit()
    return session


# =========================================================================
# FIX 1 — LIKE wildcard injection
#
# Plan:
#   File  : src/strawberry_orm/backends/sqlalchemy.py
#   Change: Add _escape_like(val) helper that escapes %, _, and \.
#           Use it in the three ilike() calls (i_contains, i_starts_with,
#           i_ends_with) with escape="\\".
#   Scope : ~10 lines added, 3 lines changed.
# =========================================================================


class TestFix1_LikeWildcardEscaping:
    """After the fix, LIKE wildcards (% and _) in user input must be
    treated as literal characters, not pattern wildcards."""

    @pytest.fixture(autouse=True)
    def _setup(self, seeded):
        self.session = seeded
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        filt = orm.filter(Account)

        @orm.type(Account, filters=filt)
        class AccountType:
            id: auto
            username: auto
            email: auto

        @strawberry.type
        class Q:
            accounts: list[AccountType] = orm.field.auto()

        self.schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])

    def _exec(self, query):
        return self.schema.execute_sync(query, context_value={"session": self.session})

    def test_icontains_percent_matches_only_literal(self):
        """iContains('%') should only match rows containing a literal '%'."""
        result = self._exec("""
            { accounts(filter: { field: { username: { iContains: "%" } } }) {
                username
            }}
        """)
        assert result.errors is None
        names = [a["username"] for a in result.data["accounts"]]
        assert names == ["a%dmin"], f"Expected only 'a%dmin' (literal %); got {names}"

    def test_istartswith_underscore_is_literal(self):
        """iStartsWith('_') should NOT match 'alice' (a != _)."""
        result = self._exec("""
            { accounts(filter: { field: { username: { iStartsWith: "_" } } }) {
                username
            }}
        """)
        assert result.errors is None
        names = [a["username"] for a in result.data["accounts"]]
        assert names == [], f"Expected no matches for literal '_'; got {names}"

    def test_iendswith_percent_matches_only_literal(self):
        """iEndsWith('%') should match nothing (no username ends in %)."""
        result = self._exec("""
            { accounts(filter: { field: { email: { iEndsWith: "%" } } }) {
                username
            }}
        """)
        assert result.errors is None
        names = [a["username"] for a in result.data["accounts"]]
        assert names == [], f"Expected no matches for literal '%' at end; got {names}"

    def test_icontains_backslash_is_literal(self):
        """Backslash should be treated literally, not as an escape char."""
        result = self._exec(r"""
            { accounts(filter: { field: { username: { iContains: "\\" } } }) {
                username
            }}
        """)
        assert result.errors is None
        names = [a["username"] for a in result.data["accounts"]]
        assert names == [], f"Expected no matches for literal backslash; got {names}"


# =========================================================================
# FIX 2 — input() auto-excludes primary keys
#
# Plan:
#   File  : All backends' input() methods.
#   Change: Add exclude_pk=True (default) that filters out PK columns.
#           Users can pass exclude_pk=False to opt back in.
#   Scope : ~5 lines per backend (SA, Django, Tortoise).
# =========================================================================


class TestFix2_InputExcludesPrimaryKeys:
    """input() should exclude primary-key columns by default."""

    def test_input_excludes_pk_by_default(self):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        InputType = orm.input(Account)
        field_names = set(InputType.__dataclass_fields__.keys())
        assert "id" not in field_names, (
            f"PK 'id' should be excluded by default; got fields: {field_names}"
        )

    def test_input_can_include_pk_via_opt_out(self):
        """Users should be able to opt-in to PK inclusion via exclude_pk=False."""
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        try:
            InputType = orm.input(Account, exclude_pk=False)
        except TypeError:
            pytest.fail("input() does not accept 'exclude_pk' parameter yet")
        field_names = set(InputType.__dataclass_fields__.keys())
        assert "id" in field_names


# =========================================================================
# FIX 3 — from __future__ import annotations (PEP 563) compatibility
#
# Plan:
#   File  : All backends' type() decorator (the inner decorator function).
#   Change: After reading cls.__annotations__, resolve string annotations
#           using typing.get_type_hints(cls, ...) or by checking both
#           `ann is strawberry.auto` AND `ann == "auto"` with a
#           module-qualified fallback.  The safest approach is
#           get_type_hints() wrapped in try/except for forward refs.
#   Scope : ~10 lines per backend.
# =========================================================================


class TestFix3_FutureAnnotationsCompat:
    """orm.type() must work when annotations are strings (PEP 563)."""

    def test_type_decorator_with_string_annotations(self):
        """Simulate PEP 563 by using string annotations directly."""
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        # Manually create a class with string annotations
        # (equivalent to `from __future__ import annotations`)
        ns = {
            "__annotations__": {
                "id": "auto",
                "username": "auto",
            },
            "__module__": __name__,
        }
        cls = type("StringAnnotUser", (), ns)
        # Make `auto` resolvable in the class's module namespace

        try:
            Decorated = orm.type(Account)(cls)
        except Exception as e:
            pytest.fail(f"type() crashed with string annotations: {e}")

        # Verify the decorated type has resolved annotations
        hints = Decorated.__annotations__
        assert hints.get("id") is not None
        assert hints.get("username") is not None

    def test_schema_creation_with_string_annotations(self):
        """Full round-trip: type with string annotations -> schema -> query."""
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        ns = {
            "__annotations__": {
                "id": "auto",
                "username": "auto",
                "email": "auto",
            },
            "__module__": __name__,
        }
        cls = type("PEP563User", (), ns)

        try:
            UserType = orm.type(Account)(cls)
        except Exception as e:
            pytest.fail(f"type() crashed: {e}")

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UserType]:
                return select(Account)

        try:
            strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        except Exception as e:
            pytest.fail(f"Schema creation crashed with PEP 563 types: {e}")


# =========================================================================
# FIX 4 — LIKE wildcard escaping in ilike (SQL-level verification)
#
# Plan:  (same fix as #1, extra test to verify generated SQL)
#   Verify the generated SQL uses an ESCAPE clause.
# =========================================================================


class TestFix4_LikeEscapeInSQL:
    """Verify the generated SQL contains an ESCAPE clause."""

    def test_ilike_sql_has_escape_clause(self, engine, seeded):
        session = seeded
        queries: list[str] = []

        def _before(conn, cursor, stmt, params, context, executemany):
            queries.append(stmt)

        event.listen(engine, "before_cursor_execute", _before)

        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        filt = orm.filter(Account)

        @orm.type(Account, filters=filt)
        class AT:
            id: auto
            username: auto

        @strawberry.type
        class Q:
            accounts: list[AT] = orm.field.auto()

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        schema.execute_sync(
            '{ accounts(filter: { field: { username: { iContains: "%" } } }) { username } }',
            context_value={"session": session},
        )

        event.remove(engine, "before_cursor_execute", _before)

        like_queries = [q for q in queries if "LIKE" in q.upper()]
        assert like_queries, "Expected at least one LIKE query"
        assert any("ESCAPE" in q.upper() for q in like_queries), (
            f"LIKE query should contain ESCAPE clause; got: {like_queries[-1]}"
        )


# =========================================================================
# FIX 5 — Filter depth / complexity limits
#
# Plan:
#   File  : src/strawberry_orm/backends/sqlalchemy.py (and django.py)
#           _build_sa_filter / _build_django_filter
#   Change: Add a `_depth` counter parameter.  When depth exceeds
#           backend.max_filter_depth (default 10), raise a ValueError
#           that the resolver converts into a GraphQL error.
#           Also accept max_filter_depth in the backend constructor.
#   Scope : ~15 lines per backend.
# =========================================================================


class TestFix5_FilterDepthLimit:
    """Deeply nested filters should be rejected."""

    @pytest.fixture(autouse=True)
    def _setup(self, seeded):
        self.session = seeded
        self.orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", max_filter_depth=5)
        filt = self.orm.filter(Account)

        @self.orm.type(Account, filters=filt)
        class AT:
            id: auto
            username: auto

        @strawberry.type
        class Q:
            accounts: list[AT] = self.orm.field.auto()

        self.schema = strawberry.Schema(
            query=Q, extensions=[self.orm.optimizer_extension()]
        )

    def _exec(self, query):
        return self.schema.execute_sync(query, context_value={"session": self.session})

    def _nested_not(self, depth):
        inner = '{ field: { username: { exact: "alice" } } }'
        for _ in range(depth):
            inner = f"{{ not: {inner} }}"
        return inner

    def test_shallow_nesting_allowed(self):
        """Nesting within the limit (3 < 5) should work."""
        filt = self._nested_not(3)
        result = self._exec(f"{{ accounts(filter: {filt}) {{ username }} }}")
        assert result.errors is None

    def test_deep_nesting_rejected(self):
        """Nesting beyond the limit (8 > 5) should produce an error."""
        filt = self._nested_not(8)
        result = self._exec(f"{{ accounts(filter: {filt}) {{ username }} }}")
        assert result.errors is not None, (
            "Deep nesting should be rejected but was accepted"
        )
        assert any(
            "depth" in str(e).lower() or "limit" in str(e).lower()
            for e in result.errors
        )

    def test_wide_any_rejected(self):
        """Excessively wide 'any' (200 branches) should be rejected."""
        branches = ", ".join(
            f'{{ field: {{ username: {{ exact: "u{i}" }} }} }}' for i in range(200)
        )
        result = self._exec(
            f"{{ accounts(filter: {{ any: [{branches}] }}) {{ username }} }}"
        )
        assert result.errors is not None, (
            "Wide 'any' filter should be rejected but was accepted"
        )


# =========================================================================
# FIX 6 — Regex filter opt-out / validation
#
# Plan:
#   File  : src/strawberry_orm/filters.py  (remove regex from StringLookup)
#           OR  src/strawberry_orm/backends/sqlalchemy.py  (_build_lookup_clauses)
#   Change: Option A (recommended): Add enable_regex=False default on
#           the backend constructor. When False, regex / i_regex lookups
#           are silently ignored or raise ValueError.
#           Option B: Add max_regex_length (default 100) and reject
#           patterns exceeding it.
#   Scope : ~10 lines.
# =========================================================================


class TestFix6_RegexValidation:
    """Regex filters should be disabled by default or length-limited."""

    @pytest.fixture(autouse=True)
    def _setup(self, seeded):
        self.session = seeded
        self.orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", enable_regex_filters=False
        )
        filt = self.orm.filter(Account)

        @self.orm.type(Account, filters=filt)
        class AT:
            id: auto
            username: auto

        @strawberry.type
        class Q:
            accounts: list[AT] = self.orm.field.auto()

        self.schema = strawberry.Schema(
            query=Q, extensions=[self.orm.optimizer_extension()]
        )

    def _exec(self, query):
        return self.schema.execute_sync(query, context_value={"session": self.session})

    def test_regex_filter_rejected_when_disabled(self):
        """When enable_regex_filters=False, regex lookups produce an error."""
        result = self._exec("""
            { accounts(filter: { field: { username: { regex: ".*" } } }) {
                username
            }}
        """)
        assert result.errors is not None, (
            "Regex filter should be rejected when enable_regex_filters=False"
        )

    def test_i_regex_filter_rejected_when_disabled(self):
        result = self._exec("""
            { accounts(filter: { field: { username: { iRegex: ".*" } } }) {
                username
            }}
        """)
        assert result.errors is not None


# =========================================================================
# FIX 7 — permission_classes wired into generated fields
#
# Plan:
#   File  : All backends' type() decorator where FieldDefinition is
#           consumed;  also fields.py / core.py where FieldDefinition
#           is turned into a strawberry.field().
#   Change: If FieldDefinition.permission_classes is set, pass it to
#           strawberry.field(permission_classes=...).
#   Scope : ~5 lines per backend.
# =========================================================================


class TestFix7_PermissionClassesApplied:
    """permission_classes on FieldDefinition should be forwarded to the
    generated strawberry field so that Strawberry enforces them."""

    def test_permission_classes_on_field_definition(self):
        from strawberry.permission import BasePermission

        from strawberry_orm.types import FieldDefinition

        class DenyAll(BasePermission):
            message = "denied"

            def has_permission(self, source, info, **kwargs):
                return False

        fd = FieldDefinition(
            permission_classes=[DenyAll],
            description="secret",
        )

        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(Account)
        class AT:
            id: auto
            username: auto
            email: auto = fd  # field with permission

        @strawberry.type
        class Q:
            @strawberry.field
            def account(self, info: strawberry.types.Info) -> AT | None:
                return None

        schema = strawberry.Schema(query=Q)

        # Introspect: the 'email' field should exist but be permission-guarded
        gql_type = None
        for td in schema._schema.type_map.values():
            if getattr(td, "name", None) == "AT":
                gql_type = td
                break

        assert gql_type is not None, "AT type not found in schema"
        # Verify via __strawberry_definition__ that the email field
        # carries permission_classes from our FieldDefinition.
        sdef = getattr(AT, "__strawberry_definition__", None)
        assert sdef is not None, "AT has no __strawberry_definition__"
        email_field = next((f for f in sdef.fields if f.name == "email"), None)
        assert email_field is not None, "email field not found on AT"
        assert email_field.permission_classes, (
            "email field should have permission_classes set"
        )


# =========================================================================
# FIX 8 — apply_ref_list authorization hook
#
# Plan:
#   File  : src/strawberry_orm/backends/protocol.py  (protocol change)
#           src/strawberry_orm/backends/sqlalchemy.py
#           src/strawberry_orm/backends/django.py
#           src/strawberry_orm/backends/tortoise.py
#           src/strawberry_orm/core.py  (pass-through)
#   Change: Add an optional `authorize` callback parameter:
#               def authorize(action, model, obj_id, info) -> bool
#           Called before every session.get / create / update / delete
#           inside apply_ref_list.  If it returns False, the ref is
#           skipped and an error is collected.
#           The callback can also be set at the backend constructor
#           level as a default: ref_authorizer=...
#   Scope : ~20 lines per backend.
# =========================================================================


class TestFix8_RefListAuthorizationHook:
    """apply_ref_list should call an authorization hook when provided."""

    @pytest.fixture(autouse=True)
    def _setup(self, seeded):
        self.session = seeded
        self.orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

    def test_authorize_callback_invoked(self):
        """The authorize callback should be called for each ref."""
        log = []

        def authorizer(action, model, obj_id, info):
            log.append((action, model.__name__, obj_id))
            return True

        role_ref_type = self.orm.ref(Role)
        update_type = role_ref_type.__dataclass_fields__["update"].type
        actual_type = (
            update_type.__args__[0] if hasattr(update_type, "__args__") else update_type
        )
        ref = role_ref_type(update=actual_type(id="1"))

        account = self.session.get(Account, 1)

        class FakeInfo:
            context = {"session": self.session}

        try:
            self.orm.apply_ref_list(
                account,
                "groups",
                [ref],
                FakeInfo(),
                authorize=authorizer,
            )
        except TypeError as e:
            if "authorize" in str(e):
                pytest.fail(
                    "apply_ref_list does not accept an 'authorize' parameter yet"
                )
            raise

        assert len(log) > 0, "Authorizer was not called"
        assert log[0][0] in ("link", "read", "get", "update"), (
            f"Expected action like 'link' or 'update'; got {log[0][0]}"
        )

    def test_authorize_rejection_prevents_link(self):
        """When authorizer returns False, the object should NOT be linked."""

        def deny_all(action, model, obj_id, info):
            return False

        role_ref_type = self.orm.ref(Role)
        update_type = role_ref_type.__dataclass_fields__["update"].type
        actual_type = (
            update_type.__args__[0] if hasattr(update_type, "__args__") else update_type
        )
        ref = role_ref_type(update=actual_type(id="1"))

        account = self.session.get(Account, 1)

        class FakeInfo:
            context = {"session": self.session}

        try:
            self.orm.apply_ref_list(
                account,
                "groups",
                [ref],
                FakeInfo(),
                authorize=deny_all,
            )
        except TypeError:
            pytest.fail("apply_ref_list does not accept an 'authorize' parameter yet")

        # The admin role (id=1) should NOT have been linked
        self.session.refresh(account)
        related = getattr(account, "groups", [])
        assert len(list(related)) == 0, (
            "Authorizer denied the link, but object was linked anyway"
        )


# =========================================================================
# FIX 8B — ref(delete=True) should unlink by default
# =========================================================================


class TestFix8B_RefExplicitUnlinkAndDelete:
    def _unlink_ref(self, orm, ref_id):
        ref_type = orm.ref(Role, unlink=True)
        unlink_type = ref_type.__dataclass_fields__["unlink"].type
        actual_type = (
            unlink_type.__args__[0] if hasattr(unlink_type, "__args__") else unlink_type
        )
        return ref_type(unlink=actual_type(id=str(ref_id)))

    def _delete_ref(self, orm, ref_id):
        ref_type = orm.ref(Role, delete=True)
        delete_type = ref_type.__dataclass_fields__["delete"].type
        actual_type = (
            delete_type.__args__[0] if hasattr(delete_type, "__args__") else delete_type
        )
        return ref_type(delete=actual_type(id=str(ref_id)))

    def test_unlink_removes_from_relation_without_hard_delete(self, seeded):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        account = seeded.get(Account, 1)
        role = seeded.get(Role, 1)
        account.groups.append(role)
        seeded.flush()

        class FakeInfo:
            context = {"session": seeded}

        orm.apply_ref_list(
            account,
            "groups",
            [self._unlink_ref(orm, 1)],
            FakeInfo(),
        )
        seeded.flush()

        assert seeded.get(Role, 1) is not None
        assert list(account.groups) == []

    def test_delete_hard_deletes_the_row(self, seeded):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        account = seeded.get(Account, 1)
        role = seeded.get(Role, 1)
        account.groups.append(role)
        seeded.flush()

        class FakeInfo:
            context = {"session": seeded}

        orm.apply_ref_list(
            account,
            "groups",
            [self._delete_ref(orm, 1)],
            FakeInfo(),
        )
        seeded.flush()

        assert seeded.get(Role, 1) is None
        assert list(account.groups) == []


# =========================================================================
# FIX 9 — Error sanitization (no table/column leakage)
#
# Plan:
#   File  : src/strawberry_orm/backends/sqlalchemy.py  (_build_lookup_clauses)
#   Change: Wrap regexp_match and other DB calls in try/except.
#           On OperationalError / ProgrammingError, raise a sanitized
#           ValueError("Invalid filter value") that does not leak
#           internal names.
#   Scope : ~10 lines.
# =========================================================================


class TestFix9_ErrorSanitization:
    """Database errors should not leak table/column names."""

    @pytest.fixture(autouse=True)
    def _setup(self, seeded):
        self.session = seeded
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        filt = orm.filter(Account)

        @orm.type(Account, filters=filt)
        class AT:
            id: auto
            username: auto

        @strawberry.type
        class Q:
            accounts: list[AT] = orm.field.auto()

        self.schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])

    def _exec(self, query):
        return self.schema.execute_sync(query, context_value={"session": self.session})

    def test_invalid_regex_does_not_leak_table_name(self):
        """An invalid regex should produce a user-friendly error without
        exposing internal table names like 'fix_account'."""
        result = self._exec("""
            { accounts(filter: { field: { username: { regex: "[invalid" } } }) {
                username
            }}
        """)
        if result.errors:
            error_text = " ".join(str(e) for e in result.errors)
            assert "fix_account" not in error_text, (
                f"Error leaked table name: {error_text}"
            )
            assert (
                "username" not in error_text.lower() or "filter" in error_text.lower()
            ), f"Error may have leaked column name: {error_text}"


# =========================================================================
# FIX 10 — filter() / input() should accept exclude_from_filters
#
# Plan:
#   This is about making existing exclude= work properly and
#   providing convenience for security-sensitive patterns.
#   The filter() and input() methods already accept exclude=,
#   but there's no way to propagate type-level exclusions
#   to their associated filters/inputs automatically.
#
#   Recommended approach:
#     - Add a class-level `__orm_exclude__` set on the type decorator
#       when exclude= is used.
#     - filter() and input() check for this if exclude= is not
#       explicitly provided.
#   Scope : ~10 lines per backend.
# =========================================================================


class TestFix10_FilterExclude:
    """filter(exclude=...) should actually remove fields from the filter
    input so they cannot be used for data exfiltration."""

    def test_sensitive_fields_excluded_by_default(self):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        filt = orm.filter(Account)
        field_names = set(filt._field_type.__dataclass_fields__.keys())
        assert "password_hash" not in field_names
        assert "api_key" not in field_names
        assert "is_admin" not in field_names

        order = orm.order(Account)
        order_names = set(order.__dataclass_fields__.keys())
        assert "password_hash" not in order_names
        assert "api_key" not in order_names
        assert "is_admin" not in order_names

        inp = orm.input(Account)
        input_names = set(inp.__dataclass_fields__.keys())
        assert "password_hash" not in input_names
        assert "api_key" not in input_names
        assert "is_admin" not in input_names

    def test_sensitive_fields_can_be_explicitly_included(self):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        filt = orm.filter(Account, include=["password_hash"])
        field_names = set(filt._field_type.__dataclass_fields__.keys())
        assert "password_hash" in field_names

        inp = orm.input(Account, include=["password_hash"])
        input_names = set(inp.__dataclass_fields__.keys())
        assert "password_hash" in input_names

    def test_excluded_field_not_in_filter(self):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        filt = orm.filter(Account, exclude=["password_hash", "api_key"])
        field_type = filt._field_type

        field_names = set(field_type.__dataclass_fields__.keys())
        assert "password_hash" not in field_names, (
            "password_hash should be excluded from filter"
        )
        assert "api_key" not in field_names, "api_key should be excluded from filter"

    def test_excluded_field_not_filterable_at_runtime(self, seeded):
        """Trying to filter on an excluded field should fail or be ignored."""
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        filt = orm.filter(Account, exclude=["password_hash"])

        @orm.type(Account, filters=filt)
        class AT:
            id: auto
            username: auto
            email: auto

        @strawberry.type
        class Q:
            accounts: list[AT] = orm.field.auto()

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])

        # password_hash should not be a valid filter field
        result = schema.execute_sync(
            '{ accounts(filter: { field: { passwordHash: { exact: "x" } } }) { username } }',
            context_value={"session": seeded},
        )
        # Should error because the field doesn't exist in the filter
        assert result.errors is not None

    def test_input_exclude_removes_fields(self):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        inp = orm.input(Account, exclude=["password_hash", "api_key", "is_admin"])
        field_names = set(inp.__dataclass_fields__.keys())
        assert "password_hash" not in field_names
        assert "api_key" not in field_names
        assert "is_admin" not in field_names
        assert "username" in field_names
        assert "email" in field_names
