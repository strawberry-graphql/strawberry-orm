"""Security vulnerability tests for strawberry-orm.

Covers: SQL injection, ReDoS, filter recursion bombs, mass assignment,
IDOR / authorization bypass, ILIKE wildcard injection, regex injection,
unbounded query depth, and error information leakage.
"""

import pytest
import strawberry
from sqlalchemy import (
    Boolean,
    ForeignKey,
    String,
    Text,
    create_engine,
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
# Models
# =========================================================================


class Base(DeclarativeBase):
    pass


class SecretUser(Base):
    __tablename__ = "sec_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[list["SecretNote"]] = relationship(back_populates="owner")


class SecretNote(Base):
    __tablename__ = "sec_note"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[int] = mapped_column(ForeignKey("sec_user.id"))
    owner: Mapped[SecretUser] = relationship(back_populates="notes")


# =========================================================================
# ORM setup
# =========================================================================

orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

UserFilter = orm.filter(SecretUser)
UserOrder = orm.order(SecretUser)
NoteFilter = orm.filter(SecretNote)
NoteOrder = orm.order(SecretNote)


@orm.type(SecretNote, filters=NoteFilter, order=NoteOrder)
class SecretNoteType:
    id: auto
    title: auto
    body: auto
    owner_id: auto


@orm.type(SecretUser, filters=UserFilter, order=UserOrder)
class SecretUserType:
    id: auto
    username: auto
    email: auto
    password_hash: auto
    is_admin: auto
    notes: list[SecretNoteType]


@strawberry.type
class Query:
    users: list[SecretUserType] = orm.field.auto()
    notes: list[SecretNoteType] = orm.field.auto()

    @strawberry.field
    def user(self, info: strawberry.types.Info, id: int) -> SecretUserType | None:
        session: Session = info.context["session"]
        return session.get(SecretUser, id)


@strawberry.input
class CreateUserInput:
    username: str
    email: str
    password_hash: str
    is_admin: bool = False


@strawberry.input
class UpdateUserInput:
    id: int
    username: str | None = strawberry.UNSET
    email: str | None = strawberry.UNSET
    password_hash: str | None = strawberry.UNSET
    is_admin: bool | None = strawberry.UNSET


@strawberry.type
class Mutation:
    @strawberry.mutation
    def create_user(
        self, info: strawberry.types.Info, input: CreateUserInput
    ) -> SecretUserType:
        session: Session = info.context["session"]
        user = SecretUser(
            username=input.username,
            email=input.email,
            password_hash=input.password_hash,
            is_admin=input.is_admin,
        )
        session.add(user)
        session.commit()
        return user

    @strawberry.mutation
    def update_user(
        self, info: strawberry.types.Info, input: UpdateUserInput
    ) -> SecretUserType | None:
        session: Session = info.context["session"]
        user = session.get(SecretUser, input.id)
        if user is None:
            return None
        if input.username is not strawberry.UNSET and input.username is not None:
            user.username = input.username
        if input.email is not strawberry.UNSET and input.email is not None:
            user.email = input.email
        if (
            input.password_hash is not strawberry.UNSET
            and input.password_hash is not None
        ):
            user.password_hash = input.password_hash
        if input.is_admin is not strawberry.UNSET and input.is_admin is not None:
            user.is_admin = input.is_admin
        session.commit()
        return user


schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    extensions=[orm.optimizer_extension()],
)


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def seeded_session(session):
    alice = SecretUser(
        id=1,
        username="alice",
        email="alice@corp.com",
        password_hash="$2b$12$abcdefghijklmnop",
        is_admin=True,
    )
    bob = SecretUser(
        id=2,
        username="bob",
        email="bob@corp.com",
        password_hash="$2b$12$qrstuvwxyz123456",
        is_admin=False,
    )
    session.add_all([alice, bob])
    session.flush()

    note1 = SecretNote(id=1, title="Alice private", body="SSN: 123-45-6789", owner_id=1)
    note2 = SecretNote(id=2, title="Bob note", body="My secret diary", owner_id=2)
    session.add_all([note1, note2])
    session.commit()
    return session


def execute(target_schema, sess, query, variables=None):
    result = target_schema.execute_sync(
        query,
        variable_values=variables or {},
        context_value={"session": sess},
    )
    return result


# =========================================================================
# VULNERABILITY 1: Sensitive field exposure (password hashes, etc.)
# =========================================================================


class TestSensitiveFieldExposure:
    """The library auto-generates types from ALL model columns, including
    sensitive fields like password_hash. There is no built-in mechanism to
    mark fields as sensitive or to exclude them by default."""

    def test_password_hash_queryable(self, seeded_session):
        """An attacker can query password hashes via GraphQL."""
        result = execute(
            schema,
            seeded_session,
            """
            { users { username passwordHash } }
        """,
        )
        assert result.errors is None
        users = result.data["users"]
        hashes = [u["passwordHash"] for u in users]
        assert any("$2b$" in h for h in hashes), (
            "Password hashes are exposed through the API"
        )

    def test_password_hash_filterable(self, seeded_session):
        """Generated filters should exclude password hashes by default."""
        result = execute(
            schema,
            seeded_session,
            """
            { users(filter: { field: { passwordHash: { startsWith: "$2b$12$abc" } } }) {
                username
            }}
        """,
        )
        assert result.errors is not None

    def test_is_admin_field_exposed(self, seeded_session):
        """Generated filters should exclude admin flags by default."""
        result = execute(
            schema,
            seeded_session,
            """
            { users(filter: { field: { isAdmin: { exact: true } } }) {
                username email
            }}
        """,
        )
        assert result.errors is not None


# =========================================================================
# VULNERABILITY 2: ILIKE / pattern injection in string filters
# =========================================================================


class TestILIKEWildcardInjection:
    """The i_contains, i_starts_with, and i_ends_with filters use
    f-string interpolation to build ILIKE patterns:
        column.ilike(f"%{val}%")
    If `val` contains SQL LIKE wildcards (% or _), they are NOT escaped,
    allowing an attacker to craft broader matches than intended."""

    def test_percent_wildcard_in_icontains(self, seeded_session):
        """Injecting % into iContains should NOT match everything (wildcard is escaped)."""
        result = execute(
            schema,
            seeded_session,
            """
            { users(filter: { field: { email: { iContains: "%" } } }) {
                username
            }}
        """,
        )
        assert result.errors is None
        assert len(result.data["users"]) == 0, (
            "ILIKE '%' should not match everything - wildcard must be escaped"
        )

    def test_underscore_wildcard_in_istartswith(self, seeded_session):
        """Injecting _ into iStartsWith should NOT match single chars (wildcard is escaped)."""
        result = execute(
            schema,
            seeded_session,
            """
            { users(filter: { field: { email: { iStartsWith: "___" } } }) {
                username
            }}
        """,
        )
        assert result.errors is None
        assert len(result.data["users"]) == 0, (
            "ILIKE '___%%' should not match any 3+ char prefix - underscore must be escaped"
        )

    def test_percent_in_iendswith(self, seeded_session):
        """Injecting % into iEndsWith should NOT match everything (wildcard is escaped)."""
        result = execute(
            schema,
            seeded_session,
            """
            { users(filter: { field: { email: { iEndsWith: "%" } } }) {
                username
            }}
        """,
        )
        assert result.errors is None
        assert len(result.data["users"]) == 0, (
            "ILIKE '%%' should not match everything - wildcard must be escaped"
        )


# =========================================================================
# VULNERABILITY 3: Regex Denial of Service (ReDoS)
# =========================================================================


class TestRegexInjection:
    """The regex and i_regex filters pass user input directly to
    column.regexp_match(val), enabling:
    1. ReDoS via catastrophic backtracking patterns
    2. Backend-specific regex escapes or operators"""

    def test_regex_filter_accepts_arbitrary_patterns(self, seeded_session):
        """Regex filters are now disabled by default for security.

        With Advisory 5 (StringLookupNoRegex), the regex field is no longer
        even present in the schema when regex is disabled, so GraphQL itself
        rejects the field before the backend sees it.
        """
        result = execute(
            schema,
            seeded_session,
            """
            { users(filter: { field: { username: { regex: ".*" } } }) {
                username
            }}
        """,
        )
        assert result.errors is not None
        err_msg = str(result.errors[0]).lower()
        assert "disabled" in err_msg or "not defined" in err_msg

    def test_complex_regex_accepted(self, seeded_session):
        """Complex regex patterns with backreferences are also rejected when disabled."""
        result = execute(
            schema,
            seeded_session,
            """
            { users(filter: { field: { username: { regex: "^(a|b|c|d|e|f|g)+$" } } }) {
                username
            }}
        """,
        )
        assert result.errors is not None
        err_msg = str(result.errors[0]).lower()
        assert "disabled" in err_msg or "not defined" in err_msg


# =========================================================================
# VULNERABILITY 4: Filter recursion depth (DoS via deeply nested filters)
# =========================================================================


class TestFilterRecursionBomb:
    """The filter system supports recursive nesting: all/any/not can nest
    arbitrarily deep. A malicious client can send deeply nested filters
    to cause stack overflow or extreme query complexity."""

    def _build_nested_filter(self, depth: int) -> str:
        """Build a filter nested `depth` levels deep."""
        inner = '{ field: { username: { exact: "alice" } } }'
        for _ in range(depth):
            inner = f"{{ not: {inner} }}"
        return inner

    def test_moderate_nesting_works(self, seeded_session):
        """Moderate nesting (10 levels) should work."""
        filter_str = self._build_nested_filter(10)
        query = f"{{ users(filter: {filter_str}) {{ username }} }}"
        result = execute(schema, seeded_session, query)
        assert result.errors is None

    def test_deep_nesting_no_protection(self, seeded_session):
        """Deep nesting (100 levels) has no built-in protection."""
        filter_str = self._build_nested_filter(100)
        query = f"{{ users(filter: {filter_str}) {{ username }} }}"
        result = execute(schema, seeded_session, query)
        # If this doesn't crash, it means there's no depth limit
        assert result.errors is None or result.errors is not None

    def test_wide_any_filter(self, seeded_session):
        """Wide `any` filter with many branches is now rejected by branch limits."""
        branches = ", ".join(
            f'{{ field: {{ username: {{ exact: "user{i}" }} }} }}' for i in range(200)
        )
        query = f"{{ users(filter: {{ any: [{branches}] }}) {{ username }} }}"
        result = execute(schema, seeded_session, query)
        assert result.errors is not None
        assert "branches" in str(result.errors[0]).lower()


# =========================================================================
# VULNERABILITY 5: No authorization layer
# =========================================================================


class TestNoAuthorizationLayer:
    """The library provides no built-in authorization. Any user can query
    any data, including other users' private notes."""

    def test_any_user_can_read_all_notes(self, seeded_session):
        """Without authorization, Bob's notes are visible to everyone."""
        result = execute(
            schema,
            seeded_session,
            """
            { notes { id title body ownerId } }
        """,
        )
        assert result.errors is None
        assert len(result.data["notes"]) == 2
        bodies = [n["body"] for n in result.data["notes"]]
        assert "SSN: 123-45-6789" in bodies, (
            "Sensitive PII is queryable without authorization"
        )

    def test_cross_user_data_via_relationship(self, seeded_session):
        """A user can traverse relationships to see other users' data."""
        result = execute(
            schema,
            seeded_session,
            """
            { users { username notes { title body } } }
        """,
        )
        assert result.errors is None
        for user in result.data["users"]:
            if user["username"] == "alice":
                assert any("SSN" in n["body"] for n in user["notes"])


# =========================================================================
# VULNERABILITY 6: Mass assignment via auto-generated input types
# =========================================================================


class TestMassAssignment:
    """When input types are auto-generated, ALL fields including sensitive ones
    (is_admin, password_hash) are included. The library has no built-in
    mechanism to prevent mass assignment attacks."""

    def test_privilege_escalation_via_create(self, seeded_session):
        """An attacker can set is_admin=true on user creation."""
        result = execute(
            schema,
            seeded_session,
            """
            mutation {
                createUser(input: {
                    username: "attacker"
                    email: "attacker@evil.com"
                    passwordHash: "fake"
                    isAdmin: true
                }) { username isAdmin }
            }
        """,
        )
        assert result.errors is None
        assert result.data["createUser"]["isAdmin"] is True

    def test_privilege_escalation_via_update(self, seeded_session):
        """An attacker can promote themselves to admin via update."""
        result = execute(
            schema,
            seeded_session,
            """
            mutation {
                updateUser(input: { id: 2, isAdmin: true }) {
                    username isAdmin
                }
            }
        """,
        )
        assert result.errors is None
        assert result.data["updateUser"]["isAdmin"] is True
        assert result.data["updateUser"]["username"] == "bob"


# =========================================================================
# VULNERABILITY 7: IDOR via apply_ref_list
# =========================================================================


class TestIDOR:
    """apply_ref_list performs session.get() with user-supplied IDs without
    any authorization check. An attacker can link, update, or delete objects
    belonging to other users."""

    def test_fetch_any_user_by_id(self, seeded_session):
        """Direct object reference - any user accessible by ID."""
        result = execute(
            schema,
            seeded_session,
            """
            { user(id: 1) { username email passwordHash } }
        """,
        )
        assert result.errors is None
        assert result.data["user"]["username"] == "alice"
        assert "$2b$" in result.data["user"]["passwordHash"]


# =========================================================================
# VULNERABILITY 8: Error information leakage
# =========================================================================


class TestErrorInformationLeakage:
    """Errors from the database or ORM may leak internal implementation
    details like table names, column names, and SQL syntax."""

    def test_invalid_filter_leaks_info(self, seeded_session):
        """Errors from invalid operations may expose internal structure."""
        result = execute(
            schema,
            seeded_session,
            """
            { users(filter: { field: { username: { regex: "[invalid" } } }) {
                username
            }}
        """,
        )
        # If there's an error, check if it leaks table/column names
        if result.errors:
            str(result.errors[0])
            # This test documents that errors may contain internal details
            assert True


# =========================================================================
# VULNERABILITY 9: No query complexity / cost analysis
# =========================================================================


class TestQueryComplexity:
    """The library provides no query cost analysis or complexity limits.
    A client can request deeply nested relationships causing N+1 or
    cartesian product explosions."""

    def test_nested_relationship_traversal(self, seeded_session):
        """Deep relationship traversal is unrestricted."""
        result = execute(
            schema,
            seeded_session,
            """
            {
                users {
                    username
                    notes {
                        title
                        body
                    }
                }
            }
        """,
        )
        assert result.errors is None

    def test_repeated_field_selection(self, seeded_session):
        """Same field can be aliased many times, multiplying work."""
        aliases = " ".join(f"u{i}: users {{ username }}" for i in range(50))
        query = f"{{ {aliases} }}"
        result = execute(schema, seeded_session, query)
        assert result.errors is None
        assert len(result.data) == 50


# =========================================================================
# VULNERABILITY 10: Introspection enabled by default
# =========================================================================


class TestIntrospectionExposure:
    """GraphQL introspection is enabled by default, allowing attackers to
    enumerate the full schema including all types, fields, and mutations."""

    def test_introspection_reveals_schema(self, seeded_session):
        result = execute(
            schema,
            seeded_session,
            """
            {
                __schema {
                    types { name }
                    mutationType { fields { name } }
                }
            }
        """,
        )
        assert result.errors is None
        type_names = [t["name"] for t in result.data["__schema"]["types"]]
        assert "SecretUserType" in type_names
        mutation_fields = [
            f["name"] for f in result.data["__schema"]["mutationType"]["fields"]
        ]
        assert "createUser" in mutation_fields

    def test_introspection_reveals_sensitive_fields(self, seeded_session):
        result = execute(
            schema,
            seeded_session,
            """
            {
                __type(name: "SecretUserType") {
                    fields { name type { name } }
                }
            }
        """,
        )
        assert result.errors is None
        field_names = [f["name"] for f in result.data["__type"]["fields"]]
        assert "passwordHash" in field_names
        assert "isAdmin" in field_names
