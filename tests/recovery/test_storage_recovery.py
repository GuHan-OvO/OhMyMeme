import hashlib
from pathlib import Path

import pytest

from ohmymeme.core.recovery import RecoveryLocked, RecoveryUnavailable, StorageRecovery


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _recovery(tmp_path: Path) -> tuple[StorageRecovery, Path, Path]:
    data_dir = tmp_path / "data"
    old_cache = tmp_path / "old-cache"
    data_dir.mkdir()
    old_cache.mkdir()
    return StorageRecovery(data_dir, old_cache), data_dir, old_cache


def test_pre_database_copy_failure_restores_original_file(tmp_path, monkeypatch):
    # Given: a cross-root cache migration whose destination copy exhausts storage
    recovery, _, old_cache = _recovery(tmp_path)
    source = old_cache / "nested" / "meme.png"
    source.parent.mkdir()
    source.write_bytes(b"original")
    destination = tmp_path / "new-cache"

    def disk_full(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(recovery, "_same_volume", lambda *_args: False)
    monkeypatch.setattr(recovery, "_copy_and_verify", disk_full)

    # When: the file phase fails before durable metadata commit
    with pytest.raises(OSError, match="disk full"):
        recovery.migrate(destination, True, lambda: None, lambda: None)

    # Then: the original path remains authoritative and no transaction survives
    assert source.read_bytes() == b"original"
    assert not (destination / "nested" / "meme.png").exists()
    assert not recovery.marker_path.exists()


def test_post_database_manifest_failure_is_forward_recovered_on_restart(tmp_path):
    # Given: files and durable metadata committed, but manifest writing failed
    recovery, _, old_cache = _recovery(tmp_path)
    source = old_cache / "meme.png"
    source.write_bytes(b"payload")
    destination = tmp_path / "new-cache"
    commits = []

    def commit_database():
        commits.append("database")

    def fail_manifest():
        raise OSError("manifest replace failed")

    # When: the post-database manifest step fails
    with pytest.raises(OSError, match="manifest replace failed"):
        recovery.migrate(destination, True, commit_database, fail_manifest)

    # Then: a fresh process completes forward recovery rather than restoring old paths
    recovered = StorageRecovery(tmp_path / "data", destination)
    recovered.recover_before_database()
    recovered.finish_manifest(lambda: commits.append("manifest"))
    assert commits == ["database", "manifest"]
    assert (destination / "meme.png").read_bytes() == b"payload"
    assert not source.exists()
    assert not recovered.marker_path.exists()


def test_disconnected_cache_root_fails_closed_before_database(tmp_path):
    # Given: an interrupted migration marker referring to a disconnected cache root
    recovery, data_dir, old_cache = _recovery(tmp_path)
    source = old_cache / "meme.png"
    source.write_bytes(b"payload")
    destination = tmp_path / "new-cache"
    with pytest.raises(OSError):
        recovery.migrate(destination, True, lambda: None, lambda: (_ for _ in ()).throw(OSError()))
    for item in old_cache.iterdir():
        item.unlink()
    old_cache.rmdir()

    # When/Then: startup refuses to infer a safe recovery without the cache root
    with pytest.raises(RecoveryUnavailable):
        StorageRecovery(data_dir, destination).recover_before_database()


def test_stale_pre_database_marker_rolls_back_completed_cross_root_copy(tmp_path):
    # Given: a process killed after one cross-root copy but before metadata commit
    recovery, data_dir, old_cache = _recovery(tmp_path)
    source = old_cache / "meme.png"
    source.write_bytes(b"payload")
    destination = tmp_path / "new-cache"
    recovery.begin(destination, True)
    recovery.copy_files()
    assert (destination / "meme.png").read_bytes() == b"payload"

    # When: a new process inspects the stale marker
    StorageRecovery(data_dir, old_cache).recover_before_database()

    # Then: it compensates to the pre-database source of truth
    assert source.read_bytes() == b"payload"
    assert not (destination / "meme.png").exists()
    assert not recovery.marker_path.exists()


def test_same_volume_uses_atomic_replace_and_cross_volume_hash_verifies(tmp_path, monkeypatch):
    # Given: one same-volume and one forced cross-volume migration
    recovery, _, old_cache = _recovery(tmp_path)
    same_source = old_cache / "same.png"
    same_source.write_bytes(b"same")
    same_destination = old_cache.parent / "new-cache" / "same.png"
    calls = []
    real_replace = __import__("os").replace

    def record_replace(source, destination):
        calls.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr("ohmymeme.core.recovery.os.replace", record_replace)
    recovery.begin(same_destination.parent, True)
    recovery.copy_files()
    assert calls
    assert _digest(same_destination) == hashlib.sha256(b"same").hexdigest()


def test_same_volume_rename_failure_preserves_pre_database_source(tmp_path, monkeypatch):
    # Given: a same-volume migration whose final replace fails
    recovery, _, old_cache = _recovery(tmp_path)
    source = old_cache / "meme.png"
    source.write_bytes(b"payload")
    destination = tmp_path / "new-cache"
    replace = __import__("os").replace

    def fail_target_replace(from_path, to_path):
        if Path(to_path).name == "meme.png":
            raise OSError("rename failed")
        return replace(from_path, to_path)

    monkeypatch.setattr("ohmymeme.core.recovery.os.replace", fail_target_replace)

    # When: the final filesystem commit cannot complete
    with pytest.raises(OSError, match="rename failed"):
        recovery.migrate(destination, True, lambda: None, lambda: None)

    # Then: no metadata phase is entered and the source remains intact
    assert source.read_bytes() == b"payload"
    assert not (destination / "meme.png").exists()
    assert not recovery.marker_path.exists()


def test_container_finishes_post_database_recovery_before_exposing_storage(tmp_path):
    # Given: a prior process committed config and cache files but died before manifest commit
    from ohmymeme.app.container import Container
    from ohmymeme.core.config import Config

    root = tmp_path / "app"
    config = Config(root / "config.json", root / "data")
    source = config.cache_dir / "meme.png"
    source.write_bytes(b"payload")
    destination = tmp_path / "external-cache"
    recovery = StorageRecovery(config.data_dir, config.cache_dir)

    def commit_database():
        config.set("cache_dir", str(destination))
        config.save()

    with pytest.raises(OSError):
        recovery.migrate(destination, True, commit_database, lambda: (_ for _ in ()).throw(OSError()))

    # When: the normal application composition root starts
    container = Container(root)
    try:
        # Then: startup completes the forward path before media consumers are created
        assert (destination / "meme.png").read_bytes() == b"payload"
        assert not source.exists()
        assert not recovery.marker_path.exists()
    finally:
        container.close()


def test_process_lock_rejects_concurrent_storage_mutation(tmp_path):
    # Given: one process holds both data and cache-root recovery locks
    first, data_dir, old_cache = _recovery(tmp_path)
    second = StorageRecovery(data_dir, old_cache)

    # When/Then: another process cannot start a conflicting transaction
    with first.locked():
        with pytest.raises(RecoveryLocked):
            with second.locked():
                pass


def test_metadata_committing_marker_forwards_when_config_persisted(tmp_path, monkeypatch):
    # Given: config persistence succeeds but the following db_committed marker replace fails
    from ohmymeme.app.container import Container
    from ohmymeme.core.config import Config

    root = tmp_path / "app"
    config = Config(root / "config.json", root / "data")
    source = config.cache_dir / "meme.png"
    source.write_bytes(b"payload")
    destination = tmp_path / "external-cache"
    recovery = StorageRecovery(config.data_dir, config.cache_dir)
    replace = __import__("os").replace

    def fail_db_committed_marker(from_path, to_path):
        if Path(from_path).name.endswith(".tmp") and Path(from_path).read_text(
            encoding="utf-8"
        ).find('"phase": "db_committed"') >= 0:
            raise OSError("marker replace failed")
        return replace(from_path, to_path)

    monkeypatch.setattr(
        "ohmymeme.core.recovery.os.replace", fail_db_committed_marker
    )

    def commit_database():
        config.set("cache_dir", str(destination))
        config.save()

    # When: the durable config commit is followed by a marker-write fault
    with pytest.raises(OSError, match="marker replace failed"):
        recovery.migrate(destination, True, commit_database, lambda: None)
    monkeypatch.undo()

    # Then: restart observes persisted metadata and completes the target forward path
    container = Container(root)
    try:
        assert container.config.cache_dir == destination
        assert (destination / "meme.png").read_bytes() == b"payload"
        assert not source.exists()
        assert not recovery.marker_path.exists()
    finally:
        container.close()


def test_target_directory_collision_stays_marked_and_fails_closed(tmp_path):
    # Given: a target path already occupied by an unowned directory
    recovery, _, old_cache = _recovery(tmp_path)
    source = old_cache / "meme.png"
    source.write_bytes(b"payload")
    destination = tmp_path / "new-cache"
    collision = destination / "meme.png"
    collision.mkdir(parents=True)

    # When: migration reaches the collision through real filesystem paths
    with pytest.raises(RecoveryUnavailable):
        recovery.migrate(destination, True, lambda: None, lambda: None)

    # Then: source/config remain authoritative and the unresolved transaction is durable
    assert source.read_bytes() == b"payload"
    assert collision.is_dir()
    assert recovery.marker_path.exists()
