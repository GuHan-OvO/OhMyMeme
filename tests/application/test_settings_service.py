from ohmymeme.app.settings import Settings
from ohmymeme.core.config import Config


def test_settings_when_saved_then_known_values_persist_and_unknown_input_is_ignored(tmp_path):
    # Given: a real configuration and platform callbacks
    config = Config(tmp_path / "config.json", tmp_path / "data")
    auto_start = []
    settings = Settings(config, lambda: False, auto_start.append)

    # When: the settings service receives a mixed legacy payload
    hotkey = settings.save_settings(
        {"hotkey": "Ctrl+Shift+S", "unknown_setting": "ignored", "auto_start": True}
    )

    # Then: the public result and stored configuration retain legacy semantics
    assert hotkey == "Ctrl+Shift+S"
    assert auto_start == [True]
    assert Config(tmp_path / "config.json").get("hotkey") == "Ctrl+Shift+S"
    assert "unknown_setting" not in config.to_dict()


def test_settings_when_reset_then_custom_cache_root_survives_and_platform_autostart_disables(
    tmp_path,
):
    # Given: non-default configuration including a user-owned cache root
    cache_dir = tmp_path / "custom-cache"
    config = Config(tmp_path / "config.json", tmp_path / "data")
    config.set("cache_dir", str(cache_dir))
    config.set("hotkey", "Ctrl+Shift+R")
    config.save()
    auto_start = []
    settings = Settings(config, lambda: True, auto_start.append)

    # When: users reset settings from the application service
    result = settings.reset_settings()

    # Then: cache location remains while all reset values and platform state are compatible
    reloaded = Config(tmp_path / "config.json", tmp_path / "data")
    assert result["hotkey"] == "Ctrl+Alt+N"
    assert reloaded.get("cache_dir") == str(cache_dir)
    assert reloaded.cache_dir == cache_dir.resolve()
    assert auto_start == [False]
