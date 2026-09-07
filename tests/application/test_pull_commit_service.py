import hashlib
import io
import json

import pytest
from PIL import Image

from ohmymeme.app.manifest_service import ManifestService
from ohmymeme.app.pull_commit_service import PullCommitError, PullCommitService
from ohmymeme.core.assets import AssetPaths


def _png(color):
    output = io.BytesIO()
    Image.new("RGBA", (1, 1), color).save(output, "PNG")
    return output.getvalue()


def _projection(data):
    digest = hashlib.sha256(data).hexdigest()
    raw = json.dumps(
        {
            "version": 3,
            "memes": [
                {
                    "filename": "asset.png",
                    "name": "asset",
                    "sha256": digest,
                    "file_size": len(data),
                    "mtime": "1",
                    "sort_order": 0,
                }
            ],
            "collections": [],
        }
    ).encode()
    return ManifestService().parse_json(raw)


def _download(data):
    def download(_meme, destination):
        destination.write_bytes(data)
        return True

    return download


def test_commit_restores_existing_file_and_leaves_durable_journal_when_metadata_fails(tmp_path):
    data_dir = tmp_path / "data"
    assets = AssetPaths(data_dir, data_dir / "cache")
    assets.cache_dir.mkdir(parents=True)
    target = assets.cache_dir / "asset.png"
    target.write_bytes(b"old")
    service = PullCommitService(
        assets,
        lambda _projection: (_ for _ in ()).throw(OSError("db unavailable")),
        lambda _projection: None,
        lambda: None,
    )

    with pytest.raises(PullCommitError, match="metadata_failed"):
        service.commit(_projection(_png((255, 0, 0, 255))), _download(_png((255, 0, 0, 255))))

    assert target.read_bytes() == b"old"
    assert json.loads(service.journal_path.read_text(encoding="utf-8"))["phase"] == "needs_recovery"


def test_commit_keeps_database_and_rebuilds_manifest_on_restart_after_manifest_failure(tmp_path):
    data_dir = tmp_path / "data"
    assets = AssetPaths(data_dir, data_dir / "cache")
    writes = []
    metadata = []
    service = PullCommitService(
        assets,
        metadata.append,
        lambda _projection: (_ for _ in ()).throw(OSError("manifest replace failed")),
        lambda: writes.append("rebuilt"),
    )
    data = _png((0, 255, 0, 255))

    with pytest.raises(PullCommitError, match="manifest_failed"):
        service.commit(_projection(data), _download(data))

    assert (assets.cache_dir / "asset.png").read_bytes() == data
    assert metadata
    assert service.recover() is True
    assert writes == ["rebuilt"]
    assert not service.journal_path.exists()


def test_invalid_staging_has_no_file_or_metadata_mutation(tmp_path):
    assets = AssetPaths(tmp_path / "data", tmp_path / "cache")
    metadata = []
    writes = []
    service = PullCommitService(assets, metadata.append, writes.append, lambda: None)
    projection = _projection(_png((0, 0, 255, 255)))

    with pytest.raises(PullCommitError, match="invalid_staged_asset"):
        service.commit(projection, _download(b"not an image"))

    assert metadata == []
    assert writes == []
    assert not list(assets.cache_dir.glob(".pull-stage-*.tmp"))
    assert not service.journal_path.exists()


def test_mismatched_remote_hash_has_no_file_or_metadata_mutation(tmp_path):
    # Given: a staged image declared with a different canonical remote digest
    assets = AssetPaths(tmp_path / "data", tmp_path / "cache")
    data = _png((0, 0, 255, 255))
    metadata = []
    writes = []
    service = PullCommitService(assets, metadata.append, writes.append, lambda: None)
    projection = ManifestService().parse_data(
        {
            "version": 3,
            "memes": [
                {
                    "filename": "asset.png",
                    "name": "asset",
                    "sha256": "a" * 64,
                    "file_size": len(data),
                    "mtime": "1",
                    "sort_order": 0,
                }
            ],
            "collections": [],
        }
    )

    # When: staged byte verification compares actual content to that digest
    with pytest.raises(PullCommitError, match="invalid_staged_asset"):
        service.commit(projection, _download(data))

    # Then: no target, metadata, manifest, or journal is created
    assert not (assets.cache_dir / "asset.png").exists()
    assert metadata == []
    assert writes == []
    assert not service.journal_path.exists()


def test_cancel_at_pull_precommit_boundary_leaves_no_ghosts(tmp_path):
    # Given: a fully valid staged pull and a cancellation at the documented pre-commit boundary
    assets = AssetPaths(tmp_path / "data", tmp_path / "cache")
    data = _png((0, 0, 255, 255))
    metadata = []
    writes = []
    service = PullCommitService(assets, metadata.append, writes.append, lambda: None)

    # When: cancellation is observed after validation but before journal/file replacement
    with pytest.raises(PullCommitError, match="cancelled"):
        service.commit(_projection(data), _download(data), cancelled=lambda: True)

    # Then: every durable state remains untouched and staging is removed
    assert metadata == []
    assert writes == []
    assert not (assets.cache_dir / "asset.png").exists()
    assert not list(assets.cache_dir.glob(".pull-stage-*.tmp"))
    assert not service.journal_path.exists()


def test_cleanup_failure_retains_only_a_recoverable_cleanup_marker(tmp_path, monkeypatch):
    assets = AssetPaths(tmp_path / "data", tmp_path / "cache")
    data = _png((20, 30, 40, 255))
    service = PullCommitService(assets, lambda _projection: None, lambda _projection: None, lambda: None)
    monkeypatch.setattr(service, "_cleanup", lambda _record: (_ for _ in ()).throw(OSError("cleanup")))

    result = service.commit(_projection(data), _download(data))

    assert result == {"ok": True, "cleanup_pending": True}
    assert json.loads(service.journal_path.read_text(encoding="utf-8"))["phase"] == "cleanup_pending"


def test_corrupt_journal_fails_closed_without_deleting_cache(tmp_path):
    assets = AssetPaths(tmp_path / "data", tmp_path / "cache")
    assets.cache_dir.mkdir(parents=True)
    target = assets.cache_dir / "keep.png"
    target.write_bytes(b"keep")
    service = PullCommitService(assets, lambda _projection: None, lambda _projection: None, lambda: None)
    service.journal_path.parent.mkdir(parents=True)
    service.journal_path.write_text("{", encoding="utf-8")

    with pytest.raises(PullCommitError, match="invalid_journal"):
        service.recover()

    assert target.read_bytes() == b"keep"
    assert service.journal_path.exists()


def test_forged_journal_stage_cannot_delete_unrelated_cache_file(tmp_path):
    assets = AssetPaths(tmp_path / "data", tmp_path / "cache")
    assets.cache_dir.mkdir(parents=True)
    keep = assets.cache_dir / "keep.png"
    keep.write_bytes(b"keep")
    service = PullCommitService(assets, lambda _projection: None, lambda _projection: None, lambda: None)
    service.journal_path.parent.mkdir(parents=True)
    service.journal_path.write_text(
        json.dumps({"version": 1, "phase": "staged", "entries": [{"filename": "asset.png", "stage": "keep.png", "backup": "", "had_original": False}]}),
        encoding="utf-8",
    )

    with pytest.raises(PullCommitError, match="invalid_journal"):
        service.recover()

    assert keep.read_bytes() == b"keep"
