"""表情目录应用服务的持久化端口。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LibraryMemeRow:
    id: int
    filename: str
    original_name: str
    file_hash: str
    from_stego: int
    width: int
    height: int
    mime_type: str


@dataclass(frozen=True, slots=True)
class LibraryCollectionRow:
    id: int
    name: str
    parent_id: int | None
    sort_order: int


@dataclass(frozen=True, slots=True)
class LibraryChildCollection:
    id: int
    name: str


class LibraryPersistencePort(Protocol):
    def get_by_id(self, meme_id: int) -> LibraryMemeRow | None: ...

    def has_hash(self, file_hash: str) -> bool: ...

    def has_filename(self, filename: str) -> bool: ...

    def search(
        self,
        keyword: str,
        tags: Sequence[str],
        collection_ids: Sequence[int],
        favorite_only: bool,
        uncategorized_only: bool,
        offset: int,
        limit: int,
    ) -> Sequence[LibraryMemeRow]: ...

    def count(
        self,
        keyword: str,
        tags: Sequence[str],
        collection_ids: Sequence[int],
        favorite_only: bool,
        uncategorized_only: bool,
    ) -> int: ...

    def get_recent(self, limit: int, offset: int) -> Sequence[LibraryMemeRow]: ...

    def count_recent(self) -> int: ...

    def get_all_tags(self) -> Sequence[str]: ...

    def get_meme_tags(self, meme_id: int) -> Sequence[str]: ...

    def set_meme_tags(self, meme_id: int, tags: Sequence[str]) -> None: ...

    def rename_meme(self, meme_id: int, new_name: str) -> None: ...

    def delete_meme(self, meme_id: int) -> None: ...

    def delete_memes(self, meme_ids: Sequence[int]) -> None: ...

    def delete_all(self) -> None: ...

    def toggle_favorite(self, meme_id: int) -> bool: ...

    def is_favorite(self, meme_id: int) -> bool: ...

    def create_collection(self, name: str, parent_id: int | None = None) -> int: ...

    def collection_exists(self, name: str, parent_id: int | None = None) -> bool: ...

    def add_to_collection(self, meme_id: int, collection_id: int) -> None: ...

    def remove_from_collection(self, meme_id: int, collection_id: int) -> None: ...

    def set_collection_members(
        self, collection_id: int, meme_ids: Sequence[int]
    ) -> None: ...

    def get_collections(self) -> Sequence[LibraryCollectionRow]: ...

    def get_child_collections(
        self, parent_id: int
    ) -> Sequence[LibraryChildCollection]: ...

    def get_collection_depth(self, collection_id: int) -> int: ...

    def delete_collection(self, collection_id: int) -> None: ...

    def rename_collection(self, collection_id: int, new_name: str) -> None: ...

    def reorder_memes(self, meme_ids: Sequence[int]) -> None: ...

    def reorder_collections(self, collection_ids: Sequence[int]) -> None: ...

    def reorder_collection_members(
        self, collection_id: int, meme_ids: Sequence[int]
    ) -> None: ...

    def record_use(self, meme_id: int) -> None: ...

    def remove_from_recent(self, meme_id: int) -> None: ...

    def clear_recent(self) -> None: ...
