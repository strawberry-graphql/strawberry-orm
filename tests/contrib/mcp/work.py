"""Counting the work one tool call makes the server do, and the thread doing it."""

from __future__ import annotations

import asyncio
import importlib
import threading
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

import graphql.execution.values as execution_values
import pytest
import strawberry.types.arguments as arguments
from graphql.language.parser import Parser
from graphql.validation import ExecutableDefinitionsRule
from strawberry.types.base import StrawberryOptional
from strawberry.types.lazy_type import LazyType

# ``graphql.utilities`` exports a function of the module's name.
coerce_input_value = importlib.import_module("graphql.utilities.coerce_input_value")


def on_loop() -> bool:
    """True on the thread running the tests' event loop."""
    return threading.current_thread() is threading.main_thread()


@dataclass
class Work:
    #: Values Strawberry converted into resolver arguments, not counting the
    #: step through ``Optional`` it takes on the way to each.
    converted: int = 0
    #: Literal values graphql-core coerced into resolver arguments.
    coerced: int = 0
    #: Each document parsed: its length, and whether the event loop parsed it.
    parses: list[tuple[int, bool]] = field(default_factory=list)
    #: Each pass of graphql-core's own validation rules: whether the loop ran it.
    validations: list[bool] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Work:
        convert = arguments.convert_argument
        coerce = coerce_input_value.coerce_input_literal
        parse_document = Parser.parse_document
        start = ExecutableDefinitionsRule.__init__

        def converting(value: Any, type_: Any, *args: Any, **kwargs: Any) -> Any:
            if not isinstance(type_, StrawberryOptional | LazyType):
                self.converted += 1
            return convert(value, type_, *args, **kwargs)

        def coercing(*args: Any, **kwargs: Any) -> Any:
            self.coerced += 1
            return coerce(*args, **kwargs)

        def parsing(parser: Parser) -> Any:
            self.parses.append((len(parser._lexer.source.body), on_loop()))
            return parse_document(parser)

        def validating(rule: Any, context: Any) -> None:
            self.validations.append(on_loop())
            start(rule, context)

        monkeypatch.setattr(arguments, "convert_argument", converting)
        monkeypatch.setattr(coerce_input_value, "coerce_input_literal", coercing)
        monkeypatch.setattr(execution_values, "coerce_input_literal", coercing)
        monkeypatch.setattr(Parser, "parse_document", parsing)
        monkeypatch.setattr(ExecutableDefinitionsRule, "__init__", validating)
        return self


async def longest_stall[T](call: Awaitable[T]) -> tuple[T, float]:
    """*call*'s result, and the longest the event loop went without a turn
    while it ran."""
    longest = 0.0
    done = False

    async def tick() -> None:
        nonlocal longest
        last = time.perf_counter()
        while not done:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            longest = max(longest, now - last)
            last = now

    ticker = asyncio.create_task(tick())
    try:
        result = await call
    finally:
        done = True
        await ticker
    return result, longest
