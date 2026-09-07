"""表情目录的读取应用服务。"""

import os
import sqlite3
from collections.abc import Callable, Sequence
from typing import Protocol, TypedDict

from ohmymeme.core.ports.library import LibraryMemeRow, LibraryPersistencePort


class LibraryPreferencesPort(Protocol):
    def get(self, key: str, default: bool) -> bool: ...


class BridgeMeme(TypedDict):
    id: int
    filename: str
    name: str
    file_hash: str
    from_stego: int
    width: int
    height: int
    mime_type: str
    is_gif: bool
    is_animated: bool
    favorited: bool
    auto_play_gif: bool
    hover_to_play: bool


class BridgeCollection(TypedDict, total=False):
    id: int
    name: str
    count: int
    children: list["BridgeCollection"]


class NewCollectionResult(TypedDict, total=False):
    ok: bool
    id: int
    error: str


class LibraryInitData(TypedDict):
    memes: list[BridgeMeme]
    tags: list[str]
    collections: list[BridgeCollection]
    show_startup_animation: bool
    startup_bg_color: str


class LibraryQueries:
    """通过目录端口读取桥接所需的目录视图。"""

    def __init__(
        self,
        preferences: LibraryPreferencesPort,
        persistence: LibraryPersistencePort,
        find_file: Callable[[str], str | None] | None = None,
        is_animated: Callable[[str], bool] | None = None,
    ) -> None:
        self._preferences = preferences
        self._persistence = persistence
        self._find_file = find_file
        self._is_animated = is_animated

    def search_memes(
        self,
        keyword: str = "",
        tags: Sequence[str] | None = None,
        collection_id: int | None = None,
        offset: int = 0,
        limit: int = 200,
    ) -> list[BridgeMeme]:
        if collection_id == -3:
            rows = self._persistence.get_recent(limit, offset)
        else:
            rows = self._persistence.search(
                keyword,
                tuple(tags or ()),
                self._collection_filter(collection_id),
                collection_id == -2,
                collection_id == -4,
                offset,
                limit,
            )
        return [self._meme_dto(row) for row in rows]

    def count_memes(
        self,
        keyword: str = "",
        tags: Sequence[str] | None = None,
        collection_id: int | None = None,
    ) -> int:
        if collection_id == -3:
            return self._persistence.count_recent()
        return self._persistence.count(
            keyword,
            tuple(tags or ()),
            self._collection_filter(collection_id),
            collection_id == -2,
            collection_id == -4,
        )

    def get_tags(self) -> list[str]:
        return list(self._persistence.get_all_tags())

    def get_meme_tags(self, meme_id: int) -> list[str]:
        try:
            return list(self._persistence.get_meme_tags(meme_id))
        except (OSError, sqlite3.Error):
            return []

    def get_meme_filename(self, meme_id: int) -> str:
        row = self._persistence.get_by_id(meme_id)
        return row.filename if row is not None else ""

    def get_meme_display_name(self, meme_id: int) -> str:
        row = self._persistence.get_by_id(meme_id)
        if row is None:
            return ""
        return row.original_name or os.path.splitext(row.filename)[0]

    def has_meme_hash(self, file_hash: str) -> bool:
        return self._persistence.has_hash(file_hash)

    def has_meme_filename(self, filename: str) -> bool:
        return self._persistence.has_filename(filename)

    def get_all_meme_filenames(self) -> tuple[str, ...]:
        return tuple(
            row.filename
            for row in self._persistence.search("", (), (), False, False, 0, 999999)
        )

    def get_collections(self) -> list[BridgeCollection]:
        system: list[BridgeCollection] = [
            {"id": -2, "name": "收藏夹", "count": self._favorite_count()},
            {"id": -3, "name": "最近使用", "count": self._persistence.count_recent()},
        ]
        if self._preferences.get("show_uncategorized", True):
            system.append(
                {"id": -4, "name": "未分类", "count": self._uncategorized_count()}
            )
        return system + self._collection_tree()

    def get_init_data(self, startup_bg_color: str) -> LibraryInitData:
        return {
            "memes": self.search_memes(limit=200),
            "tags": self.get_tags(),
            "collections": self.get_collections(),
            "show_startup_animation": self._preferences.get(
                "show_startup_animation", True
            ),
            "startup_bg_color": startup_bg_color,
        }

    def get_child_collections(self, parent_id: int) -> list[dict[str, int | str]]:
        return [
            {"id": child.id, "name": child.name}
            for child in self._persistence.get_child_collections(parent_id)
        ]

    def search_collections(self, keyword: str = "") -> list[dict[str, int | str]]:
        normalized = keyword.strip().lower()
        return [
            {"id": item["id"], "name": item["name"], "depth": item["depth"]}
            for item in self._flatten_collections()
            if not normalized or normalized in item["name"].lower()
        ][:20]

    def get_collection_members(self, collection_id: int) -> list[BridgeMeme]:
        try:
            return self.search_memes(collection_id=collection_id, limit=5000)
        except (OSError, sqlite3.Error):
            return []

    def _favorite_count(self) -> int:
        return self._persistence.count("", (), (), True, False)

    def _uncategorized_count(self) -> int:
        return self._persistence.count("", (), (), False, True)

    def _collection_filter(self, collection_id: int | None) -> tuple[int, ...]:
        if collection_id is None or collection_id < 1:
            return ()
        return self._collection_ids(collection_id, ())

    def _collection_ids(
        self, collection_id: int, visited: tuple[int, ...]
    ) -> tuple[int, ...]:
        if collection_id in visited:
            return ()
        children = self._persistence.get_child_collections(collection_id)
        current = visited + (collection_id,)
        return (collection_id,) + tuple(
            descendant
            for child in children
            for descendant in self._collection_ids(child.id, current)
        )

    def _collection_tree(self, parent_id: int | None = None) -> list[BridgeCollection]:
        result: list[BridgeCollection] = []
        for row in self._persistence.get_collections():
            if row.parent_id != parent_id:
                continue
            children = self._collection_tree(row.id)
            item: BridgeCollection = {
                "id": row.id,
                "name": row.name,
                "count": self._persistence.count(
                    "", (), self._collection_ids(row.id, ()), False, False
                ),
            }
            if children:
                item["children"] = children
            result.append(item)
        return result

    def _flatten_collections(self) -> list[dict[str, int | str]]:
        flattened: list[dict[str, int | str]] = []

        def walk(items: Sequence[BridgeCollection], depth: int) -> None:
            for item in items:
                flattened.append(
                    {"id": item["id"], "name": item["name"], "depth": depth}
                )
                walk(item.get("children", ()), depth + 1)

        walk(self._collection_tree(), 0)
        return flattened

    def _meme_dto(self, row: LibraryMemeRow) -> BridgeMeme:
        lowered = row.filename.lower()
        is_gif = row.mime_type.endswith("gif") or lowered.endswith(".gif")
        animated = is_gif
        if (
            lowered.endswith(".webp")
            and self._find_file is not None
            and self._is_animated is not None
        ):
            path = self._find_file(row.filename)
            animated = self._is_animated(path) if path is not None else False
        return {
            "id": row.id,
            "filename": row.filename,
            "name": row.original_name or os.path.splitext(row.filename)[0],
            "file_hash": row.file_hash,
            "from_stego": row.from_stego,
            "width": row.width,
            "height": row.height,
            "mime_type": row.mime_type,
            "is_gif": is_gif,
            "is_animated": animated,
            "favorited": self._persistence.is_favorite(row.id),
            "auto_play_gif": self._preferences.get("auto_play_gif", True),
            "hover_to_play": self._preferences.get("hover_to_play", False),
        }
