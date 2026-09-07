from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Generic, Hashable, TypeVar

from .errors import DuplicateValueError

Identifier = TypeVar("Identifier", bound=Hashable)


@dataclass(frozen=True, slots=True)
class SortPosition(Generic[Identifier]):
    identifier: Identifier
    position: int


@dataclass(frozen=True, slots=True)
class StableOrder(Generic[Identifier]):
    identifiers: tuple[Identifier, ...]

    def __post_init__(self) -> None:
        seen: set[Identifier] = set()
        for identifier in self.identifiers:
            if identifier in seen:
                raise DuplicateValueError("sort_order", str(identifier))
            seen.add(identifier)

    @classmethod
    def from_ids(cls, identifiers: Iterable[Identifier]) -> StableOrder[Identifier]:
        return cls(tuple(identifiers))

    @property
    def positions(self) -> tuple[SortPosition[Identifier], ...]:
        return tuple(
            SortPosition(identifier, position)
            for position, identifier in enumerate(self.identifiers)
        )
