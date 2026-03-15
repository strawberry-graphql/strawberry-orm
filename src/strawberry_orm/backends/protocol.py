"""Backend protocol that all ORM adapters must implement."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from strawberry.extensions import SchemaExtension

from strawberry_orm._async import AwaitableOrValue


@runtime_checkable
class Backend(Protocol):
    """Abstract interface implemented by each ORM backend.

    Every method receives ``**kwargs`` so individual backends can accept
    backend-specific options without breaking the common interface.
    """

    # -- Type generation -----------------------------------------------------

    def type(self, model: type, **kwargs: Any) -> Any:
        """Generate a Strawberry object type from an ORM model."""
        ...

    def input(self, model: type, **kwargs: Any) -> Any:
        """Generate a Strawberry input type from an ORM model."""
        ...

    def partial(self, model: type, **kwargs: Any) -> Any:
        """Generate a partial (all-optional) Strawberry input from a model."""
        ...

    def filter(self, model_or_type: type, **kwargs: Any) -> Any:
        """Generate a @oneOf filter input for a model or Strawberry type."""
        ...

    def order(self, model_or_type: type, **kwargs: Any) -> Any:
        """Generate an ordering input for a model or Strawberry type."""
        ...

    # -- Fields --------------------------------------------------------------

    def field(self, **kwargs: Any) -> Any:
        """Create a Strawberry field descriptor with optimizer hints."""
        ...

    def node(self, **kwargs: Any) -> Any:
        """Create a Relay node field descriptor."""
        ...

    def connection(self, **kwargs: Any) -> Any:
        """Create a Relay connection field descriptor."""
        ...

    # -- Mutations -----------------------------------------------------------

    def create(self, input_type: type, **kwargs: Any) -> Any:
        """Create a mutation field that inserts a new object."""
        ...

    def update(self, input_type: type, **kwargs: Any) -> Any:
        """Create a mutation field that updates an existing object."""
        ...

    def delete(self, **kwargs: Any) -> Any:
        """Create a mutation field that deletes objects."""
        ...

    # -- Related list refs ---------------------------------------------------

    def ref(
        self,
        model: type,
        *,
        create: type | None = None,
        update: type | None = None,
        delete: bool = False,
    ) -> type:
        """Generate a ``@oneOf`` ref input for managing a related list."""
        ...

    def apply_ref_list(
        self,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        *,
        authorize: Any | None = None,
        mode: str = "replace",
    ) -> AwaitableOrValue[None]:
        """Apply a list of ref operations to *instance*'s *field* relation.

        *mode* controls how the relationship list is updated:
        - ``"replace"`` (default): the entire relationship list is replaced
        - ``"patch"``: only the mentioned items are added/removed/updated
        """
        ...

    # -- Query application ----------------------------------------------------

    def apply_filters(self, query: Any, filter_input: Any, model: type) -> Any:
        """Translate a filter input object into ORM-specific query conditions."""
        ...

    def apply_ordering(self, query: Any, order_input: Any, model: type) -> Any:
        """Translate a list of ``@oneOf`` order entries into ORM-specific ordering.

        Each entry represents one column; list position determines tie-break
        priority.
        """
        ...

    # -- Queryset overrides --------------------------------------------------

    def get_default_queryset(self, model: type) -> Any:
        """Return the default queryset for *model*."""
        ...

    def is_query_object(self, value: Any) -> bool:
        """Return ``True`` if *value* is a query object the optimizer can handle."""
        ...

    # -- Optimizer -----------------------------------------------------------

    def optimizer_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        """Return the schema extension class that handles query optimization."""
        ...

    def apply_optimizer_hints(
        self, store: Any, query: Any, info: Any
    ) -> AwaitableOrValue[Any]:
        """Apply field-level hints from *store* onto *query*."""
        ...
