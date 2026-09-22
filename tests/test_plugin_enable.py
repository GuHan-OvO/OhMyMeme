# pyright: basic

"""插件启用/禁用配置与运行时门控测试。"""

from ohmymeme.app.container import Container
from ohmymeme.core.plugins.manifest import CANONICAL_PROVIDERS


def test_enabled_plugins_cover_canonical_nine(tmp_path):
    # 默认全部启用，WebUI 导入门控与 Container 一致
    container = Container(tmp_path / "host")
    try:
        all_ids = {provider_id for provider_id, _, _ in CANONICAL_PROVIDERS}
        assert container.enabled_plugins == all_ids
        webui = container.create_webui()
        assert webui._enabled_import_plugins == all_ids
    finally:
        container.close()
        container.db.close()


def test_disabled_plugins_persist_and_filter(tmp_path):
    # 禁用列表持久化后，重启时从 enabled 集合中移除
    root = tmp_path / "host"
    first = Container(root)
    try:
        first.config.set("disabled_plugins", ["source.qqnt", "sync.ftp"])
        first.config.save()
    finally:
        first.close()
        first.db.close()
    second = Container(root)
    try:
        assert "source.qqnt" not in second.enabled_plugins
        assert "sync.ftp" not in second.enabled_plugins
        assert "source.telegram" in second.enabled_plugins
    finally:
        second.close()
        second.db.close()
