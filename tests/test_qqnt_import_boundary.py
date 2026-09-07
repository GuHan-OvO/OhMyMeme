import inspect
import io
from pathlib import Path

from PIL import Image

from ohmymeme.core.assets import AssetPaths
from ohmymeme.core.database import MemeDB
from ohmymeme.core.imports import ImageImportService, ImportPath
from ohmymeme.integrations.imports import qqnt
from ohmymeme.presentation.desktop import window_manager


def _png(width=1):
    output = io.BytesIO()
    Image.new("RGBA", (width, 1), (255, 0, 0, 255)).save(output, "PNG")
    return output.getvalue()


def _emoji_source(root):
    source = (
        root
        / "userdata"
        / "10001"
        / "nt_qq"
        / "nt_data"
        / "Emoji"
        / "personal_emoji"
        / "Ori"
    )
    source.mkdir(parents=True)
    (source / "valid.png").write_bytes(_png())
    (source / "corrupt.png").write_bytes(b"not an image")
    (source / "oversize.png").write_bytes(_png(2561))
    return source.parents[5]


def _library_importer(service):
    def import_paths(paths):
        result = service.import_batch(
            tuple(ImportPath(Path(path), Path(path).stem) for path in paths)
        )
        return {"ids": list(result.imported_ids), "rejected": result.rejected}

    return import_paths


def test_qqnt_cache_target_routes_every_image_through_atomic_import(tmp_path):
    # Given: QQNT files including valid, corrupt and oversize images and a cache output target
    userdata = _emoji_source(tmp_path)
    assets = AssetPaths(tmp_path / "data", tmp_path / "data" / "cache")
    database = MemeDB(tmp_path / "data" / "memes.db")
    service = ImageImportService(
        database, assets, lambda: assets.manifest_path.write_text("manifest")
    )

    # When: the GUI worker receives the application cache as its selected output
    window_manager._QQNT_CANCEL = False
    window_manager._qqnt_worker(
        "10001",
        str(assets.cache_dir),
        False,
        False,
        None,
        str(userdata),
        str(assets.cache_dir),
        _library_importer(service),
    )

    # Then: only the valid item commits through ImageImportService, never raw copy2 paths
    result = window_manager.get_qqnt_progress()["result"]
    assert result["copied"] == 1
    assert result["skipped"] == 2
    assert len(database.search()) == 1
    assert len(list(assets.cache_dir.iterdir())) == 1
    assert assets.manifest_path.exists()
    database.close()


def test_qqnt_external_output_preserves_copy_export_semantics(tmp_path):
    # Given: an ordinary user external export directory
    userdata = _emoji_source(tmp_path)
    output = tmp_path / "external-export"

    # When: the reusable extractor handles the external export directly
    result = qqnt.extract_qq_emojis("10001", str(output), str(userdata))

    # Then: it preserves copy2 export behavior without an application cache dependency
    assert result["output_dir"] == str(output)
    assert result["copied"] == 3
    assert (output / "valid.png").exists()
    assert (output / "corrupt.png").exists()


def test_qqnt_detects_cache_aliases_children_and_parents(tmp_path):
    # Given: cache-relative output paths, including a lexical alias
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True)

    # When/Then: no equivalent, child or parent target can use direct export copying
    assert qqnt.targets_library_output(str(cache), str(cache))
    assert qqnt.targets_library_output(str(cache / "nested"), str(cache))
    assert qqnt.targets_library_output(str(cache.parent), str(cache))
    assert qqnt.targets_library_output(str(cache / ".." / "cache"), str(cache))
    assert not qqnt.targets_library_output(str(tmp_path / "external"), str(cache))


def test_qqnt_cache_target_cancellation_creates_no_library_state(tmp_path):
    # Given: a cache-target extraction already cancelled before library admission
    userdata = _emoji_source(tmp_path)
    assets = AssetPaths(tmp_path / "data", tmp_path / "data" / "cache")
    database = MemeDB(tmp_path / "data" / "memes.db")
    service = ImageImportService(database, assets, lambda: None)

    # When: the GUI worker reaches its controlled staging path
    window_manager._QQNT_CANCEL = True
    window_manager._qqnt_worker(
        "10001",
        str(assets.cache_dir),
        False,
        False,
        None,
        str(userdata),
        str(assets.cache_dir),
        _library_importer(service),
    )

    # Then: cancellation leaves no cache asset, database row or manifest
    assert window_manager.get_qqnt_progress()["status"] == "cancelled"
    assert not database.search()
    assert not assets.cache_dir.exists()
    assert not assets.manifest_path.exists()
    database.close()


def test_qqnt_cache_target_deduplicates_through_image_import_service(tmp_path):
    # Given: the same valid QQNT image is extracted twice into the library target
    userdata = _emoji_source(tmp_path)
    assets = AssetPaths(tmp_path / "data", tmp_path / "data" / "cache")
    database = MemeDB(tmp_path / "data" / "memes.db")
    service = ImageImportService(database, assets, lambda: None)
    importer = _library_importer(service)

    # When: both GUI worker passes route through the cache-target path
    for _ in range(2):
        window_manager._QQNT_CANCEL = False
        window_manager._qqnt_worker(
            "10001",
            str(assets.cache_dir),
            False,
            False,
            None,
            str(userdata),
            str(assets.cache_dir),
            importer,
        )

    # Then: the content-addressed library retains only the first valid image
    assert len(database.search()) == 1
    assert len(list(assets.cache_dir.iterdir())) == 1
    database.close()


def test_qqnt_gui_start_signature_remains_compatible():
    # Given/When: the settings bridge is inspected by existing GUI callers
    from ohmymeme.presentation.desktop.window_manager import SettingsApi

    # Then: the public bridge method shape has not changed
    assert str(inspect.signature(SettingsApi.qqnt_start)) == (
        "(self, qq_number: str, output_dir: str, image_only: bool = False, "
        "overwrite: bool = False) -> dict"
    )


def test_qqnt_encoding_probe_preserves_gb18030_compatibility(tmp_path):
    ini = tmp_path / "UserDataInfo.ini"
    ini.write_bytes("[UserDataSet]\nUserDataSavePath=C:\\QQ\n昵称=测试\n".encode("gb18030"))

    assert qqnt.read_file_with_correct_encoding(str(ini)) == "gb18030"
