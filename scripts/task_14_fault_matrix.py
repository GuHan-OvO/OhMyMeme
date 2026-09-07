import hashlib
import io
import json
import secrets
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ohmymeme.app.manifest_service import ManifestService
from ohmymeme.app.pull_commit_service import PullCommitService
from ohmymeme.core.assets import AssetPaths
from ohmymeme.core.database import MemeDB


SCENARIOS = (
    "downloaded-invalid",
    "staged-valid",
    "file-replaced-db-failed",
    "db-committed-manifest-failed",
    "manifest-write-failed",
    "staging-cleanup-failed",
    "heartbeat-failed",
    "restart-with-journal",
)

type JsonValue = str | int | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class _ExecutionSeal:
    run_id: str


@dataclass(frozen=True, slots=True)
class _ScenarioExecution:
    seal: _ExecutionSeal
    scenario: str
    row_id: str
    report: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _ExecutedMatrix:
    seal: _ExecutionSeal
    entries: tuple[_ScenarioExecution, ...]

    def report_rows(self):
        return {
            entry.scenario: {
                **entry.report,
                "execution_identity": {
                    "run_id": self.seal.run_id,
                    "row_id": entry.row_id,
                },
            }
            for entry in self.entries
        }


def new_execution_seal():
    return _ExecutionSeal(secrets.token_urlsafe(24))


def _png():
    output = io.BytesIO()
    Image.new("RGBA", (1, 1), (1, 2, 3, 255)).save(output, "PNG")
    return output.getvalue()


@contextmanager
def _fixture(root, original=False):
    assets = AssetPaths(root / "data", root / "data" / "cache")
    assets.cache_dir.mkdir(parents=True)
    database = MemeDB(assets.data_dir / "memes.db")
    data = _png()
    projection = ManifestService().parse_data(
        {
            "version": 3,
            "memes": [
                {
                    "filename": "asset.png",
                    "name": "asset",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "file_size": len(data),
                    "mtime": "1",
                    "sort_order": 0,
                }
            ],
            "collections": [],
        }
    )
    manifest = assets.data_dir / "meme-index.json"

    def apply_metadata(value):
        meme = value.memes[0]
        database.add_meme(meme.filename, file_hash=meme.sha256)

    def write_manifest(value):
        manifest.write_bytes(ManifestService().canonical_bytes(value.to_data()))

    service = PullCommitService(
        assets,
        apply_metadata,
        write_manifest,
        lambda: write_manifest(projection),
        lambda _record: database.get_by_filename("asset.png") is not None,
    )
    if original:
        (assets.cache_dir / "asset.png").write_bytes(b"old")
    try:
        yield {
            "assets": assets,
            "data": data,
            "database": database,
            "manifest": manifest,
            "projection": projection,
            "service": service,
        }
    finally:
        database.close()


def _download(data):
    def download(_meme, destination):
        destination.write_bytes(data)
        return True

    return download


def _state(fixture):
    assets = fixture["assets"]
    service = fixture["service"]
    manifest = fixture["manifest"]
    journal = "absent"
    if service.journal_path.exists():
        journal = json.loads(service.journal_path.read_text(encoding="utf-8"))["phase"]
    files = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(assets.cache_dir.iterdir())
        if path.is_file() and path.name != ".ohmymeme-storage-recovery.lock"
    }
    database = [
        {"filename": row["filename"], "sha256": row["file_hash"]}
        for row in fixture["database"].search(limit=100)
    ]
    manifest_state = {"status": "absent"}
    if manifest.exists():
        data = ManifestService().parse_json(manifest.read_bytes()).to_data()
        manifest_state = {
            "status": "present",
            "filenames": [meme["filename"] for meme in data["memes"]],
            "sort_orders": [meme["sort_order"] for meme in data["memes"]],
        }
    return {"journal": journal, "database": database, "files": files, "manifest": manifest_state}


def _row(fixture, before, remote, fixture_name, fault, assertions):
    after = _state(fixture)
    local = [key for key in before if before[key] != after[key]]
    return {
        "fixture": fixture_name,
        "fault": fault,
        "assertions": assertions,
        "observed": {
            "local_mutations": local,
            "remote_mutations": remote,
            "final_state": after,
        },
    }


def _run_row(evidence_root, name, runner, seal):
    with tempfile.TemporaryDirectory(prefix=".task-14-", dir=evidence_root) as temporary:
        root = Path(temporary)
        row = runner(root)
    row["executed"] = True
    row["cleanup"] = {"temporary_root_removed": not root.exists()}
    return _ScenarioExecution(seal, name, secrets.token_urlsafe(16), row)


def run_matrix(evidence_root, seal):
    from scripts.task_14_fault_scenarios import (
        db_committed_manifest_failed,
        downloaded_invalid,
        file_replaced_db_failed,
        heartbeat_failed,
        manifest_write_failed,
        restart_with_journal,
        staged_valid,
        staging_cleanup_failed,
    )

    runners = (
        ("downloaded-invalid", downloaded_invalid),
        ("staged-valid", staged_valid),
        ("file-replaced-db-failed", file_replaced_db_failed),
        ("db-committed-manifest-failed", db_committed_manifest_failed),
        ("manifest-write-failed", manifest_write_failed),
        ("staging-cleanup-failed", staging_cleanup_failed),
        ("heartbeat-failed", heartbeat_failed),
        ("restart-with-journal", restart_with_journal),
    )
    return _ExecutedMatrix(
        seal,
        tuple(_run_row(evidence_root, name, runner, seal) for name, runner in runners),
    )


def validate_matrix(matrix, seal):
    if not isinstance(matrix, _ExecutedMatrix) or matrix.seal is not seal:
        return False
    if {entry.scenario for entry in matrix.entries} != set(SCENARIOS):
        return False
    row_ids = set()
    for entry in matrix.entries:
        if entry.seal is not seal or not entry.row_id or entry.row_id in row_ids:
            return False
        row_ids.add(entry.row_id)
        row = entry.report
        observed = row.get("observed", {})
        if not row.get("executed") or not row.get("assertions") or set(observed) != {"local_mutations", "remote_mutations", "final_state"}:
            return False
        if set(observed["final_state"]) != {"journal", "database", "files", "manifest"}:
            return False
        if not row.get("cleanup", {}).get("temporary_root_removed"):
            return False
    return True
