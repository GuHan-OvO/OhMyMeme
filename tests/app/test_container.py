from pathlib import Path

import pytest

from ohmymeme.app.container import Container
from ohmymeme.core.domain import TaskKind


def test_containers_isolate_config_database_and_manifest(tmp_path):
    first = Container(tmp_path / "first")
    second = Container(tmp_path / "second")
    try:
        assert first.config is not second.config
        assert first.db is not second.db
        assert first.config.config_dir != second.config.config_dir
        assert first.db._db_path != second.db._db_path
        first.config.set("hotkey", "Ctrl+Shift+X")
        first.config.save()
        first.db.add_meme("first.png", file_hash="a" * 64)
        first.build_manifest()
        assert first.assets.manifest_path.exists()
        assert not second.assets.manifest_path.exists()
        assert second.config.get("hotkey") == "Ctrl+Alt+N"
        assert second.db.count() == 0
    finally:
        first.close()
        second.close()


def test_container_routes_lan_through_its_operation_coordinator(tmp_path):
    container = Container(tmp_path / "app")
    try:
        assert container.lan._coordinator is container.operations
    finally:
        container.close()


def test_container_shutdown_reports_a_running_lan_service(tmp_path):
    container = Container(tmp_path / "app")
    assert container.lan.start(0, "")

    report = container.close()

    assert any(
        task_id.value.startswith(TaskKind.LAN_SERVICE.value)
        for task_id in report.cancelled
    )
    assert report.inventory.threads == ()
    assert report.inventory.sockets == ()


def test_close_is_idempotent_and_continues_after_component_failure(tmp_path):
    events = []

    class BrokenHotkey:
        def unregister(self):
            events.append("hotkey")
            raise RuntimeError("expected")

    class Tray:
        def stop(self):
            events.append("tray")

    class WebUI:
        def stop(self):
            events.append("webui")

    container = Container(tmp_path / "app")
    original_close = container.db.close
    original_save = container.config.save

    def close_db():
        events.append("db")
        original_close()

    def save_config():
        events.append("config")
        original_save()

    container.db.close = close_db
    object.__setattr__(container.config, "save", save_config)
    with pytest.raises(ExceptionGroup):
        container.close(BrokenHotkey(), Tray(), lambda: events.append("lan"), WebUI())
    container.close(BrokenHotkey(), Tray(), lambda: events.append("lan"), WebUI())

    assert events == ["hotkey", "tray", "lan", "webui", "db", "config"]
    assert Path(container.config.config_dir / "config.json").exists()


def test_presentation_uses_the_supplied_container_without_global_factories(
    monkeypatch, tmp_path
):
    import ohmymeme.presentation.desktop.window_manager as webui_module

    first = Container(tmp_path / "first")
    second = Container(tmp_path / "second")
    monkeypatch.setattr(
        webui_module,
        "get_config",
        lambda: (_ for _ in ()).throw(AssertionError("global Config factory called")),
        raising=False,
    )
    monkeypatch.setattr(
        webui_module,
        "get_db",
        lambda: (_ for _ in ()).throw(AssertionError("global DB factory called")),
        raising=False,
    )
    try:
        first_ui = first.create_webui()
        second_ui = second.create_webui()

        assert first_ui._cfg is first.config
        assert first_ui._library is first.catalog
        assert not hasattr(first_ui, "_db")
        assert first_ui._api._catalog is first.catalog
        assert first_ui._settings_api._settings is first.settings
        assert second_ui._cfg is second.config
        assert second_ui._library is second.catalog
        assert not hasattr(second_ui, "_db")
        assert second_ui._api._catalog is second.catalog
        assert second_ui._settings_api._settings is second.settings
    finally:
        first.close()
        second.close()
