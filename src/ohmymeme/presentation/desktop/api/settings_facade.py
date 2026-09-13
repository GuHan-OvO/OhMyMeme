"""Validated settings-window bridge facade."""

from ohmymeme.core.schemas.bridge import SETTINGS_BRIDGE_CONTRACT

from .facade_base import FacadeBase


class SettingsBridgeFacade(FacadeBase):
    """Preserve the historical settings-window SettingsApi ABI."""

    _surface = "settings"

    def __init__(self, webui, settings) -> None:
        from ..window_manager import _LegacySettingsApi

        self._legacy = _LegacySettingsApi(webui, settings)
        self._contract = SETTINGS_BRIDGE_CONTRACT
        self._webui = webui
        self._cfg = webui._cfg
        self._library = webui._library
        self._settings = settings
        self._sync = self._legacy._sync

    def _ensure_legacy(self):
        if hasattr(self, "_legacy"):
            return
        from ..window_manager import _LegacySettingsApi

        legacy = _LegacySettingsApi.__new__(_LegacySettingsApi)
        legacy.__dict__.update(self.__dict__)
        self._legacy = legacy

    def check_connectivity(self) -> dict:
        return self._call("check_connectivity", (), {"ok": False, "latency": ""})

    def lan_start(self, port: int = None, secret: str = None) -> dict:
        return self._call("lan_start", (port, secret), {"ok": False})

    def lan_stop(self) -> dict:
        return self._call("lan_stop", (), {"ok": False})

    def lan_get_status(self) -> dict:
        return self._call("lan_get_status", (), {})

    def lan_get_ip(self) -> str:
        return self._call("lan_get_ip", (), "")

    def lan_set_allow_secret_config(self, enabled: bool) -> dict:
        return self._call("lan_set_allow_secret_config", (enabled,), {"ok": False})

    def get_settings(self) -> dict:
        return self._call("get_settings", (), {})

    def refresh_memes(self):
        return self._call("refresh_memes", (), None)

    def refresh_tags(self):
        return self._call("refresh_tags", (), None)

    def refresh_collections(self):
        return self._call("refresh_collections", (), None)

    def save_settings(self, settings: dict):
        return self._call("save_settings", (settings,), None)

    def reset_settings(self) -> dict:
        return self._call("reset_settings", (), {})

    def move_window(self, dx: int, dy: int):
        return self._call("move_window", (dx, dy), None)

    def start_window_drag(self, button: int, root_x: int, root_y: int) -> bool:
        return self._call("start_window_drag", (button, root_x, root_y), False)

    def start_qq_import(self) -> dict:
        return self._call("start_qq_import", (), {"ok": False})

    def get_qq_import_progress(self) -> dict:
        return self._call("get_qq_import_progress", (), {})

    def save_qq_zip(self) -> dict:
        return self._call("save_qq_zip", (), {"ok": False})

    def open_adb_folder(self) -> bool:
        return self._call("open_adb_folder", (), False)

    def export_logs(self) -> dict:
        return self._call("export_logs", (), {"ok": False})

    def open_adb_help(self) -> bool:
        return self._call("open_adb_help", (), False)

    def cancel_qq_import(self):
        return self._call("cancel_qq_import", (), None)

    def pick_tg_tdata(self) -> dict:
        return self._call("pick_tg_tdata", (), {"ok": False})

    def start_tg_import(self, tdata_path=None, passcode="", convert_webm=True) -> dict:
        return self._call(
            "start_tg_import", (tdata_path, passcode, convert_webm), {"ok": False}
        )

    def get_tg_import_progress(self) -> dict:
        return self._call("get_tg_import_progress", (), {})

    def cancel_tg_import(self):
        return self._call("cancel_tg_import", (), None)

    def start_douyin_import(self, cookie: str) -> dict:
        return self._call("start_douyin_import", (cookie,), {"ok": False})

    def get_douyin_import_progress(self) -> dict:
        return self._call("get_douyin_import_progress", (), {})

    def cancel_douyin_import(self):
        return self._call("cancel_douyin_import", (), None)

    def pick_wechat_root(self):
        return self._call("pick_wechat_root", (), {"ok": False})

    def inspect_wechat_environment(self, user_root=None):
        return self._call("inspect_wechat_environment", (user_root,), {})

    def list_wechat_stickers(self, user_root, account_path=None):
        return self._call("list_wechat_stickers", (user_root, account_path), {})

    def start_wechat_import(self, user_root=None, download=True, account_path=None):
        return self._call(
            "start_wechat_import", (user_root, download, account_path), {"ok": False}
        )

    def get_wechat_import_progress(self):
        return self._call("get_wechat_import_progress", (), {})

    def cancel_wechat_import(self):
        return self._call("cancel_wechat_import", (), None)

    def qqnt_check_env(self) -> dict:
        return self._call("qqnt_check_env", (), {})

    def qqnt_pick_ini(self) -> dict:
        return self._call("qqnt_pick_ini", (), {"ok": False})

    def qqnt_pick_userdata(self) -> dict:
        return self._call("qqnt_pick_userdata", (), {"ok": False})

    def qqnt_pick_base(self) -> dict:
        return self._call("qqnt_pick_base", (), {"ok": False})

    def get_storage_info(self):
        return self._call("get_storage_info", (), {})

    def pick_storage_dir(self):
        return self._call("pick_storage_dir", (), {"ok": False})

    def apply_storage_dir(self, path, move_files=False):
        return self._call("apply_storage_dir", (path, move_files), {"ok": False})

    def qqnt_default_dir(self, base: str, qq_number: str) -> dict:
        return self._call("qqnt_default_dir", (base, qq_number), {"ok": False})

    def qqnt_start(
        self,
        qq_number: str,
        output_dir: str,
        image_only: bool = False,
        overwrite: bool = False,
    ) -> dict:
        return self._call(
            "qqnt_start", (qq_number, output_dir, image_only, overwrite), {"ok": False}
        )

    def qqnt_get_progress(self) -> dict:
        return self._call("qqnt_get_progress", (), {})

    def qqnt_cancel(self):
        return self._call("qqnt_cancel", (), None)

    def qqnt_open_dir(self, path: str) -> bool:
        return self._call("qqnt_open_dir", (path,), False)

    def import_memes(self) -> dict:
        return self._call("import_memes", (), {"ok": False})

    def close_settings(self):
        return self._call("close_settings", (), None)

    def get_current_version(self) -> str:
        return self._call("get_current_version", (), "")

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

    def get_sync_progress(self) -> dict:
        return self._call("get_sync_progress", (), {})

    def sync_push(self, delete_remote: bool = None) -> dict:
        return self._call(
            "sync_push",
            (delete_remote,),
            {"ok": False, "error": "sync failed", "failed_files": []},
        )

    def sync_pull(self, remove_local: bool = None) -> dict:
        return self._call(
            "sync_pull",
            (remove_local,),
            {"ok": False, "error": "sync failed", "failed_files": []},
        )

    def sync_test(self) -> str:
        return self._call("sync_test", (), "")

    def delete_all_local(self) -> dict:
        return self._call("delete_all_local", (), {"ok": False})

    def delete_all_cloud(self) -> dict:
        return self._call("delete_all_cloud", (), {"ok": False})

    def get_remote_orphans(self, delete: bool = False) -> dict:
        return self._call("get_remote_orphans", (delete,), {"ok": False})

    def check_sync_status(self) -> dict:
        return self._call("check_sync_status", (), {"ok": False})
