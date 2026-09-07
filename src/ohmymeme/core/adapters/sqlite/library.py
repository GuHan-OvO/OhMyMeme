"""将兼容 MemeDB 门面适配为目录持久化端口。"""

from collections.abc import Sequence

from ohmymeme.core.database import MemeDB
from ohmymeme.core.ports.library import (
    LibraryChildCollection,
    LibraryCollectionRow,
    LibraryMemeRow,
)


class MemeDbLibraryPort:
    """仅通过 MemeDB 的公开兼容方法提供目录持久化。"""

    def __init__(self, database: MemeDB) -> None:
        self._database = database

    def get_by_id(self, meme_id: int) -> LibraryMemeRow | None:
        row = self._database.get_by_id(meme_id)
        return self._meme(row) if row is not None else None

    def has_hash(self, file_hash: str) -> bool:
        return self._database.get_by_hash(file_hash) is not None

    def has_filename(self, filename: str) -> bool:
        return self._database.get_by_filename(filename) is not None

    def search(
        self,
        keyword: str,
        tags: Sequence[str],
        collection_ids: Sequence[int],
        favorite_only: bool,
        uncategorized_only: bool,
        offset: int,
        limit: int,
    ) -> tuple[LibraryMemeRow, ...]:
        collection_id = list(collection_ids) if collection_ids else None
        rows = self._database.search(
            keyword=keyword,
            tags=list(tags),
            collection_id=collection_id,
            favorite_only=favorite_only,
            uncategorized_only=uncategorized_only,
            offset=offset,
            limit=limit,
        )
        return tuple(self._meme(row) for row in rows)

    def count(
        self,
        keyword: str,
        tags: Sequence[str],
        collection_ids: Sequence[int],
        favorite_only: bool,
        uncategorized_only: bool,
    ) -> int:
        collection_id = list(collection_ids) if collection_ids else None
        return self._database.count(
            keyword=keyword,
            tags=list(tags),
            collection_id=collection_id,
            favorite_only=favorite_only,
            uncategorized_only=uncategorized_only,
        )

    def get_recent(self, limit: int, offset: int) -> tuple[LibraryMemeRow, ...]:
        return tuple(
            self._meme(row) for row in self._database.get_recent(limit, offset)
        )

    def count_recent(self) -> int:
        return self._database.count_recent()

    def get_all_tags(self) -> tuple[str, ...]:
        return tuple(self._database.get_all_tags())

    def get_meme_tags(self, meme_id: int) -> tuple[str, ...]:
        return tuple(self._database.get_meme_tags(meme_id))

    def set_meme_tags(self, meme_id: int, tags: Sequence[str]) -> None:
        self._database.set_meme_tags(meme_id, list(tags))

    def rename_meme(self, meme_id: int, new_name: str) -> None:
        self._database.update_meme(meme_id, original_name=new_name)

    def delete_meme(self, meme_id: int) -> None:
        self._database.delete_meme(meme_id)

    def delete_memes(self, meme_ids: Sequence[int]) -> None:
        self._database.delete_memes(list(meme_ids))

    def delete_all(self) -> None:
        self._database.delete_all()

    def toggle_favorite(self, meme_id: int) -> bool:
        return self._database.toggle_favorite(meme_id)

    def is_favorite(self, meme_id: int) -> bool:
        return self._database.is_favorite(meme_id)

    def create_collection(self, name: str, parent_id: int | None = None) -> int:
        return self._database.create_collection(name, parent_id)

    def collection_exists(self, name: str, parent_id: int | None = None) -> bool:
        return self._database.collection_exists(name, parent_id)

    def add_to_collection(self, meme_id: int, collection_id: int) -> None:
        self._database.add_to_collection(meme_id, collection_id)

    def remove_from_collection(self, meme_id: int, collection_id: int) -> None:
        self._database.remove_from_collection(meme_id, collection_id)

    def set_collection_members(
        self, collection_id: int, meme_ids: Sequence[int]
    ) -> None:
        self._database.set_collection_members(collection_id, list(meme_ids))

    def get_collections(self) -> tuple[LibraryCollectionRow, ...]:
        collections = self._database.get_collections()
        return tuple(
            LibraryCollectionRow(collection_id, name, parent_id, sort_order)
            for collection_id, name, parent_id, sort_order in collections
        )

    def get_child_collections(
        self, parent_id: int
    ) -> tuple[LibraryChildCollection, ...]:
        return tuple(
            LibraryChildCollection(row["id"], row["name"])
            for row in self._database.get_child_collections(parent_id)
        )

    def get_collection_depth(self, collection_id: int) -> int:
        return self._database.get_collection_depth(collection_id)

    def delete_collection(self, collection_id: int) -> None:
        self._database.delete_collection(collection_id)

    def rename_collection(self, collection_id: int, new_name: str) -> None:
        self._database.rename_collection(collection_id, new_name)

    def reorder_memes(self, meme_ids: Sequence[int]) -> None:
        self._database.reorder_memes(list(meme_ids))

    def reorder_collections(self, collection_ids: Sequence[int]) -> None:
        self._database.reorder_collections(list(collection_ids))

    def reorder_collection_members(
        self, collection_id: int, meme_ids: Sequence[int]
    ) -> None:
        self._database.reorder_collection_members(collection_id, list(meme_ids))

    def record_use(self, meme_id: int) -> None:
        self._database.record_use(meme_id)

    def remove_from_recent(self, meme_id: int) -> None:
        self._database.remove_from_recent(meme_id)

    def clear_recent(self) -> None:
        self._database.clear_recent()

    @staticmethod
    def _meme(row: dict[str, int | str | None]) -> LibraryMemeRow:
        return LibraryMemeRow(
            id=row["id"],
            filename=row["filename"],
            original_name=row.get("original_name") or "",
            file_hash=row.get("file_hash") or "",
            from_stego=row.get("from_stego") or 0,
            width=row.get("width") or 0,
            height=row.get("height") or 0,
            mime_type=row.get("mime_type") or "",
        )
