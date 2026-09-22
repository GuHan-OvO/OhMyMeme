# pyright: basic

"""第三方插件 ZIP 安装/卸载与包安全测试。"""

import json
import zipfile

import pytest

from ohmymeme.core.plugins.runtime import seeding


def _write_zip(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _meta(**overrides):
    meta = {
        "id": "community.demo",
        "name": "Demo",
        "version": "1.0.0",
        "entry": "demo_plugin:create_plugin",
        "kind": "source",
        "api_version": 1,
    }
    meta.update(overrides)
    return json.dumps(meta)


def test_install_plugin_extracts_and_registers(tmp_path):
    data = tmp_path / "data"
    package = _write_zip(
        tmp_path / "demo.zip",
        {"plugin.json": _meta(), "demo_plugin.py": "def create_plugin():\n    pass\n"},
    )
    entry = seeding.install_plugin(data, package)
    assert entry["id"] == "community.demo"
    assert entry["version"] == "1.0.0"
    assert seeding.resolve_plugin(data, "community.demo")["entry"] == (
        "demo_plugin:create_plugin"
    )
    assert (
        seeding.plugins_dir(data) / "community.demo" / "1.0.0" / "demo_plugin.py"
    ).is_file()
    assert seeding.installed_plugins(data)[0]["name"] == "Demo"


def test_install_plugin_update_switches_active_version(tmp_path):
    data = tmp_path / "data"
    first = _write_zip(
        tmp_path / "a.zip", {"plugin.json": _meta(), "demo_plugin.py": "x = 1\n"}
    )
    second = _write_zip(
        tmp_path / "b.zip",
        {"plugin.json": _meta(version="2.0.0"), "demo_plugin.py": "x = 2\n"},
    )
    seeding.install_plugin(data, first)
    entry = seeding.install_plugin(data, second)
    assert entry["version"] == "2.0.0"
    assert (seeding.plugins_dir(data) / "community.demo" / "1.0.0").is_dir()


@pytest.mark.parametrize(
    "meta,reason",
    [
        (_meta(id="sync.ftp"), "invalid_plugin_id"),
        (_meta(id="demo"), "invalid_plugin_id"),
        (_meta(version="../escape"), "invalid_version"),
        (_meta(entry="demo_plugin"), "invalid_entry"),
        (_meta(kind="other"), "invalid_kind"),
        (_meta(api_version=2), "invalid_api_version"),
    ],
)
def test_install_plugin_rejects_invalid_metadata(tmp_path, meta, reason):
    data = tmp_path / "data"
    package = _write_zip(tmp_path / "bad.zip", {"plugin.json": meta})
    with pytest.raises(seeding.PluginSeedError) as error:
        seeding.install_plugin(data, package)
    assert error.value.reason == reason


def test_install_plugin_rejects_traversal_member(tmp_path):
    data = tmp_path / "data"
    package = _write_zip(
        tmp_path / "evil.zip",
        {"plugin.json": _meta(), "../escape.py": "x = 1\n"},
    )
    with pytest.raises(seeding.PluginSeedError) as error:
        seeding.install_plugin(data, package)
    assert error.value.reason == "unsafe_package"
    assert not (tmp_path / "escape.py").exists()


def test_uninstall_plugin_removes_third_party_only(tmp_path):
    data = tmp_path / "data"
    package = _write_zip(
        tmp_path / "demo.zip", {"plugin.json": _meta(), "demo_plugin.py": "x = 1\n"}
    )
    seeding.install_plugin(data, package)
    assert seeding.uninstall_plugin(data, "community.demo") is True
    assert not (seeding.plugins_dir(data) / "community.demo").exists()
    with pytest.raises(seeding.PluginSeedError) as error:
        seeding.resolve_plugin(data, "community.demo")
    assert error.value.reason == "plugin_missing"
    with pytest.raises(seeding.PluginSeedError) as error:
        seeding.uninstall_plugin(data, "source.qqnt")
    assert error.value.reason == "official_plugin"
