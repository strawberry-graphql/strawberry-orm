"""OptimizerStore: per-field metadata consumed by the OptimizerExtension."""

from __future__ import annotations

from dataclasses import dataclass, field

from strawberry_orm.types import FieldHints


@dataclass
class OptimizerStore:
    """Registry of field-level optimization hints for a schema.

    Populated during schema construction when ``orm.field()`` /
    ``orm.type()`` are called. The ``OptimizerExtension`` reads this store
    at resolve time to decide what eager-loads and column restrictions to
    apply.
    """

    hints: dict[str, dict[str, FieldHints]] = field(default_factory=dict)

    def register(
        self,
        type_name: str,
        field_name: str,
        hints: FieldHints,
    ) -> None:
        self.hints.setdefault(type_name, {})[field_name] = hints

    def get(self, type_name: str, field_name: str) -> FieldHints | None:
        return self.hints.get(type_name, {}).get(field_name)
