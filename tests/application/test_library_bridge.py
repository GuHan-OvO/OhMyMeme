import inspect
from pathlib import Path

from ohmymeme.presentation.desktop import window_manager
from ohmymeme.presentation.desktop.window_manager import JsApi, SettingsApi, WebUI
from ohmymeme.app.library import LibraryService
from ohmymeme.core.adapters.sqlite.library import MemeDbLibraryPort
from ohmymeme.core.config import Config
from ohmymeme.core.database import MemeDB


class RejectingDatabase:
    def __getattr__(self, name):
        raise AssertionError(f"JsApi bypassed LibraryService via database.{name}")


class RejectingContainer:
    def build_manifest(self):
        raise AssertionError("JsApi bypassed LibraryService via build_manifest")


class LibraryDouble:
    def __init__(self):
        self.calls = []

    def rename_meme(self, meme_id, name):
        self.calls.append(("rename_meme", meme_id, name))
        return True

    def get_meme_tags(self, meme_id):
        self.calls.append(("get_meme_tags", meme_id))
        return ["tag"]

    def set_meme_tags(self, meme_id, tags):
        self.calls.append(("set_meme_tags", meme_id, tuple(tags)))
        return True

    def get_meme_filename(self, meme_id):
        self.calls.append(("get_meme_filename", meme_id))
        return f"{meme_id}.png"

    def delete_meme(self, meme_id):
        self.calls.append(("delete_meme", meme_id))
        return True

    def delete_memes(self, meme_ids):
        self.calls.append(("delete_memes", tuple(meme_ids)))

    def add_imported_to_collection(self, name, meme_ids):
        self.calls.append(("add_imported_to_collection", name, tuple(meme_ids)))
        return 17

    def record_meme_use(self, meme_id):
        self.calls.append(("record_meme_use", meme_id))
        return True

    def remove_from_recent(self, meme_id):
        self.calls.append(("remove_from_recent", meme_id))
        return True

    def clear_recent(self):
        self.calls.append(("clear_recent",))
        return True

    def get_collection_members(self, collection_id):
        self.calls.append(("get_collection_members", collection_id))
        return [{"id": 1}]

    def set_collection_members(self, collection_id, meme_ids):
        self.calls.append(("set_collection_members", collection_id, tuple(meme_ids)))
        return True

    def delete_all_metadata(self):
        self.calls.append(("delete_all_metadata",))

    def rebuild_manifest(self):
        self.calls.append(("rebuild_manifest",))


class ConfigDouble:
    def __init__(self, root):
        self.cache_dir = root
        self.thumbnail_dir = root / "thumbnails"
        self.thumbnail_dir.mkdir()

    def get(self, key, default=None):
        return True if key == "record_recent_use" else default


class WebUiDouble:
    def __init__(self, root):
        self._cfg = ConfigDouble(root)
        self._db = RejectingDatabase()
        self._container = RejectingContainer()
        self._file_cache = {}
        self.imported = []
        self._library = None

    def _do_import(self, paths, names=None):
        self.imported.append((tuple(paths), tuple(names or ())))
        return {"ids": [4, 5], "rejected": 1}

    def schedule_hide(self):
        return None


class DialogWindow:
    def create_file_dialog(self, *_args, **_kwargs):
        return (str(self.folder),)


def _api(root):
    webui = WebUiDouble(root)
    library = LibraryDouble()
    return JsApi(webui, library, None), webui, library


def test_bridge_when_library_mutations_run_then_only_library_service_owns_persistence(
    tmp_path, monkeypatch
):
    # Given: a bridge with a database that fails if any library path touches it
    api, webui, library = _api(tmp_path)
    folder = tmp_path / "memes"
    folder.mkdir()
    (folder / "one.png").write_bytes(b"image")
    dialog = DialogWindow()
    dialog.folder = folder
    monkeypatch.setattr(window_manager.webview, "windows", [dialog])

    # When: every Todo13 library category uses its existing bridge API
    tags = api.get_meme_tags(3)
    tagged = api.set_meme_tags(3, ["tag"])
    renamed = api.rename_meme(3, "renamed")
    deleted = api.delete_meme(3)
    batch = api.delete_memes([3, 3, 8])
    imported = api.import_folder(True)
    recorded = api.record_meme_use(3)
    removed_recent = api.remove_from_recent(3)
    cleared_recent = api.clear_recent()
    members = api.get_collection_members(17)
    members_set = api.set_collection_members(17, [3, 8])

    # Then: legacy result shapes remain while persistence stays behind the service
    assert tags == ["tag"]
    assert tagged is True
    assert renamed is True
    assert deleted is True
    assert batch == {"ok": True, "deleted": 2}
    assert imported == {
        "ok": True,
        "imported": 2,
        "rejected": 1,
        "collection_id": 17,
        "collection_name": "memes",
    }
    assert recorded is True
    assert removed_recent is True
    assert cleared_recent is True
    assert members == [{"id": 1}]
    assert members_set is True
    assert library.calls == [
        ("get_meme_tags", 3),
        ("set_meme_tags", 3, ("tag",)),
        ("rename_meme", 3, "renamed"),
        ("get_meme_filename", 3),
        ("delete_meme", 3),
        ("get_meme_filename", 3),
        ("get_meme_filename", 8),
        ("delete_memes", (3, 8)),
        ("add_imported_to_collection", "memes", (4, 5)),
        ("record_meme_use", 3),
        ("remove_from_recent", 3),
        ("clear_recent",),
        ("get_collection_members", 17),
        ("set_collection_members", 17, (3, 8)),
    ]
    assert webui.imported == [((str(folder / "one.png"),), ("one",))]


def test_bridge_when_library_contract_is_inspected_then_public_signatures_stay_frozen():
    # Given: every Todo13-owned bridge entrypoint
    methods = (
        "get_meme_tags",
        "set_meme_tags",
        "rename_meme",
        "delete_meme",
        "delete_memes",
        "get_collection_members",
        "set_collection_members",
        "record_meme_use",
        "remove_from_recent",
        "clear_recent",
        "import_folder",
    )

    # When: public bridge call signatures are examined
    signatures = {name: str(inspect.signature(getattr(JsApi, name))) for name in methods}

    # Then: the presentation ABI remains unchanged by application delegation
    assert signatures == {
        "get_meme_tags": "(self, meme_id)",
        "set_meme_tags": "(self, meme_id, tags)",
        "rename_meme": "(self, meme_id: int, new_name: str) -> bool",
        "delete_meme": "(self, meme_id: int) -> bool",
        "delete_memes": "(self, meme_ids: list) -> dict",
        "get_collection_members": "(self, collection_id: int) -> list",
        "set_collection_members": "(self, collection_id: int, meme_ids: list) -> bool",
        "record_meme_use": "(self, meme_id: int) -> bool",
        "remove_from_recent": "(self, meme_id: int) -> bool",
        "clear_recent": "(self) -> bool",
        "import_folder": "(self, make_collection=True) -> dict",
    }


def test_bridge_when_real_temporary_database_is_used_then_rename_and_delete_project_once(
    tmp_path,
):
    # Given: a real application library with a temporary cache, database and manifest sink
    config = Config(tmp_path / "config.json", tmp_path / "data")
    database = MemeDB(config.db_path)
    config.cache_dir.joinpath("one.png").write_bytes(b"image")
    meme_id = database.add_meme("one.png", original_name="one")
    projections = []
    library = LibraryService(
        config, MemeDbLibraryPort(database), lambda: projections.append("manifest")
    )
    webui = WebUiDouble(tmp_path)
    webui._cfg = config
    api = JsApi(webui, library, None)

    # When: public bridge mutations rename and delete the imported meme
    renamed = api.rename_meme(meme_id, "renamed")
    deleted = api.delete_meme(meme_id)

    # Then: the observable bridge booleans, database result and projection count agree
    assert renamed is True
    assert deleted is True
    assert database.get_by_id(meme_id) is None
    assert projections == ["manifest", "manifest"]


def test_bridge_when_todo13_library_methods_are_read_then_no_direct_database_or_manifest_bypass():
    # Given: the complete primary bridge facade
    source = inspect.getsource(JsApi)

    # When: its implementation is inspected for bypasses
    forbidden = ("self._db", "_container.build_manifest")

    # Then: only the application boundary owns library persistence/projection
    assert not any(marker in source for marker in forbidden)


def test_webui_when_library_routes_are_read_then_no_direct_database_or_manifest_bypass():
    # Given: the presentation owner for cache scanning and HTTP import routes
    source = inspect.getsource(WebUI)

    # When: its library-facing implementation is inspected
    forbidden = ("self._db", "self._container.build_manifest")

    # Then: application services own persistence and projection
    assert not any(marker in source for marker in forbidden)


def test_settings_bridge_when_delete_all_runs_then_library_service_owns_metadata_and_projection(
    tmp_path,
):
    # Given: settings bridge files and a rejecting presentation database attribute
    webui = WebUiDouble(tmp_path)
    library = LibraryDouble()
    webui._library = library
    webui._cfg.cache_dir.joinpath("meme.png").write_bytes(b"image")
    api = SettingsApi(webui, None)

    # When: the public local-library cleanup entrypoint is called
    result = api.delete_all_local()

    # Then: the legacy result shape remains while metadata/projection delegate
    assert result == {"ok": True}
    assert library.calls == [("delete_all_metadata",), ("rebuild_manifest",)]
    assert not webui._cfg.cache_dir.joinpath("meme.png").exists()


def test_settings_bridge_when_source_is_read_then_all_four_reported_bypasses_fail_closed():
    # Given: the settings presentation bridge containing storage/delete/status routes
    source = inspect.getsource(SettingsApi)

    # When: every verifier-reported private persistence/projection form is scanned
    forbidden = ("self._webui._db", "self._webui._container.build_manifest")

    # Then: tests reject any recurrence, including callback-form projection use
    assert not any(marker in source for marker in forbidden)


def test_task13_evidence_audit_when_source_has_private_presentation_access_then_rejects_it(
    tmp_path,
):
    # Given: a source candidate carrying every verifier-reported bypass form
    from scripts.build_task_13_evidence import build_task_report

    source = tmp_path / "window_manager.py"
    source.write_text(
        "self._webui._db\nself._webui._container.build_manifest()\n",
        encoding="utf-8",
    )

    # When: evidence derives the source-bound audit result
    audit = build_task_report(source)["source_audit"]

    # Then: a report cannot claim a passing boundary while bypasses are live
    assert audit["passed"] is False
    assert audit["private_database"] == 1
    assert audit["direct_projection"] == 1


def test_task13_evidence_audit_when_live_presentation_is_scanned_then_matches_boundary():
    # Given: the current desktop presentation source
    from scripts.build_task_13_evidence import audit_presentation_source

    source = (
        Path(__file__).parents[2]
        / "src"
        / "ohmymeme"
        / "presentation"
        / "desktop"
        / "window_manager.py"
    )

    # When: evidence derives the audit from the parsed source tree
    audit = audit_presentation_source(source)

    # Then: live source contains no private DB or direct projection ownership
    assert audit["passed"] is True
    assert audit["private_database"] == 0
    assert audit["direct_projection"] == 0
