import hashlib
import os
from pathlib import Path

import pytest

from ohmymeme.core.adapters.filesystem.atomic_repository import AtomicFileRepository
from ohmymeme.core.assets import AssetPaths


def _force_cross_volume(repository, monkeypatch):
    copies = []
    replaces = []
    copy_and_verify = repository._recovery._copy_and_verify
    replace = os.replace

    def record_copy(source, destination, expected_hash):
        copies.append((Path(source), Path(destination), expected_hash))
        return copy_and_verify(source, destination, expected_hash)

    def record_replace(source, destination):
        replaces.append((Path(source), Path(destination)))
        return replace(source, destination)

    monkeypatch.setattr(repository._recovery, "_same_volume", lambda *_paths: False)
    monkeypatch.setattr(repository._recovery, "_copy_and_verify", record_copy)
    monkeypatch.setattr("ohmymeme.core.recovery.os.replace", record_replace)
    return copies, replaces


def test_commit_bytes_when_target_is_empty_writes_content_addressed_file(tmp_path):
    # Given: a configured custom cache root and a payload
    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "custom-cache"
    assets = AssetPaths(data_dir, cache_dir)
    repository = AtomicFileRepository(assets)
    payload = b"atomic-payload"
    expected_hash = hashlib.sha256(payload).hexdigest()

    # When: the repository commits the content
    stored = repository.commit_bytes(payload, ".png")

    # Then: the committed name is content-addressed and no private temp survives
    assert stored.filename == f"{expected_hash[:16]}.png"
    assert stored.path.read_bytes() == payload
    assert not list(cache_dir.glob(".atomic-*"))


def test_commit_path_when_cross_root_copies_without_consuming_source(tmp_path):
    # Given: a source path outside the configured cache root
    source = tmp_path / "incoming" / "meme.webp"
    source.parent.mkdir()
    payload = b"cross-root-payload"
    source.write_bytes(payload)
    repository = AtomicFileRepository(AssetPaths(tmp_path / "data", tmp_path / "cache"))

    # When: the repository commits from the external path
    stored = repository.commit_path(source, ".webp")

    # Then: the durable cache copy survives without shortening the source lifetime
    assert source.read_bytes() == payload
    assert stored.path.read_bytes() == payload
    assert repository.locate(stored.filename) == stored.path


def test_commit_bytes_when_duplicate_exists_does_not_replace_live_file(tmp_path, monkeypatch):
    # Given: one content-addressed file already committed
    repository = AtomicFileRepository(AssetPaths(tmp_path / "data", tmp_path / "cache"))
    payload = b"duplicate-payload"
    first = repository.commit_bytes(payload, ".gif")
    replace = os.replace
    targets = []

    def record_replace(source, destination):
        targets.append(Path(destination))
        return replace(source, destination)

    monkeypatch.setattr("ohmymeme.core.adapters.filesystem.atomic_repository.os.replace", record_replace)

    # When: identical content arrives again
    second = repository.commit_bytes(payload, ".gif")

    # Then: the original live file remains the winner and no target replacement occurs
    assert second.path == first.path
    assert second.created is False
    assert first.path not in targets


@pytest.mark.parametrize("fault", ("fsync", "rename"))
def test_commit_bytes_when_filesystem_commit_fails_leaves_no_asset_or_temp(
    tmp_path, monkeypatch, fault
):
    # Given: a repository whose flush or atomic rename fails
    cache_dir = tmp_path / "cache"
    repository = AtomicFileRepository(AssetPaths(tmp_path / "data", cache_dir))
    if fault == "fsync":
        monkeypatch.setattr(
            "ohmymeme.core.adapters.filesystem.atomic_repository.os.fsync",
            lambda _descriptor: (_ for _ in ()).throw(OSError("disk full")),
        )
    else:
        monkeypatch.setattr(
            "ohmymeme.core.adapters.filesystem.atomic_repository.os.replace",
            lambda _source, _destination: (_ for _ in ()).throw(OSError("rename failed")),
        )

    # When: the repository tries to commit content
    with pytest.raises(OSError):
        repository.commit_bytes(b"failure-payload", ".png")

    # Then: neither a public asset nor a private temporary file remains
    assert not list(cache_dir.glob(".atomic-*"))
    assert not list(cache_dir.glob("*.png"))


def test_recover_when_interrupted_before_atomic_replace_removes_private_temp(tmp_path):
    # Given: a process interrupted with a private same-volume temporary file
    repository = AtomicFileRepository(AssetPaths(tmp_path / "data", tmp_path / "cache"))
    repository.assets.cache_dir.mkdir(parents=True)
    temporary = repository.assets.cache_dir / ".atomic-interrupted.tmp"
    temporary.write_bytes(b"interrupted-payload")

    # When: the next process reopens the repository
    recovered = AtomicFileRepository(repository.assets).recover()

    # Then: recovery removes the unpublished temporary file
    assert recovered == 1
    assert not temporary.exists()


def test_invalidate_thumbnails_keeps_committed_asset(tmp_path):
    # Given: a committed asset and stale thumbnails
    assets = AssetPaths(tmp_path / "data", tmp_path / "cache")
    repository = AtomicFileRepository(assets)
    committed = repository.commit_bytes(b"committed-payload", ".jpg")
    assets.thumbnail_dir.mkdir(parents=True)
    stale = assets.thumbnail_dir / "42_150.png"
    stale.write_bytes(b"stale")

    # When: the thumbnail cache is invalidated for the meme
    repository.invalidate_thumbnails(42)

    # Then: the original stays available while thumbnail cache is removed
    assert committed.path.exists()
    assert not stale.exists()


def test_cross_volume_recovery_when_interrupted_after_file_copy_rolls_back_before_metadata(
    tmp_path, monkeypatch
):
    # Given: a repository whose cross-root file phase completed before metadata
    data_dir = tmp_path / "data"
    source_root = tmp_path / "source-cache"
    target_root = tmp_path / "target-cache"
    repository = AtomicFileRepository(AssetPaths(data_dir, source_root))
    committed = repository.commit_bytes(b"copy-interruption", ".png")
    copies, replaces = _force_cross_volume(repository, monkeypatch)
    repository._recovery.begin(target_root, True)
    repository._recovery.copy_files()

    # When: a restarted repository recovers against unchanged metadata
    manifest_calls = []
    recovered = AtomicFileRepository(AssetPaths(data_dir, source_root)).recover_migration(
        source_root, lambda: manifest_calls.append("manifest")
    )

    # Then: Todo4 restores the source, clears its marker, and does not write a manifest
    assert recovered is True
    assert committed.path.exists()
    assert not (target_root / committed.filename).exists()
    assert manifest_calls == []
    assert not repository._recovery.marker_path.exists()
    assert copies == [(committed.path, target_root / committed.filename, committed.content_hash)]
    assert all(source != committed.path for source, _destination in replaces)


def test_cross_volume_recovery_when_metadata_target_persisted_forwards_before_manifest(
    tmp_path, monkeypatch
):
    # Given: file copying completed and metadata now points at the target cache root
    data_dir = tmp_path / "data"
    source_root = tmp_path / "source-cache"
    target_root = tmp_path / "target-cache"
    repository = AtomicFileRepository(AssetPaths(data_dir, source_root))
    committed = repository.commit_bytes(b"metadata-interruption", ".gif")
    copies, replaces = _force_cross_volume(repository, monkeypatch)
    repository._recovery.begin(target_root, True)
    repository._recovery.copy_files()
    repository._recovery._require_record()["phase"] = "metadata_committing"
    repository._recovery._write_record()

    # When: restart observes the committed target metadata and finishes recovery
    manifests = []
    recovered = AtomicFileRepository(AssetPaths(data_dir, source_root)).recover_migration(
        target_root, lambda: manifests.append("manifest")
    )

    # Then: the asset is authoritative at target and manifest finalization clears the marker
    assert recovered is True
    assert (target_root / committed.filename).read_bytes() == b"metadata-interruption"
    assert not committed.path.exists()
    assert manifests == ["manifest"]
    assert not repository._recovery.marker_path.exists()
    assert copies == [(committed.path, target_root / committed.filename, committed.content_hash)]
    assert all(source != committed.path for source, _destination in replaces)


def test_cross_volume_migration_when_manifest_interrupts_restarts_from_db_committed(
    tmp_path, monkeypatch
):
    # Given: metadata persisted after a repository-owned cross-root migration
    data_dir = tmp_path / "data"
    source_root = tmp_path / "source-cache"
    target_root = tmp_path / "target-cache"
    repository = AtomicFileRepository(AssetPaths(data_dir, source_root))
    committed = repository.commit_bytes(b"manifest-interruption", ".webp")
    metadata = []
    copies, replaces = _force_cross_volume(repository, monkeypatch)

    # When: the manifest phase raises after canonical DB commit
    with pytest.raises(OSError, match="manifest interrupted"):
        repository.migrate_to(
            target_root,
            lambda: metadata.append(str(target_root)),
            lambda: (_ for _ in ()).throw(OSError("manifest interrupted")),
        )

    # Then: restart retains the DB-targeted asset and completes only manifest recovery
    manifests = []
    recovered = AtomicFileRepository(AssetPaths(data_dir, source_root)).recover_migration(
        target_root, lambda: manifests.append("manifest")
    )
    assert metadata == [str(target_root)]
    assert recovered is True
    assert (target_root / committed.filename).read_bytes() == b"manifest-interruption"
    assert not committed.path.exists()
    assert manifests == ["manifest"]
    assert copies == [(committed.path, target_root / committed.filename, committed.content_hash)]
    assert all(source != committed.path for source, _destination in replaces)


def test_recover_when_todo4_marker_exists_preserves_private_temp_for_unified_recovery(tmp_path):
    # Given: a private temp beside an active Todo4 recovery marker
    data_dir = tmp_path / "data"
    source_root = tmp_path / "source-cache"
    target_root = tmp_path / "target-cache"
    repository = AtomicFileRepository(AssetPaths(data_dir, source_root))
    source_root.mkdir()
    temporary = source_root / ".atomic-pending.tmp"
    temporary.write_bytes(b"pending")
    repository._recovery.begin(target_root, False)

    # When: ordinary temporary recovery runs while the canonical transaction exists
    recovered = repository.recover()

    # Then: it leaves the transaction-owned state untouched for StorageRecovery
    assert recovered == 0
    assert temporary.exists()
    assert repository._recovery.marker_path.exists()
