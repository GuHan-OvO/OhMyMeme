import inspect
import io

import pytest
from PIL import Image

from ohmymeme.core.assets import AssetPaths
from ohmymeme.core.database import MemeDB
from ohmymeme.core.imports import ImageImportService
from ohmymeme.integrations.imports import qqnt


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
