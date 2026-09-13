# pyright: basic

import inspect
import io
import os
import threading

import pytest
from PIL import Image

from ohmymeme.core.assets import AssetPaths
from ohmymeme.core.imports import (
    HostImportSink,
    ImageImportService,
    ImportBytes,
    ImportPath,
)


def _png_bytes(width=1, height=1):
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), (255, 0, 0, 255)).save(buffer, "PNG")
    return buffer.getvalue()


class FakeDb:
    def __init__(self, fail_add_at=None, fail_delete=False):
        self.rows = {}
        self.fail_add_at = fail_add_at
        self.fail_delete = fail_delete
        self.add_calls = 0

    def get_by_hash(self, file_hash):
        return self.rows.get(file_hash)

    def add_meme(self, **row):
        self.add_calls += 1
        if self.add_calls == self.fail_add_at:
            raise OSError("database write failed")
        meme_id = self.add_calls
        self.rows[row["file_hash"]] = {"id": meme_id, **row}
        return meme_id

    def delete_meme(self, meme_id):
        if self.fail_delete:
            raise OSError("database rollback failed")
        for file_hash, row in tuple(self.rows.items()):
            if row["id"] == meme_id:
                del self.rows[file_hash]
                return


def _service(tmp_path, db=None, fail_manifest=False):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    assets = AssetPaths(tmp_path, cache_dir)

    def build_manifest():
        if fail_manifest:
            raise OSError("manifest replace failed")

    return ImageImportService(db or FakeDb(), assets, build_manifest), cache_dir


def test_import_bytes_creates_validated_file_row_and_manifest(tmp_path):
    # Given: valid image bytes and an empty cache/database
    service, cache_dir = _service(tmp_path)

    # When: importing bytes through the application service
    result = service.import_bytes(ImportBytes(_png_bytes(), "example.png"))

    # Then: one content-addressed file and metadata row are committed
    assert result.imported_ids == (1,)
    assert result.rejected == 0
    assert len(list(cache_dir.iterdir())) == 1


def test_import_bytes_returns_duplicate_without_mutation(tmp_path):
    # Given: an image already committed by its content hash
    db = FakeDb()
    service, cache_dir = _service(tmp_path, db)
    first = service.import_bytes(ImportBytes(_png_bytes(), "one.png"))

    # When: importing the same image under another name
    result = service.import_bytes(ImportBytes(_png_bytes(), "two.png"))

    # Then: no second file or row is created
    assert first.imported_ids == (1,)
    assert result.imported_ids == ()
    assert result.rejected == 0
    assert len(db.rows) == 1
    assert len(list(cache_dir.iterdir())) == 1


def test_import_bytes_rejects_oversize_and_corrupt_payloads_without_orphans(tmp_path):
    # Given: an oversize payload and a PNG magic prefix with corrupt contents
    service, cache_dir = _service(tmp_path)
    oversize = b"x" * (20 * 1024 * 1024 + 1)
    corrupt = b"\x89PNG\r\n\x1a\nnot-an-image"

    # When: both payloads cross the validation boundary
    result = service.import_batch(
        (ImportBytes(oversize, "big.png"), ImportBytes(corrupt, "evil.png"))
    )

    # Then: validation rejects them before any file or database mutation
    assert result.imported_ids == ()
    assert result.rejected == 2
    assert not list(cache_dir.iterdir())


def test_import_bytes_rejects_excessive_dimensions_without_orphans(tmp_path):
    # Given: a valid PNG whose longest edge exceeds the import contract
    service, cache_dir = _service(tmp_path)

    # When: importing the oversized dimensions
    result = service.import_bytes(ImportBytes(_png_bytes(2561, 1), "wide.png"))

    # Then: it is rejected before cache or metadata writes
    assert result.imported_ids == ()
    assert result.rejected == 1
    assert not list(cache_dir.iterdir())


def test_import_path_uses_magic_not_malicious_filename(tmp_path):
    # Given: a valid image disguised by a traversal-like display name
    source = tmp_path / "source.bin"
    source.write_bytes(_png_bytes())
    service, cache_dir = _service(tmp_path)

    # When: the path import receives the untrusted display name
    result = service.import_path(ImportPath(source, "../malicious.png"))

    # Then: the cache only receives a content-addressed magic-derived filename
    assert result.imported_ids == (1,)
    assert all(".." not in item.name for item in cache_dir.iterdir())


@pytest.mark.parametrize("fault", ("file", "db", "manifest", "batch-db"))
def test_import_transaction_faults_compensate_files_and_rows(
    tmp_path, fault, monkeypatch
):
    # Given: a write, manifest, or second batch database fault
    fail_add_at = 2 if fault == "batch-db" else (1 if fault == "db" else None)
    db = FakeDb(fail_add_at=fail_add_at)
    service, cache_dir = _service(tmp_path, db, fail_manifest=fault == "manifest")
    request = (
        ImportBytes(_png_bytes(), "one.png"),
        ImportBytes(_png_bytes(2, 1), "two.png"),
    )
    if fault == "file":

            def fail_install(*_):
                raise OSError

            monkeypatch.setattr(service._files, "commit_bytes", fail_install)

    # When: the service attempts one atomic batch
    with pytest.raises(OSError):
        service.import_batch(request)

    # Then: compensation leaves neither cache files nor metadata ghosts
    assert not list(cache_dir.iterdir())
    assert db.rows == {}


def test_import_path_restores_stg3_payload_without_storing_carrier(tmp_path):
    # Given: a GIF carrier and an injected decoder outputting a PNG payload
    carrier = tmp_path / "carrier.gif"
    carrier_data = io.BytesIO()
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(carrier_data, "GIF")
    carrier.write_bytes(carrier_data.getvalue() + b"STG3")
    restored = _png_bytes()
    db = FakeDb()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    service = ImageImportService(
        db, AssetPaths(tmp_path, cache_dir), lambda: None, lambda _: restored
    )

    # When: importing the carrier through the path entrypoint
    result = service.import_path(ImportPath(carrier, "carrier.gif"))

    # Then: only the restored image is persisted and marked as stego-derived
    row = next(iter(db.rows.values()))
    assert result.imported_ids == (1,)
    assert row["from_stego"] == 1
    assert list(cache_dir.iterdir())[0].suffix == ".png"


def test_import_bytes_restores_stg3_payload_without_storing_carrier(tmp_path):
    # Given: a valid GIF carrier with STG3 data and an in-memory decoder outputting PNG bytes
    carrier = io.BytesIO()
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(carrier, "GIF")
    restored = _png_bytes()
    db = FakeDb()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    service = ImageImportService(
        db,
        AssetPaths(tmp_path, cache_dir),
        lambda: None,
        lambda _data: restored,
    )

    # When: a byte-oriented receiver imports the STG3 carrier
    result = service.import_bytes(ImportBytes(carrier.getvalue() + b"STG3", "carrier.gif"))

    # Then: it follows the same restore path and never persists the carrier
    row = next(iter(db.rows.values()))
    assert result.imported_ids == (1,)
    assert row["from_stego"] == 1
    assert list(cache_dir.iterdir())[0].suffix == ".png"


def test_import_bytes_uses_default_stg3_decoder(tmp_path):
    # Given: a real STG3 GIF generated from a source PNG
    from ohmymeme.core.gif_stego import make_stego_gif

    original = tmp_path / "original.png"
    original.write_bytes(_png_bytes())
    carrier = tmp_path / "carrier.gif"
    make_stego_gif(str(original), str(carrier), quiet=True)
    db = FakeDb()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    service = ImageImportService(db, AssetPaths(tmp_path, cache_dir), lambda: None)

    # When: a receiver without presentation-specific wiring imports its bytes
    result = service.import_bytes(ImportBytes(carrier.read_bytes(), "carrier.gif"))

    # Then: the canonical default decoder persists only the restored image
    row = next(iter(db.rows.values()))
    assert result.imported_ids == (1,)
    assert row["from_stego"] == 1
    assert list(cache_dir.iterdir())[0].suffix == ".png"


def test_import_bytes_rejects_malformed_stg3_without_persisting_carrier(tmp_path):
    # Given: a valid GIF with a malformed STG3 payload
    carrier = io.BytesIO()
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(carrier, "GIF")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    observed = []

    def reject_stego(data):
        observed.append((data, tuple(cache_dir.iterdir())))
        return None

    service = ImageImportService(
        FakeDb(), AssetPaths(tmp_path, cache_dir), lambda: None, reject_stego
    )

    # When: an untrusted byte receiver submits the malformed carrier
    result = service.import_bytes(ImportBytes(carrier.getvalue() + b"STG3", "bad.gif"))

    # Then: the carrier never falls through as an ordinary GIF asset
    assert result.imported_ids == ()
    assert result.rejected == 1
    assert observed == [(carrier.getvalue() + b"STG3", ())]
    assert not list(cache_dir.iterdir())


def test_cancel_before_import_commit_leaves_no_file_row_or_manifest(tmp_path):
    # Given: a valid request and a cancellation at the shared pre-commit boundary
    db = FakeDb()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    manifest = tmp_path / "meme-index.json"
    manifest.write_text("before", encoding="utf-8")
    service = ImageImportService(db, AssetPaths(tmp_path, cache_dir), lambda: None)

    # When: the batch observes cancellation after validating its input
    result = service.import_batch(
        (ImportBytes(_png_bytes(), "cancelled.png"),), cancelled=lambda: True
    )

    # Then: the safe boundary leaves every durable import state unchanged
    assert result.imported_ids == ()
    assert result.rejected == 0
    assert db.rows == {}
    assert not list(cache_dir.iterdir())
    assert manifest.read_text(encoding="utf-8") == "before"


def test_register_existing_path_uses_the_same_manifest_transaction(tmp_path):
    # Given: a cache-resident valid image and a manifest callback
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    source = cache_dir / "existing.png"
    source.write_bytes(_png_bytes())
    calls = []
    service = ImageImportService(FakeDb(), AssetPaths(tmp_path, cache_dir), lambda: calls.append(True))

    # When: cache scanning uses its legacy registration entrypoint
    result = service.register_existing_path(ImportPath(source, "existing.png"))

    # Then: metadata and manifest commit through the normal path transaction
    assert result.imported_ids == (1,)
    assert calls == [True]


def test_isolated_path_bytes_and_batch_import_scenario(tmp_path):
    # Given: isolated path and byte sources with a shared import service
    source = tmp_path / "path-source.png"
    source.write_bytes(_png_bytes(2, 1))
    service, cache_dir = _service(tmp_path)

    # When: bytes, path, and a mixed batch enter the same boundary
    byte_result = service.import_bytes(ImportBytes(_png_bytes(), "bytes.png"))
    path_result = service.import_path(ImportPath(source, "path.png"))
    batch_result = service.import_batch(
        (
            ImportBytes(_png_bytes(3, 1), "batch-bytes.png"),
            ImportPath(source, "batch-duplicate.png"),
        )
    )

    # Then: each unique image is atomically represented once and the source remains
    assert byte_result.imported_ids == (1,)
    assert path_result.imported_ids == (2,)
    assert batch_result.imported_ids == (3,)
    assert batch_result.rejected == 0
    assert source.exists()
    assert len(list(cache_dir.iterdir())) == 3


def test_manifest_failure_restores_previous_snapshot(tmp_path):
    # Given: a pre-existing manifest and a failing replacement operation
    db = FakeDb()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    manifest = tmp_path / "meme-index.json"
    manifest.write_text("old", encoding="utf-8")

    def fail_manifest():
        manifest.write_text("new", encoding="utf-8")
        raise OSError("replace failed")

    service = ImageImportService(db, AssetPaths(tmp_path, cache_dir), fail_manifest)

    # When: the manifest update fails after a successful image insert
    with pytest.raises(OSError):
        service.import_bytes(ImportBytes(_png_bytes(), "example.png"))

    # Then: the prior manifest, cache and database state are recovered
    assert manifest.read_text(encoding="utf-8") == "old"
    assert not list(cache_dir.iterdir())
    assert db.rows == {}


def test_existing_corrupt_content_addressed_target_fails_closed(tmp_path):
    # Given: the target hash name exists but holds other bytes
    service, cache_dir = _service(tmp_path)
    payload = _png_bytes()
    digest = __import__("hashlib").sha256(payload).hexdigest()
    (cache_dir / f"{digest[:16]}.png").write_bytes(b"wrong")

    # When: importing the matching payload
    with pytest.raises(OSError):
        service.import_bytes(ImportBytes(payload, "example.png"))

    # Then: corrupt existing content is never trusted or registered
    assert not service._db.rows


def test_concurrent_duplicate_imports_create_one_row(tmp_path):
    # Given: 24 callers sharing the same service and content
    service, cache_dir = _service(tmp_path)
    results = []
    barrier = threading.Barrier(24)

    def import_same():
        barrier.wait()
        results.append(service.import_bytes(ImportBytes(_png_bytes(), "same.png")))

    workers = [threading.Thread(target=import_same) for _ in range(24)]

    # When: all callers import concurrently
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    # Then: content-addressed dedup has one durable winner
    assert sum(bool(result.imported_ids) for result in results) == 1
    assert len(service._db.rows) == 1
    assert len(list(cache_dir.iterdir())) == 1


def test_manifest_restore_failure_writes_recovery_marker(tmp_path, monkeypatch):
    # Given: a manifest failure followed by a failed snapshot restore
    service, cache_dir = _service(tmp_path, fail_manifest=True)
    manifest = cache_dir.parent / "meme-index.json"
    manifest.write_text("old", encoding="utf-8")
    replace = os.replace

    def fail_manifest_restore(source, destination):
        if str(source).endswith(".restore.tmp"):
            raise OSError("restore")
        return replace(source, destination)

    monkeypatch.setattr("ohmymeme.core.imports.os.replace", fail_manifest_restore)

    # When: the import fails after mutation
    with pytest.raises(OSError):
        service.import_bytes(ImportBytes(_png_bytes(), "example.png"))

    # Then: recovery is durably marked rather than silently losing cleanup state
    assert (cache_dir.parent / ".import-recovery.json").exists()


def test_public_import_method_signatures_remain_frozen():
    # Given: the established bridge and service methods
    from ohmymeme.services.lan.commands import _import_bytes
    from ohmymeme.services.sync.service import _pull_worker
    from ohmymeme.presentation.desktop.window_manager import JsApi

    # When: inspecting their public callable contracts
    signatures = {
        "import_memes": str(inspect.signature(JsApi.import_memes)),
        "import_folder": str(inspect.signature(JsApi.import_folder)),
        "clipboard": str(inspect.signature(JsApi.import_from_clipboard)),
        "lan": str(inspect.signature(_import_bytes)),
        "sync": str(inspect.signature(_pull_worker)),
    }

    # Then: bridge defaults and annotations stay ABI-compatible
    assert signatures == {
        "import_memes": "(self) -> dict",
        "import_folder": "(self, make_collection=True) -> dict",
        "clipboard": "(self) -> dict",
        "lan": "(data: bytes, filename: str) -> dict",
        "sync": "(entries, remote_root, cache_dir, db)",
    }


def test_recovery_marker_failure_preserves_primary_import_error(tmp_path, monkeypatch):
    # Given: manifest recovery and marker persistence both fail
    service, _ = _service(tmp_path, fail_manifest=True)
    monkeypatch.setattr(service, "_restore_manifest", lambda *_: ["restore"])

    def fail_marker(*_):
        raise OSError("marker")

    monkeypatch.setattr(service, "_write_recovery_marker", fail_marker)

    # When: the primary manifest mutation raises
    with pytest.raises(OSError, match="manifest replace failed"):
        service.import_bytes(ImportBytes(_png_bytes(), "example.png"))


def test_external_cache_keeps_manifest_marker_and_cleanup_in_data_dir(
    tmp_path, monkeypatch
):
    # Given: a custom cache root separated from canonical application data.
    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "external-cache"
    data_dir.mkdir()
    cache_dir.mkdir()
    assets = AssetPaths(data_dir, cache_dir)
    assets.manifest_path.write_text("old", encoding="utf-8")
    db = FakeDb()

    def fail_manifest():
        assets.manifest_path.write_text("new", encoding="utf-8")
        raise OSError("manifest replace failed")

    service = ImageImportService(db, assets, fail_manifest)

    # When: manifest recovery fails after the primary import error.
    replace = os.replace

    def fail_manifest_restore(source, destination):
        if str(source).endswith(".restore.tmp"):
            raise OSError("restore")
        return replace(source, destination)

    monkeypatch.setattr("ohmymeme.core.imports.os.replace", fail_manifest_restore)
    with pytest.raises(OSError, match="manifest replace failed"):
        service.import_bytes(ImportBytes(_png_bytes(), "example.png"))

    # Then: the custom cache has no image orphan and canonical artifacts stay in data.
    assert not list(cache_dir.iterdir())
    assert db.rows == {}
    assert assets.manifest_path.parent == data_dir
    assert assets.recovery_marker_path.parent == data_dir
    assert assets.recovery_marker_path.exists()


def test_container_recovers_import_rollback_marker_before_exposing_catalog(tmp_path):
    # Given: a real import whose unfinished rollback is durably marked
    from ohmymeme.app.container import Container

    root = tmp_path / "app"
    container = Container(root)
    service = ImageImportService(
        container.db, container.assets, container.build_manifest
    )
    result = service.import_bytes(ImportBytes(_png_bytes(), "rollback.png"))
    meme_id = result.imported_ids[0]
    row = container.db.get_by_id(meme_id)
    asset = container.assets.cache_dir / row["filename"]
    service._write_recovery_marker([meme_id], [asset], b'{"before":true}', ["fault"])
    container.close()

    # When: the canonical Container startup path consumes the rollback marker
    recovered = Container(root)
    try:
        # Then: file, DB row, manifest and marker return to their pre-import state
        assert recovered.db.get_by_id(meme_id) is None
        assert not asset.exists()
        assert recovered.assets.manifest_path.read_bytes() == b'{"before":true}'
        assert not recovered.assets.recovery_marker_path.exists()
    finally:
        recovered.close()


def test_container_consumes_legacy_import_marker_by_rebuilding_manifest(tmp_path):
    # Given: a legacy text marker from the pre-transactional recovery format
    from ohmymeme.app.container import Container

    root = tmp_path / "app"
    container = Container(root)
    container.assets.recovery_marker_path.write_text("manifest_restore: old", encoding="utf-8")
    container.close()

    # When: startup consumes the legacy recovery record
    recovered = Container(root)
    try:
        # Then: the marker is consumed only after a manifest rebuild
        assert recovered.assets.manifest_path.exists()
        assert not recovered.assets.recovery_marker_path.exists()
    finally:
        recovered.close()


def test_truncated_import_journal_fails_closed_and_remains_for_recovery(tmp_path):
    # Given: a truncated version-two journal beside otherwise valid durable state
    db = FakeDb()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    assets = AssetPaths(tmp_path, cache_dir)
    assets.manifest_path.write_text("before", encoding="utf-8")
    assets.recovery_marker_path.write_text('{"version": 2,', encoding="utf-8")
    rebuilt = []
    service = ImageImportService(db, assets, lambda: rebuilt.append(True))

    # When: startup attempts recovery
    with pytest.raises(RuntimeError, match="invalid_import_recovery_marker"):
        service.recover()

    # Then: corrupt data is preserved for recovery without any durable mutation
    assert assets.recovery_marker_path.exists()
    assert assets.manifest_path.read_text(encoding="utf-8") == "before"
    assert rebuilt == []
    assert db.rows == {}


def test_invalid_journal_snapshot_fails_before_rollback_mutates_state(tmp_path):
    # Given: a rollback journal with valid entries but an invalid base64 snapshot
    db = FakeDb()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    assets = AssetPaths(tmp_path, cache_dir)
    payload = _png_bytes()
    file_hash = __import__("hashlib").sha256(payload).hexdigest()
    filename = f"{file_hash[:16]}.png"
    asset = cache_dir / filename
    asset.write_bytes(payload)
    meme_id = db.add_meme(
        filename=filename,
        file_hash=file_hash,
        width=1,
        height=1,
        file_size=len(payload),
        mime_type="image/png",
        original_name="existing",
    )
    assets.manifest_path.write_text("before", encoding="utf-8")
    assets.recovery_marker_path.write_text(
        __import__("json").dumps(
            {
                "cache_dir": str(cache_dir.resolve()),
                "data_dir": str(assets.data_dir.resolve()),
                "entries": [
                    {
                        "created": True,
                        "file_hash": file_hash,
                        "filename": filename,
                        "had_target": False,
                        "meme_id": meme_id,
                        "state": "replaced",
                    }
                ],
                "manifest_snapshot": "%%%",
                "phase": "metadata_committing",
                "version": 2,
            }
        ),
        encoding="utf-8",
    )
    rebuilt = []
    service = ImageImportService(db, assets, lambda: rebuilt.append(True))

    # When: startup validates the journal before applying its rollback
    with pytest.raises(RuntimeError, match="invalid_import_recovery_marker"):
        service.recover()

    # Then: invalid snapshot data blocks every row, file, manifest, and journal mutation
    assert assets.recovery_marker_path.exists()
    assert file_hash in db.rows
    assert asset.read_bytes() == payload
    assert assets.manifest_path.read_text(encoding="utf-8") == "before"
    assert rebuilt == []


def test_container_completes_import_forward_marker_before_exposing_catalog(tmp_path):
    # Given: real committed file/DB state and a durable forward manifest marker
    from ohmymeme.app.container import Container
    import json

    root = tmp_path / "app"
    container = Container(root)
    service = ImageImportService(
        container.db, container.assets, container.build_manifest
    )
    result = service.import_bytes(ImportBytes(_png_bytes(), "forward.png"))
    meme_id = result.imported_ids[0]
    row = container.db.get_by_id(meme_id)
    container.assets.manifest_path.write_text("stale", encoding="utf-8")
    container.assets.recovery_marker_path.write_text(
        json.dumps(
            {
                "version": 1,
                "phase": "forward",
                "data_dir": str(container.assets.data_dir.resolve()),
                "cache_dir": str(container.assets.cache_dir.resolve()),
                "meme_ids": [meme_id],
                "filenames": [row["filename"]],
                "manifest_snapshot": None,
                "failures": ["manifest"],
            }
        ),
        encoding="utf-8",
    )
    container.close()

    # When: the canonical Container startup path consumes the forward marker
    recovered = Container(root)
    try:
        # Then: committed DB/file state is retained and the manifest is rebuilt
        assert recovered.db.get_by_id(meme_id) is not None
        assert (recovered.assets.cache_dir / row["filename"]).exists()
        assert row["filename"].encode("utf-8") in recovered.assets.manifest_path.read_bytes()
        assert not recovered.assets.recovery_marker_path.exists()
    finally:
        recovered.close()


def test_bounded_upload_body_rejects_missing_and_lying_content_length():
    # Given: streams larger than the upload limit with untrusted length metadata
    from ohmymeme.presentation.desktop.window_manager import _read_upload_body

    oversized = b"x" * (28 * 1024 * 1024 + 1)

    # When/Then: both no-header and lying-header streams stop at limit + 1
    assert _read_upload_body(io.BytesIO(oversized)) is None
    assert _read_upload_body(io.BytesIO(oversized)) is None


def test_host_import_sink_routes_path_and_bytes_through_precommit_boundary(tmp_path):
    # Given: a host-owned sink backed by one image import service
    source = tmp_path / "source.png"
    source.write_bytes(_png_bytes(2, 1))
    service, cache_dir = _service(tmp_path)
    sink = HostImportSink(service)

    # When: a provider submits a cancelled path then a byte payload through the sink
    cancelled = sink.import_path(
        ImportPath(source, "source.png"), cancelled=lambda: True
    )
    committed = sink.import_bytes(ImportBytes(_png_bytes(), "bytes.png"))

    # Then: cancellation has no durable effect and bytes use the host transaction
    assert cancelled.imported_ids == ()
    assert committed.imported_ids == (1,)
    assert len(service._db.rows) == 1
    assert len(tuple(cache_dir.iterdir())) == 1


def test_host_import_sink_forwards_manifest_after_postcommit_crash(tmp_path):
    # Given: a manifest callback that crashes after the image and metadata commit
    class Crash(BaseException):
        pass

    db = FakeDb()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    assets = AssetPaths(tmp_path, cache_dir)
    sink = HostImportSink(
        ImageImportService(
            db,
            assets,
            lambda: (_ for _ in ()).throw(Crash()),
        )
    )

    # When: the host process dies during manifest finalization and a fresh service starts
    with pytest.raises(Crash):
        sink.import_bytes(ImportBytes(_png_bytes(), "crash.png"))
    marker = __import__("json").loads(assets.recovery_marker_path.read_text())
    rebuilt = []
    recovered = ImageImportService(db, assets, lambda: rebuilt.append(True))
    result = recovered.recover()

    # Then: recovery keeps committed media/metadata, rebuilds manifest, and clears journal
    assert marker["phase"] == "manifest_committing"
    assert result is True
    assert rebuilt == [True]
    assert len(db.rows) == 1
    assert len(tuple(cache_dir.iterdir())) == 1
    assert not assets.recovery_marker_path.exists()
