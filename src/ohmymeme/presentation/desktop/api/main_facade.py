"""Validated main-window bridge facade."""

from ohmymeme.core.schemas.bridge import MAIN_BRIDGE_CONTRACT

from .facade_base import FacadeBase


class MainBridgeFacade(FacadeBase):
    """Preserve the historical main-window JsApi ABI."""

    def __init__(self, webui, catalog, settings) -> None:
        from ..window_manager import _LegacyJsApi

        self._legacy = _LegacyJsApi(webui, catalog, settings)
        self._contract = MAIN_BRIDGE_CONTRACT
        self._webui = webui
        self._cfg = webui._cfg
        self._catalog = catalog
        self._settings = settings
        self._sync = self._legacy._sync
        self._find_meme_file = self._legacy._find_meme_file

    def _ensure_legacy(self):
        if hasattr(self, "_legacy"):
            return
        from ..window_manager import _LegacyJsApi

        legacy = _LegacyJsApi.__new__(_LegacyJsApi)
        legacy.__dict__.update(self.__dict__)
        self._legacy = legacy

    def search_memes(
        self, keyword="", tags=None, collection_id=None, offset=0, limit=200
    ):
        return self._call(
            "search_memes", (keyword, tags, collection_id, offset, limit), []
        )

    def count_memes(self, keyword="", tags=None, collection_id=None) -> int:
        return self._call("count_memes", (keyword, tags, collection_id), 0)

    def get_tags(self) -> list:
        return self._call("get_tags", (), [])

    def get_meme_tags(self, meme_id):
        return self._call("get_meme_tags", (meme_id,), [])

    def set_meme_tags(self, meme_id, tags):
        return self._call("set_meme_tags", (meme_id, tags), False)

    def get_init_data(self) -> dict:
        return self._call("get_init_data", (), {})

    def get_meme_path(self, meme_id: int) -> str:
        return self._call("get_meme_path", (meme_id,), "")

    def get_meme_paths(self, meme_ids: list) -> dict:
        return self._call("get_meme_paths", (meme_ids,), {})

    def start_native_drag(self, meme_id: int) -> bool:
        self._legacy._find_meme_file = self._find_meme_file
        return self._call("start_native_drag", (meme_id,), False)

    def copy_meme(self, meme_id):
        self._legacy._find_meme_file = self._find_meme_file
        return self._call(
            "copy_meme", (meme_id,), {"ok": False, "status": "copy_failed"}
        )

    def toggle_favorite(self, meme_id: int) -> bool:
        return self._call("toggle_favorite", (meme_id,), False)

    def is_favorite(self, meme_id: int) -> bool:
        return self._call("is_favorite", (meme_id,), False)

    def rename_meme(self, meme_id: int, new_name: str) -> bool:
        return self._call("rename_meme", (meme_id, new_name), False)

    def delete_meme(self, meme_id: int) -> bool:
        return self._call("delete_meme", (meme_id,), False)

    def delete_memes(self, meme_ids: list) -> dict:
        return self._call("delete_memes", (meme_ids,), {"ok": False, "deleted": 0})

    def get_collections(self) -> list:
        return self._call("get_collections", (), [])

    def get_child_collections(self, parent_id: int) -> list:
        return self._call("get_child_collections", (parent_id,), [])

    def search_collections(self, keyword: str = "") -> list:
        return self._call("search_collections", (keyword,), [])

    def get_collection_members(self, collection_id: int) -> list:
        return self._call("get_collection_members", (collection_id,), [])

    def add_to_collection(self, meme_id: int, name: str) -> bool:
        return self._call("add_to_collection", (meme_id, name), False)

    def add_to_existing_collection(self, meme_id: int, collection_id: int) -> bool:
        return self._call("add_to_existing_collection", (meme_id, collection_id), False)

    def set_collection_members(self, collection_id: int, meme_ids: list) -> bool:
        return self._call("set_collection_members", (collection_id, meme_ids), False)

    def set_collection_members_new(self, name: str, meme_ids: list) -> dict:
        return self._call("set_collection_members_new", (name, meme_ids), {"ok": False})

    def reorder_memes(self, meme_ids: list) -> bool:
        return self._call("reorder_memes", (meme_ids,), False)

    def reorder_collections(self, collection_ids: list) -> bool:
        return self._call("reorder_collections", (collection_ids,), False)

    def reorder_collection_members(self, collection_id: int, meme_ids: list) -> bool:
        return self._call(
            "reorder_collection_members", (collection_id, meme_ids), False
        )

    def delete_collection(self, collection_id: int) -> bool:
        return self._call("delete_collection", (collection_id,), False)

    def rename_collection(self, collection_id: int, new_name: str) -> bool:
        return self._call("rename_collection", (collection_id, new_name), False)

    def create_subcollection(self, name: str, parent_id: int) -> dict:
        return self._call("create_subcollection", (name, parent_id), {"ok": False})

    def record_meme_use(self, meme_id: int) -> bool:
        return self._call("record_meme_use", (meme_id,), False)

    def remove_from_recent(self, meme_id: int) -> bool:
        return self._call("remove_from_recent", (meme_id,), False)

    def clear_recent(self) -> bool:
        return self._call("clear_recent", (), False)

    def remove_from_collection(self, meme_id: int, collection_id: int) -> bool:
        return self._call("remove_from_collection", (meme_id, collection_id), False)

    def log(self, msg, level="info"):
        return self._call("log", (msg, level), None)

    def rescan_cache(self) -> bool:
        return self._call("rescan_cache", (), False)

    def check_update(self, debug=False, force=False) -> dict:
        return self._call("check_update", (debug, force), {"pending": False})

    def start_download(self, url: str) -> bool:
        return self._call("start_download", (url,), False)

    def get_download_progress(self) -> dict:
        return self._call("get_download_progress", (), {})

    def run_downloaded_installer(self) -> bool:
        return self._call("run_downloaded_installer", (), False)

    def download_update(self, url: str) -> dict:
        return self._call(
            "download_update", (url,), {"ok": False, "error": "download failed"}
        )

    def check_connectivity(self) -> dict:
        return self._call("check_connectivity", (), {"ok": False, "latency": ""})

    def download_original_image(self, url: str) -> dict:
        return self._call(
            "download_original_image", (url,), {"ok": False, "error": "导入失败"}
        )

    def get_sync_progress(self) -> dict:
        return self._call("get_sync_progress", (), {})

    def sync_push(self) -> dict:
        return self._call(
            "sync_push", (), {"ok": False, "error": "sync failed", "failed_files": []}
        )

    def sync_pull(self) -> dict:
        return self._call(
            "sync_pull", (), {"ok": False, "error": "sync failed", "failed_files": []}
        )

    def run_auto_sync(self) -> dict:
        return self._call(
            "run_auto_sync", (), {"fetched": False, "synced": False, "error": ""}
        )

    def sync_test(self) -> str:
        return self._call("sync_test", (), "")

    def import_memes(self) -> dict:
        return self._call("import_memes", (), {"ok": False})

    def import_folder(self, make_collection=True) -> dict:
        return self._call("import_folder", (make_collection,), {"ok": False})

    def import_from_clipboard(self) -> dict:
        return self._call("import_from_clipboard", (), {"ok": False})

    def open_settings(self):
        return self._call("open_settings", (), None)

    def lan_confirm_device(self, approved: bool, confirm_id: str = "") -> dict:
        return self._call("lan_confirm_device", (approved, confirm_id), {"ok": False})

    def get_settings(self) -> dict:
        return self._call("get_settings", (), {})

    def save_settings(self, settings: dict):
        return self._call("save_settings", (settings,), None)

    def reset_settings(self) -> dict:
        return self._call("reset_settings", (), {})

    def move_window(self, dx: int, dy: int):
        return self._call("move_window", (dx, dy), None)

    def start_window_drag(self, button: int, root_x: int, root_y: int) -> bool:
        return self._call("start_window_drag", (button, root_x, root_y), False)

    def hide_window(self):
        return self._call("hide_window", (), None)
