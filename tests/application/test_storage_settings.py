from pathlib import Path

from ohmymeme.presentation.desktop import window_manager


def test_storage_settings_rejects_network_path_and_preserves_local_validation(tmp_path):
    old = tmp_path / "old-cache"
    old.mkdir()

    assert window_manager._storage_dir_validation(
        str(tmp_path / "new-cache"), str(old), (tmp_path / "data",)
    ) == (True, "")
    assert window_manager._storage_dir_validation(
        "https://evil.example/cache", str(old), ()
    ) == (False, "请选择绝对路径")


def test_storage_settings_validation_keeps_protected_roots_closed(tmp_path):
    old = tmp_path / "old-cache"
    data = tmp_path / "data"
    old.mkdir()
    data.mkdir()

    assert window_manager._storage_dir_validation(str(data), str(old), (data,))[0] is False
    assert Path(old).exists()
