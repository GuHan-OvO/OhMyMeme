"""Run Todo 15 import scenarios and emit one canonical execution record."""

import argparse
import hashlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path
from secrets import token_urlsafe

from PIL import Image

from ohmymeme.core.assets import AssetPaths
from ohmymeme.core.imports import ImageImportService, ImportBytes, ImportPath
from ohmymeme.app.remote_mutation_coordinator import RemoteMutationCoordinator
from ohmymeme.integrations.imports import qqnt
from ohmymeme.presentation.desktop import window_manager
from ohmymeme.services.sync import service as sync_service
from scripts.baseline_contracts import canonical_bytes


SCENARIOS = (
    "bytes-path-batch",
    "malformed-stg3",
    "cancelled",
    "qqnt-cache",
    "sync-legacy-worker",
    "sync-production-cancel",
)


class _Db:
    def __init__(self):
        self.rows = {}
        self._next_id = 1

    def get_by_hash(self, file_hash):
        return self.rows.get(file_hash)

    def add_meme(self, **row):
        meme_id = self._next_id
        self._next_id += 1
        self.rows[row["file_hash"]] = {"id": meme_id, **row}
        return meme_id

    def delete_meme(self, meme_id):
        for file_hash, row in tuple(self.rows.items()):
            if row["id"] == meme_id:
                del self.rows[file_hash]


class _SyncBackend:
    def __init__(self, data):
        self.data = data
        self.downloaded = []

    def connect(self):
        return None

    def close(self):
        return None

    def download_file(self, _remote_path, destination):
        self.downloaded.append(destination)
        Path(destination).write_bytes(self.data)
        return True


class _SyncConfig:
    def __init__(self, root):
        self.data_dir = root
        self.cache_dir = root / "cache"

    def get(self, _key, default=None):
        return default


def _png(width=1):
    output = io.BytesIO()
    Image.new("RGBA", (width, 1), (255, 0, 0, 255)).save(output, "PNG")
    return output.getvalue()


def _observe(root, db, assets):
    return {
        "cache": sorted(path.name for path in assets.cache_dir.iterdir()),
        "database_hashes": sorted(db.rows),
        "manifest_sha256": (
            hashlib.sha256(assets.manifest_path.read_bytes()).hexdigest()
            if assets.manifest_path.exists()
            else None
        ),
        "marker": assets.recovery_marker_path.exists(),
    }


def _scenario(run_id, name, evidence_root):
    root = Path(tempfile.mkdtemp(prefix="task-15-", dir=evidence_root))
    row = None
    try:
        cache = root / "cache"
        cache.mkdir()
        assets = AssetPaths(root, cache)
        db = _Db()
        service = ImageImportService(db, assets, lambda: assets.manifest_path.write_text("manifest", encoding="utf-8"))
        if name == "bytes-path-batch":
            source = root / "source.png"
            source.write_bytes(_png(2))
            results = (
                service.import_bytes(ImportBytes(_png(), "bytes.png")),
                service.import_path(ImportPath(source, "path.png")),
                service.import_batch((ImportBytes(_png(3), "batch.png"),)),
            )
            assertions = [
                all(result.imported_ids for result in results),
                len(db.rows) == 3,
                source.exists(),
            ]
        elif name == "malformed-stg3":
            carrier = io.BytesIO()
            Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(carrier, "GIF")
            result = service.import_bytes(ImportBytes(carrier.getvalue() + b"STG3", "bad.gif"))
            assertions = [result.rejected == 1, not db.rows, not list(cache.iterdir())]
        elif name == "cancelled":
            result = service.import_batch(
                (ImportBytes(_png(), "cancelled.png"),), cancelled=lambda: True
            )
            assertions = [not result.imported_ids, not db.rows, not list(cache.iterdir())]
        elif name == "qqnt-cache":
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

            def library_import(paths):
                result = service.import_batch(
                    tuple(ImportPath(Path(path), Path(path).stem) for path in paths)
                )
                return {"ids": list(result.imported_ids), "rejected": result.rejected}

            window_manager._QQNT_CANCEL = False
            window_manager._qqnt_worker(
                "10001",
                str(cache),
                False,
                False,
                None,
                str(source.parents[5]),
                str(cache),
                library_import,
            )
            result = window_manager.get_qqnt_progress()["result"]
            assertions = [
                result["copied"] == 1,
                result["skipped"] == 1,
                len(db.rows) == 1,
                len(list(cache.iterdir())) == 1,
            ]
        elif name == "sync-legacy-worker":
            backend = _SyncBackend(_png())
            original_backend = sync_service._get_backend
            original_increment = sync_service._increment_sync_progress
            sync_service._get_backend = lambda: backend
            sync_service._increment_sync_progress = lambda **_changes: None
            coordinator = RemoteMutationCoordinator(root / "coordinator")
            try:
                with coordinator.mutation("task-15.worker") as lease:
                    with sync_service._bind_legacy_runtime(
                        coordinator, lease, config=_SyncConfig(root), database=db
                    ):
                        result = sync_service._pull_worker(
                            [("missing.png", {"file_size": len(_png())})],
                            "/",
                            cache,
                            db,
                        )
                        lease.commit()
            finally:
                coordinator.close()
                sync_service._get_backend = original_backend
                sync_service._increment_sync_progress = original_increment
            assertions = [
                result["errors"] == 1,
                not backend.downloaded,
                not db.rows,
                not list(cache.iterdir()),
            ]
        else:
            config = _SyncConfig(root)
            backend = _SyncBackend(_png())
            digest = hashlib.sha256(_png()).hexdigest()
            remote_data = {
                "version": 3,
                "memes": [
                    {
                        "filename": "remote.png",
                        "name": "remote",
                        "sha256": digest,
                        "file_size": len(_png()),
                        "mtime": "",
                        "sort_order": 0,
                    }
                ],
                "collections": [],
            }
            originals = {
                "get_config": sync_service.get_config,
                "get_db": sync_service.get_db,
                "load_manifest": sync_service.load_manifest,
                "download_index": sync_service.download_index,
                "_get_backend": sync_service._get_backend,
                "_remote_root": sync_service._remote_root,
                "_reset_sync_state": sync_service._reset_sync_state,
                "_update_sync_state": sync_service._update_sync_state,
            }
            sync_service.get_config = lambda: config
            sync_service.get_db = lambda: db
            sync_service.load_manifest = lambda: {"version": 3, "memes": []}
            sync_service.download_index = lambda: remote_data
            sync_service._get_backend = lambda: backend
            sync_service._remote_root = lambda _config: "/"
            sync_service._reset_sync_state = lambda *_args: None
            sync_service._update_sync_state = lambda **_changes: None
            try:
                result = sync_service.pull(cancelled=lambda: True)
            finally:
                for symbol, original in originals.items():
                    setattr(sync_service, symbol, original)
            assertions = [
                result["cancelled"] is True,
                not db.rows,
                not (cache / "remote.png").exists(),
                not (root / ".ohmymeme-pull-recovery.json").exists(),
            ]
        if not all(assertions):
            raise RuntimeError("scenario assertion failed")
        row = {
            "execution_identity": {"run_id": run_id, "row_id": token_urlsafe(18)},
            "executed": True,
            "assertions": assertions,
            "observed": _observe(root, db, assets),
            "cleanup": {"temporary_root_removed": False},
        }
        return row
    finally:
        shutil.rmtree(root)
        if row is not None:
            row["cleanup"]["temporary_root_removed"] = not root.exists()


def run_matrix(evidence_root):
    run_id = token_urlsafe(24)
    rows = {}
    for name in SCENARIOS:
        rows[name] = _scenario(run_id, name, evidence_root)
    return {"execution_identity": run_id, "scenarios": rows}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", required=True)
    args = parser.parse_args(argv)
    evidence_root = Path(args.evidence_root).resolve()
    if not evidence_root.is_dir():
        return 2
    sys.stdout.write(canonical_bytes(run_matrix(evidence_root)).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
