from dataclasses import dataclass

from .errors import (
    CollectionCycleError,
    DuplicateCollectionError,
    UnknownCollectionParentError,
)
from .values import CollectionId, CollectionName


@dataclass(frozen=True, slots=True)
class Collection:
    id: CollectionId
    name: CollectionName
    parent_id: CollectionId | None = None

    def __post_init__(self) -> None:
        if self.parent_id == self.id:
            raise CollectionCycleError((self.id, self.id))


@dataclass(frozen=True, slots=True)
class CollectionGraph:
    collections: tuple[Collection, ...]

    def __post_init__(self) -> None:
        ordered = tuple(
            sorted(self.collections, key=lambda collection: collection.id.value)
        )
        object.__setattr__(self, "collections", ordered)
        by_id: dict[CollectionId, Collection] = {}
        for collection in ordered:
            if collection.id in by_id:
                raise DuplicateCollectionError(collection.id)
            by_id[collection.id] = collection
        for collection in ordered:
            if collection.parent_id is not None and collection.parent_id not in by_id:
                raise UnknownCollectionParentError(collection.id, collection.parent_id)
        for collection in ordered:
            path = (collection.id,)
            current = collection
            while current.parent_id is not None:
                parent_id = current.parent_id
                if parent_id in path:
                    start = path.index(parent_id)
                    raise CollectionCycleError(path[start:] + (parent_id,))
                path += (parent_id,)
                current = by_id[parent_id]

    @property
    def roots(self) -> tuple[CollectionId, ...]:
        return tuple(
            collection.id
            for collection in self.collections
            if collection.parent_id is None
        )

    def children_of(self, parent_id: CollectionId) -> tuple[CollectionId, ...]:
        return tuple(
            collection.id
            for collection in self.collections
            if collection.parent_id == parent_id
        )
