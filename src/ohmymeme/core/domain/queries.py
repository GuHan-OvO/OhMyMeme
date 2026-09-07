from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from .errors import DuplicateValueError, InvalidDomainValue
from .values import CollectionId, TagName


@dataclass(frozen=True, slots=True)
class AllMemes:
    pass


@dataclass(frozen=True, slots=True)
class InCollection:
    collection_id: CollectionId


@dataclass(frozen=True, slots=True)
class FavoriteMemes:
    pass


@dataclass(frozen=True, slots=True)
class RecentMemes:
    pass


@dataclass(frozen=True, slots=True)
class UncategorizedMemes:
    pass


MemeScope = AllMemes | InCollection | FavoriteMemes | RecentMemes | UncategorizedMemes


@dataclass(frozen=True, slots=True)
class TagFilter:
    values: tuple[TagName, ...] = ()

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.values, key=lambda tag: tag.value.casefold()))
        seen: set[str] = set()
        for tag in ordered:
            normalized = tag.value.casefold()
            if normalized in seen:
                raise DuplicateValueError("tags", tag.value)
            seen.add(normalized)
        object.__setattr__(self, "values", ordered)

    @classmethod
    def from_tags(cls, values: tuple[TagName, ...] | list[TagName]) -> TagFilter:
        return cls(tuple(values))


@dataclass(frozen=True, slots=True)
class Page:
    offset: int = 0
    limit: int = 200

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise InvalidDomainValue("offset", "must not be negative")
        if self.limit < 1:
            raise InvalidDomainValue("limit", "must be positive")


@dataclass(frozen=True, slots=True)
class MemeQuery:
    keyword: str = ""
    tags: TagFilter = TagFilter()
    scope: MemeScope = AllMemes()
    page: Page = Page()

    def __post_init__(self) -> None:
        object.__setattr__(self, "keyword", self.keyword.strip())


def scope_key(scope: MemeScope) -> str:
    match scope:
        case AllMemes():
            return "all"
        case InCollection(collection_id=collection_id):
            return f"collection:{collection_id.value}"
        case FavoriteMemes():
            return "favorites"
        case RecentMemes():
            return "recent"
        case UncategorizedMemes():
            return "uncategorized"
        case unreachable:
            assert_never(unreachable)
