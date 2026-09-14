import importlib
import inspect
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
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
    # Given: valid, corrupt and oversized QQNT images and a cache output target.
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

    # Then: only the valid item commits through ImageImportService, not copy2.
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
    ini.write_bytes(
        "[UserDataSet]\nUserDataSavePath=C:\\QQ\n昵称=测试\n".encode("gb18030")
    )

    assert qqnt.read_file_with_correct_encoding(str(ini)) == "gb18030"


def test_qqnt_factory_context_sink_and_instance_isolation(tmp_path):
    # Exercise installed factory and real host admission, not the legacy wrapper.
    from ohmymeme_plugin_qqnt import create_plugin

    from ohmymeme.core.imports import HostImportSink
    from ohmymeme.core.plugins.contracts import ImportPluginContext
    from ohmymeme.core.plugins.policy import (
        PluginConfigPort,
        PluginOperation,
        PluginSecretPort,
    )
    from ohmymeme.presentation.desktop.api.plugin_dispatch import _descriptor

    userdata = _emoji_source(tmp_path)
    assets = AssetPaths(tmp_path / "data", tmp_path / "data/cache")
    database = MemeDB(tmp_path / "data/memes.db")
    sink = HostImportSink(ImageImportService(database, assets, lambda: None))
    descriptor = _descriptor("source.qqnt")
    first, second = create_plugin(), create_plugin()
    try:
        with PluginOperation(
            PluginConfigPort({}), PluginSecretPort({}), descriptor, tmp_path / "work"
        ) as operation:
            context = ImportPluginContext(
                descriptor,
                sink,
                lambda value: None,
                lambda: False,
                operation=operation,
                request={"qq_number": "10001", "userdata_save_path": str(userdata)},
            )
            assert first.start(context) is True
            assert first.start(context) is False
            first.stop()
            first.import_media(context)
            assert first.get_progress()["status"] == "cancelled"
            assert second.get_progress()["status"] == "idle"
            assert not database.search()
            assert first.start(context) is True
            first.import_media(context)
            assert first.get_progress()["result"]["copied"] == 1
            assert first.get_progress()["result"]["skipped"] == 2
            assert len(database.search()) == 1
            assert second.get_progress()["status"] == "idle"
            staging = operation.temporary.path("qqnt")
            assert staging.exists()
        assert not staging.exists()
    finally:
        database.close()


def test_qqnt_provider_rejects_output_path_in_request(tmp_path):
    # The provider cannot accept the host's persistent cache as its staging target.
    import pytest
    from ohmymeme_plugin_qqnt import create_plugin

    from ohmymeme.core.plugins.contracts import ImportPluginContext
    from ohmymeme.presentation.desktop.api.plugin_dispatch import _descriptor

    context = ImportPluginContext(
        _descriptor("source.qqnt"),
        None,
        lambda value: None,
        lambda: False,
        request={"output_dir": str(tmp_path / "cache")},
    )
    with pytest.raises(ValueError, match="output_dir"):
        create_plugin().start(context)
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("name", ["qqnt", "remote_import"])
@pytest.mark.parametrize("version", [True, 1.0], ids=["boolean", "float"])
def test_qa_fixture_version_rejects_non_integer_before_provider(
    tmp_path, monkeypatch, request, name, version
):
    # Preserve the reviewer's exact malformed CLI cases and stale-PASS attack.
    root = Path(__file__).resolve().parents[1]
    script = f"scripts/plugin_{name}_qa.py"
    fixture_name = (
        "qqnt-failure.json" if name == "qqnt" else "remote-import-failure.json"
    )
    data = json.loads(
        (root / "fixtures/plugin-parity" / fixture_name).read_text(encoding="utf-8")
    )
    data["schema_version"] = version
    source, report = tmp_path / "input.json", tmp_path / "report.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    report.write_text('{"status":"PASS","expected":"PASS"}', encoding="utf-8")
    dirty = tmp_path / "user-note.txt"
    dirty.write_bytes(b"preserve unrelated user content\n")
    argv = [
        "mise",
        "exec",
        "--",
        "python",
        script,
        "--fixture",
        str(source),
        "--report",
        str(report),
    ]
    completed = subprocess.run(
        argv,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    actual = json.loads(report.read_text(encoding="utf-8"))
    qa = importlib.import_module("scripts." + Path(script).stem)
    provider_probe = Mock(return_value={})
    monkeypatch.setattr(qa, "probe", provider_probe)
    monkeypatch.setattr(
        sys, "argv", [script, "--fixture", str(source), "--report", str(report)]
    )
    in_process_exit = qa.main()
    facts = {
        "script": script,
        "input": data,
        "argv": argv,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "report": actual,
        "guard_exit": in_process_exit,
        "provider_called": provider_probe.called,
        "dirty_preserved": dirty.read_bytes() == b"preserve unrelated user content\n",
    }
    request.node.user_properties.append(("review_attack", json.dumps(facts)))
    assert completed.returncode != 0 and actual["status"] == "REJECTED", facts
    assert actual["observations"] == {} and "schema_version" in repr(actual["errors"])
    assert in_process_exit != 0
    provider_probe.assert_not_called()
    assert facts["dirty_preserved"]
