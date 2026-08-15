# Contributing to strawberry-orm

Thanks for your interest in improving `strawberry-orm`. The package is in **alpha**, so contributions that tighten correctness — especially around row scoping, the optimizer, and backend parity — are particularly welcome.

## Contents

- [Ground rules](#ground-rules)
- [Development setup](#development-setup)
- [Running tests](#running-tests)
- [Linting and formatting](#linting-and-formatting)
- [Project layout](#project-layout)
- [Backend parity](#backend-parity)
- [Writing tests](#writing-tests)
- [Inspecting schemas locally](#inspecting-schemas-locally)
- [Coding conventions](#coding-conventions)
- [Documentation](#documentation)
- [Commits and pull requests](#commits-and-pull-requests)
- [Releasing](#releasing)
- [Reporting bugs and security issues](#reporting-bugs-and-security-issues)

## Ground rules

- **Open an issue first** for anything larger than a bug fix. Public API changes, new backend support, and optimizer behavior changes benefit from a design discussion before the code exists.
- **Tests are mandatory.** The suite enforces 100% line coverage of `strawberry_orm`; CI fails below that.
- **Behavior changes must land on every backend**, or be explicitly excluded in `tests/test_backend_parity.py` with a reason.
- Be respectful in issues and reviews. Assume good intent.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/) for dependency management and builds. Python `3.12` or newer is required.

```bash
git clone https://github.com/strawberry-graphql/strawberry-orm
cd strawberry-orm
uv sync --group dev --extra django --extra sqlalchemy --extra tortoise
```

Installing all three extras matches CI. Skipping one will make that backend's tests fail to import, and coverage will drop below the required threshold.

## Running tests

```bash
uv run pytest                                   # full suite with coverage gate
uv run pytest tests/backends/sqlalchemy         # one backend
uv run pytest tests/backends/django/test_query_optimizer.py::TestQueryOptimizerEagerLoading
uv run pytest -k filter_object_traversal        # by name
```

Coverage options live in `pyproject.toml` (`--cov=strawberry_orm --cov-fail-under=100`), so every invocation reports coverage. When iterating on a subset, pass `--no-cov` to skip the gate that a partial run can never satisfy:

```bash
uv run pytest tests/backends/tortoise -q --no-cov
```

Always run the full suite without `--no-cov` before pushing.

Notes:

- `asyncio_mode = "auto"`, so `async def` tests need no marker.
- Django settings are configured in `tests/backends/django/conftest.py` against in-memory SQLite; there is no project to migrate.
- SQLAlchemy and Tortoise tests also use in-memory SQLite. No external services are needed.

If a line genuinely cannot be covered, prefer restructuring the code. Only as a last resort add `# pragma: no cover` with a comment explaining why the branch is unreachable.

## Linting and formatting

Ruff config lives in `pyproject.toml` (line length 88, target `py312`, rules `E`/`F`/`I`/`UP`/`B`/`SIM`). Ruff is not pinned as a dependency, so run it via `uvx`:

```bash
uvx ruff check .
uvx ruff format .
```

Run both before opening a pull request. `E501` is deliberately ignored — let the formatter handle line length.

Because Ruff is unpinned, a newer release may surface findings unrelated to your change (new rules land regularly). Fix what your diff introduces; leave pre-existing findings to a dedicated cleanup pull request.

## Project layout

```
src/strawberry_orm/
  core.py            StrawberryORM facade: type/input/filter/order/group factories, schema()
  fields.py          orm.field(), make_field(), field hints
  filters.py         lookup input types and filter tree construction
  types.py           auto, Ordering, group-by and aggregate types
  mutations.py       refs, apply_ref_list, recursive node mutations
  policy.py          mutation projection / _meta policy resolution
  repo.py            AbstractRepo authorization and lifecycle hooks
  lazy_resolution.py lazy-load detection and warning/error modes
  optimizer/         selection-set walking, prefetch store, Strawberry extension
  backends/
    _base.py         shared backend behavior
    protocol.py      backend interface (excluded from coverage)
    django.py  sqlalchemy.py  tortoise.py
  relay/             ORMListConnection and orm.connection()
tests/
  abstract/          backend-agnostic test bodies, shared by all backends
  backends/{django,sqlalchemy,tortoise}/
                     fixtures, models, and thin subclasses of the abstract tests
  test_backend_parity.py
cli.py               GraphiQL inspector for the test schemas
```

Backend-specific behavior belongs in `backends/*.py`. If you find yourself writing the same logic in two backends, move it to `backends/_base.py`.

## Backend parity

`tests/test_backend_parity.py` uses AST introspection to assert that Django, SQLAlchemy, and Tortoise expose the same test files, classes, and methods. Adding a test to one backend without the others fails this check.

When a test truly cannot apply to a backend (for example SQLAlchemy session resolution), add the file to `EXCLUDED_FILES` or the specific test to `EXCLUDED_METHODS` in that module. Keep exclusions narrow — prefer a per-method exclusion over an entire file.

## Writing tests

Most behavior is tested once, in `tests/abstract/`, and inherited per backend. The abstract class receives `execute` and `seed` fixtures supplied by each backend's `fixtures.py`:

```python
# tests/abstract/query_basic.py
class AbstractTestQueryBasic:
    def test_list_all_users(self, execute, seed):
        data = execute("{ users { id name email } }")
        assert data == {"users": [...]}
```

```python
# tests/backends/django/test_query_basic.py
from tests.abstract.query_basic import AbstractTestQueryBasic


class TestQueryBasic(AbstractTestQueryBasic):
    pass
```

Guidelines:

- Write GraphQL-level assertions on the full response dict rather than checking one key. It catches accidental extra fields.
- Assert on **SQL query counts** for anything touching the optimizer — see the `test_query_n_plus_one.py` files for the pattern.
- Scoping behavior needs a test proving rows are hidden, not just that the hook ran.
- Naming follows `test_<area>_<topic>.py` with a matching `tests/abstract/<area>_<topic>.py` module. Keep the names identical across backends so parity passes.

## Inspecting schemas locally

`cli.py` serves the test schemas in GraphiQL, which is the fastest way to see how a change affects the generated SDL:

```bash
uv run python cli.py                          # list available schemas
uv run python cli.py main --backend sqlalchemy
uv run python cli.py get_queryset -b django -p 8500
```

Schemas are seeded with demo data by default; pass `--no-seed` for an empty database. Django and SQLAlchemy are the supported backends here; `SCHEMA_NAMES` in `cli.py` is the list of exposed schemas.

## Coding conventions

- Target Python `3.12+`. Use modern syntax (`X | None`, builtin generics, `match` where it reads well).
- `from __future__ import annotations` at the top of new modules.
- Type-annotate public functions. Keep runtime-introspected generics intact — `UP046` is ignored because `Generic[M]` is needed for `get_original_bases`.
- Public API additions must be exported from `strawberry_orm/__init__.py` and documented in the README's [Public Exports](README.md#public-exports) section.
- Keep backend-facing changes behind the interface in `backends/protocol.py`.
- Comments should explain non-obvious constraints or trade-offs. Don't narrate what the code does.
- Prefer safe defaults. New options that widen what clients can read or write should default to off (as `enable_regex_filters` does).

## Documentation

The README is the single source of user documentation. If your change affects behavior a user can observe, update the relevant section in the same pull request — including the option tables under [Backends](README.md#backends) and, for anything scoping-related, the [Security](README.md#security) chapter.

## Commits and pull requests

- Write commit subjects in the imperative mood, describing the change in one sentence: `Add relation FK presence filters and non-id primary key support`. Commits that also bump the version carry the version in parentheses, matching existing history: `Add totalCount to Relay connections and release v0.13.0`.
- Keep pull requests focused. Unrelated refactors make review harder and bisecting worse.
- Before pushing, confirm: `uvx ruff check .`, `uvx ruff format .`, and `uv run pytest` (full suite, coverage at 100%).
- In the pull request description, state what changed, why, and how you verified it. Call out any backend that behaves differently and why.
- CI runs the full suite on Python 3.12 with all three backends and uploads coverage to Codecov.

## Releasing

Releases are cut by maintainers:

1. Bump `project.version` in `pyproject.toml` on `main`.
2. Run the **Release and Publish** workflow (`workflow_dispatch`). It must be dispatched from `main`.

The workflow reads the version from `pyproject.toml`, refuses to run if the `v<version>` tag already exists, builds with `uv build`, validates the artifacts with `twine check --strict`, creates a GitHub release with generated notes, and publishes to PyPI via trusted publishing.

## Reporting bugs and security issues

For bugs, open an issue on the [issue tracker](https://github.com/strawberry-graphql/strawberry-orm/issues) with the backend, the relevant type and schema definitions, the GraphQL query, and what you expected versus what happened. A minimal reproduction using the test fixtures is ideal.

For anything that could expose data — a query that returns rows `get_queryset` should have hidden, a filter that escapes its scope, a sensitive field leaking into a generated input — **do not open a public issue.** Email the maintainers (see `authors` in `pyproject.toml`) so a fix can ship before disclosure.

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
