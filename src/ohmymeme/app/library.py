"""表情目录的变更应用服务。"""

import sqlite3
from collections.abc import Callable, Sequence

from ohmymeme.app.library_queries import (
    LibraryPreferencesPort,
    LibraryQueries,
    NewCollectionResult,
)
from ohmymeme.core.ports.library import LibraryPersistencePort

from .remote_mutation_coordinator import (
    RemoteMutationBusyError,
    RemoteMutationConflictError,
    RemoteMutationCoordinator,
    RemoteMutationLease,
    RemoteMutationReentrantError,
)


class LibraryService(LibraryQueries):
    """协调目录变更，并保留桌面桥接的旧返回形状。"""

    def __init__(
        self,
        preferences: LibraryPreferencesPort,
        persistence: LibraryPersistencePort,
        build_manifest: Callable[[], None],
        find_file: Callable[[str], str | None] | None = None,
        is_animated: Callable[[str], bool] | None = None,
        mutation_coordinator: RemoteMutationCoordinator | None = None,
    ) -> None:
        super().__init__(preferences, persistence, find_file, is_animated)
        self._build_manifest = build_manifest
        self._mutation_coordinator = mutation_coordinator

    def set_meme_tags(self, meme_id: int, tags: Sequence[str] | None) -> bool:
        return self._persist(
            lambda: self._persistence.set_meme_tags(meme_id, tuple(tags or ())),
            "library.set_meme_tags",
        )

    def rename_meme(self, meme_id: int, new_name: str) -> bool:
        if not new_name:
            return False
        return self._persist_and_build(
            lambda: self._persistence.rename_meme(meme_id, new_name),
            "library.rename_meme",
        )

    def delete_meme(self, meme_id: int) -> bool:
        return self._persist_and_build(
            lambda: self._persistence.delete_meme(meme_id), "library.delete_meme"
        )

    def delete_memes(self, meme_ids: Sequence[int]) -> bool:
        return self._persist_and_build(
            lambda: self._persistence.delete_memes(meme_ids), "library.delete_memes"
        )

    def delete_all_metadata(self, lease: RemoteMutationLease | None = None) -> bool:
        if lease is not None:
            if self._mutation_coordinator is None:
                raise RemoteMutationReentrantError()
            self._mutation_coordinator.validate_lease(lease)
            self._persistence.delete_all()
            return True
        return self._persist(self._persistence.delete_all, "library.delete_all")

    def add_imported_to_collection(self, name: str, meme_ids: Sequence[int]) -> int:
        try:
            if self._mutation_coordinator is None:
                return self._add_imported_to_collection(name, meme_ids)
            with self._mutation_coordinator.mutation(
                "library.add_imported_to_collection"
            ) as lease:
                collection_id = self._add_imported_to_collection(name, meme_ids)
                if collection_id >= 0:
                    lease.commit()
                return collection_id
        except (
            RemoteMutationBusyError,
            RemoteMutationConflictError,
            OSError,
            sqlite3.Error,
        ):
            return -1

    def rebuild_manifest(self, lease: RemoteMutationLease | None = None) -> bool:
        if lease is not None:
            if self._mutation_coordinator is None:
                raise RemoteMutationReentrantError()
            self._mutation_coordinator.validate_lease(lease)
            self._build_manifest()
            return True
        return self._persist(self._build_manifest, "library.rebuild_manifest")

    def toggle_favorite(self, meme_id: int) -> bool:
        try:
            if self._mutation_coordinator is None:
                return self._persistence.toggle_favorite(meme_id)
            with self._mutation_coordinator.mutation(
                "library.toggle_favorite"
            ) as lease:
                result = self._persistence.toggle_favorite(meme_id)
                lease.commit()
                return result
        except (
            RemoteMutationBusyError,
            RemoteMutationConflictError,
            OSError,
            sqlite3.Error,
        ):
            return False

    def is_favorite(self, meme_id: int) -> bool:
        return self._persistence.is_favorite(meme_id)

    def add_to_collection(self, meme_id: int, name: str) -> bool:
        try:
            if self._mutation_coordinator is None:
                collection_id = self._persistence.create_collection(name)
                if collection_id < 0:
                    return False
                self._persistence.add_to_collection(meme_id, collection_id)
                return True
            with self._mutation_coordinator.mutation(
                "library.add_to_collection"
            ) as lease:
                collection_id = self._persistence.create_collection(name)
                if collection_id < 0:
                    return False
                self._persistence.add_to_collection(meme_id, collection_id)
                lease.commit()
                return True
        except (
            RemoteMutationBusyError,
            RemoteMutationConflictError,
            OSError,
            sqlite3.Error,
        ):
            return False

    def add_to_existing_collection(self, meme_id: int, collection_id: int) -> bool:
        return self._persist(
            lambda: self._persistence.add_to_collection(meme_id, collection_id),
            "library.add_to_existing_collection",
        )

    def set_collection_members(
        self, collection_id: int, meme_ids: Sequence[int]
    ) -> bool:
        return self._persist_and_build(
            lambda: self._persistence.set_collection_members(collection_id, meme_ids),
            "library.set_collection_members",
        )

    def create_collection_members(
        self, name: str, meme_ids: Sequence[int]
    ) -> NewCollectionResult:
        try:
            if self._mutation_coordinator is None:
                return self._create_collection_members(name, meme_ids)
            with self._mutation_coordinator.mutation(
                "library.create_collection_members"
            ) as lease:
                result = self._create_collection_members(name, meme_ids)
                if result.get("ok"):
                    lease.commit()
                return result
        except (
            RemoteMutationBusyError,
            RemoteMutationConflictError,
            OSError,
            sqlite3.Error,
        ):
            return {"ok": False}

    def _create_collection_members(
        self, name: str, meme_ids: Sequence[int]
    ) -> NewCollectionResult:
        try:
            if self._persistence.collection_exists(name):
                return {"ok": False, "error": "同名分组已存在，请从下拉框选择已有分组"}
            collection_id = self._persistence.create_collection(name)
            if collection_id < 0:
                return {"ok": False}
            self._persistence.set_collection_members(collection_id, meme_ids)
            self._build_manifest()
            return {"ok": True, "id": collection_id}
        except (OSError, sqlite3.Error):
            return {"ok": False}

    def reorder_memes(self, meme_ids: Sequence[int]) -> bool:
        return self._persist_and_build(
            lambda: self._persistence.reorder_memes(meme_ids), "library.reorder_memes"
        )

    def reorder_collections(self, collection_ids: Sequence[int]) -> bool:
        return self._persist_and_build(
            lambda: self._persistence.reorder_collections(collection_ids),
            "library.reorder_collections",
        )

    def reorder_collection_members(
        self, collection_id: int, meme_ids: Sequence[int]
    ) -> bool:
        return self._persist_and_build(
            lambda: self._persistence.reorder_collection_members(
                collection_id, meme_ids
            ),
            "library.reorder_collection_members",
        )

    def delete_collection(self, collection_id: int) -> bool:
        return self._persist(
            lambda: self._persistence.delete_collection(collection_id),
            "library.delete_collection",
        )

    def rename_collection(self, collection_id: int, new_name: str) -> bool:
        if not new_name:
            return False
        return self._persist_and_build(
            lambda: self._persistence.rename_collection(collection_id, new_name),
            "library.rename_collection",
        )

    def create_subcollection(self, name: str, parent_id: int) -> NewCollectionResult:
        if self._persistence.get_collection_depth(parent_id) >= 1:
            return {"ok": False, "error": "最大支持1层小分组"}
        try:
            if self._mutation_coordinator is None:
                collection_id = self._persistence.create_collection(name, parent_id)
                if collection_id >= 0:
                    return {"ok": True, "id": collection_id}
                return {"ok": False}
            with self._mutation_coordinator.mutation(
                "library.create_subcollection"
            ) as lease:
                collection_id = self._persistence.create_collection(name, parent_id)
                if collection_id < 0:
                    return {"ok": False}
                lease.commit()
                return {"ok": True, "id": collection_id}
        except (
            RemoteMutationBusyError,
            RemoteMutationConflictError,
            OSError,
            sqlite3.Error,
        ):
            return {"ok": False, "error": "同步正在进行中"}

    def record_meme_use(self, meme_id: int) -> bool:
        return self._persist(
            lambda: self._persistence.record_use(meme_id), "library.record_meme_use"
        )

    def remove_from_recent(self, meme_id: int) -> bool:
        return self._persist(
            lambda: self._persistence.remove_from_recent(meme_id),
            "library.remove_from_recent",
        )

    def clear_recent(self) -> bool:
        return self._persist(self._persistence.clear_recent, "library.clear_recent")

    def remove_from_collection(self, meme_id: int, collection_id: int) -> bool:
        return self._persist(
            lambda: self._persistence.remove_from_collection(meme_id, collection_id),
            "library.remove_from_collection",
        )

    def _add_imported_to_collection(self, name: str, meme_ids: Sequence[int]) -> int:
        collection_id = self._persistence.create_collection(name)
        if collection_id < 0:
            return collection_id
        for meme_id in meme_ids:
            self._persistence.add_to_collection(meme_id, collection_id)
        self._build_manifest()
        return collection_id

    def _persist_and_build(
        self, operation: Callable[[], None], entrypoint: str
    ) -> bool:
        if self._mutation_coordinator is not None:
            try:
                with self._mutation_coordinator.mutation(entrypoint) as lease:
                    operation()
                    self._build_manifest()
                    lease.commit()
                    return True
            except (
                RemoteMutationBusyError,
                RemoteMutationConflictError,
                OSError,
                sqlite3.Error,
            ):
                return False
        try:
            operation()
            self._build_manifest()
            return True
        except (OSError, sqlite3.Error):
            return False

    def _persist(self, operation: Callable[[], None], entrypoint: str) -> bool:
        if self._mutation_coordinator is not None:
            try:
                with self._mutation_coordinator.mutation(entrypoint) as lease:
                    operation()
                    lease.commit()
                    return True
            except (
                RemoteMutationBusyError,
                RemoteMutationConflictError,
                OSError,
                sqlite3.Error,
            ):
                return False
        try:
            operation()
            return True
        except (OSError, sqlite3.Error):
            return False
