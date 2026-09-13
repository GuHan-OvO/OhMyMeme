# pyright: basic

import io
import json
import sys
import tempfile
from pathlib import Path

from PIL import Image

from ohmymeme.core.assets import AssetPaths
from ohmymeme.core.imports import (
    HostImportSink,
    ImageImportService,
    ImportBytes,
    ImportPath,
)

ROOT = Path(__file__).resolve().parents[1]


EXPECTED_CASES = (
    "malformed-bytes",
    "precommit-cancellation",
    "postcommit-cancellation",
    "manifest-crash-recovery",
    "rollback-recovery",
)
EXPECTED_PHASES = (
    "staging",
    "staged",
    "files_committing",
    "files_committed",
    "metadata_committing",
    "db_committed",
    "manifest_committing",
    "manifest_committed",
    "rollback",
    "cleanup_pending",
)


class SinkQaError(RuntimeError):
    pass


class ManifestCrash(BaseException):
    pass


class FakeDb:
    def __init__(self):
        self.rows = {}
        self.next_id = 1

    def get_by_hash(self, file_hash):
        return self.rows.get(file_hash)

    def add_meme(
        self,
        filename,
        file_hash,
        width,
        height,
        file_size,
        mime_type,
        original_name,
        stego_of_hash=None,
        from_stego=0,
    ):
        meme_id = self.next_id
        self.next_id += 1
        self.rows[file_hash] = {
            "file_hash": file_hash,
            "filename": filename,
            "from_stego": from_stego,
            "height": height,
            "id": meme_id,
            "mime_type": mime_type,
            "original_name": original_name,
            "size": file_size,
            "stego_of_hash": stego_of_hash,
            "width": width,
        }
        return meme_id

    def delete_meme(self, meme_id):
        for file_hash, row in tuple(self.rows.items()):
            if row["id"] == meme_id:
                del self.rows[file_hash]
                return


def _parse_arguments(argv):
    fixture = None
    matrix = None
    report = None
    index = 0
    while index < len(argv):
        option = argv[index]
        if option in ("--fixture", "--recovery-matrix", "--report"):
            if index + 1 >= len(argv):
                raise SinkQaError(f"{option}: missing value")
            value = Path(argv[index + 1])
            if option == "--fixture":
                fixture = value
            elif option == "--recovery-matrix":
                matrix = value
            else:
                report = value
            index += 2
            continue
        if option == "--help":
            print(
                "usage: plugin_import_sink_qa.py --fixture FIXTURE "
                "--recovery-matrix MATRIX --report REPORT"
            )
            raise SystemExit(0)
        raise SinkQaError(f"unknown option: {option}")
    if fixture is None or matrix is None or report is None:
        raise SinkQaError("--fixture, --recovery-matrix and --report are required")
    return fixture, matrix, report


def _png_bytes():
    output = io.BytesIO()
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(output, "PNG")
    return output.getvalue()


def _validate_inputs(fixture_path, matrix_path):
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    if fixture.get("schema_version") != 1:
        raise SinkQaError("fixture.schema_version: expected 1")
    cases = fixture.get("cases")
    ids = tuple(case.get("id") for case in cases) if isinstance(cases, list) else ()
    if ids != EXPECTED_CASES:
        raise SinkQaError("fixture.cases: expected canonical failure cases")
    if matrix.get("schema_version") != 1:
        raise SinkQaError("matrix.schema_version: expected 1")
    if tuple(matrix.get("journal_phases", ())) != EXPECTED_PHASES:
        raise SinkQaError("matrix.journal_phases: expected host journal protocol")
    if matrix.get("cancellation") != {
        "postcommit": "complete_manifest",
        "precommit": "abort_without_durable_state",
    }:
        raise SinkQaError("matrix.cancellation: expected host cancellation protocol")
    if matrix.get("recovery") != {
        "forward": [
            "db_committed",
            "manifest_committing",
            "manifest_committed",
            "cleanup_pending",
        ],
        "rollback": [
            "staging",
            "staged",
            "files_committing",
            "files_committed",
            "metadata_committing",
            "rollback",
        ],
    }:
        raise SinkQaError("matrix.recovery: expected host recovery protocol")


def _sink(root, manifest):
    assets = AssetPaths(root / "data", root / "data" / "cache")
    database = FakeDb()
    service = ImageImportService(database, assets, manifest)
    return HostImportSink(service), service, database, assets


def _assert_no_durable_state(database, assets):
    if database.rows:
        raise SinkQaError("unexpected metadata row")
    if assets.cache_dir.exists() and tuple(assets.cache_dir.iterdir()):
        raise SinkQaError("unexpected cache file")
    if assets.manifest_path.exists():
        raise SinkQaError("unexpected manifest")
    if assets.recovery_marker_path.exists():
        raise SinkQaError("unexpected import journal")


def check(fixture_path, matrix_path, report):
    _validate_inputs(fixture_path, matrix_path)
    with tempfile.TemporaryDirectory(prefix="ohmymeme-import-sink-") as temporary:
        root = Path(temporary)

        sink, _service, database, assets = _sink(root / "malformed", lambda: None)
        malformed = sink.import_bytes(ImportBytes(b"not-an-image", "bad.bin"))
        if malformed.rejected != 1:
            raise SinkQaError("malformed bytes were accepted")
        _assert_no_durable_state(database, assets)

        source_root = root / "precommit"
        source_root.mkdir()
        source = source_root / "source.png"
        source.write_bytes(_png_bytes())
        sink, _service, database, assets = _sink(source_root, lambda: None)
        cancelled = sink.import_path(
            ImportPath(source, "source.png"), cancelled=lambda: True
        )
        if cancelled.imported_ids:
            raise SinkQaError("precommit cancellation imported media")
        _assert_no_durable_state(database, assets)

        postcommit = {"cancelled": False, "manifest": 0}

        def complete_manifest():
            postcommit["cancelled"] = True
            postcommit["manifest"] += 1

        sink, _service, database, assets = _sink(root / "postcommit", complete_manifest)
        completed = sink.import_bytes(
            ImportBytes(_png_bytes(), "complete.png"),
            cancelled=lambda: postcommit["cancelled"],
        )
        if not completed.imported_ids or postcommit["manifest"] != 1:
            raise SinkQaError(
                "postcommit cancellation interrupted manifest finalization"
            )
        if len(database.rows) != 1 or not tuple(assets.cache_dir.iterdir()):
            raise SinkQaError("postcommit import did not commit durable state")

        sink, _service, database, assets = _sink(
            root / "recovery", lambda: (_ for _ in ()).throw(ManifestCrash())
        )
        try:
            sink.import_bytes(ImportBytes(_png_bytes(), "recovery.png"))
        except ManifestCrash:
            pass
        else:
            raise SinkQaError("manifest crash injection was not observed")
        if not assets.recovery_marker_path.exists():
            raise SinkQaError("postcommit crash did not retain a recovery journal")
        recovered_manifest = []
        recovery = ImageImportService(
            database, assets, lambda: recovered_manifest.append("rebuilt")
        )
        if not recovery.recover() or recovered_manifest != ["rebuilt"]:
            raise SinkQaError("postcommit crash did not forward recover manifest")
        if assets.recovery_marker_path.exists():
            raise SinkQaError("recovery journal was not cleared")
        if len(database.rows) != 1 or not tuple(assets.cache_dir.iterdir()):
            raise SinkQaError("forward recovery left incomplete durable state")

        rollback_root = root / "rollback"
        rollback_assets = AssetPaths(
            rollback_root / "data", rollback_root / "data" / "cache"
        )
        rollback_assets.data_dir.mkdir(parents=True)
        rollback_assets.manifest_path.write_text("before", encoding="utf-8")

        def fail_manifest():
            rollback_assets.manifest_path.write_text("after", encoding="utf-8")
            raise OSError("manifest fault")

        rollback_db = FakeDb()
        rollback_service = ImageImportService(
            rollback_db, rollback_assets, fail_manifest
        )
        rollback_service._restore_manifest = lambda snapshot, failures: failures.append(
            "restore"
        )
        try:
            HostImportSink(rollback_service).import_bytes(
                ImportBytes(_png_bytes(), "rollback.png")
            )
        except OSError:
            pass
        else:
            raise SinkQaError("rollback fault injection was not observed")
        rollback_recovery = ImageImportService(
            rollback_db, rollback_assets, lambda: None
        )
        if not rollback_recovery.recover():
            raise SinkQaError("rollback journal was not recovered")
        if rollback_db.rows or tuple(rollback_assets.cache_dir.iterdir()):
            raise SinkQaError("rollback recovery left an orphan file or row")
        if rollback_assets.manifest_path.read_text(encoding="utf-8") != "before":
            raise SinkQaError("rollback recovery left a manifest ghost")
        if rollback_assets.recovery_marker_path.exists():
            raise SinkQaError("rollback journal was not cleared")

    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {"cases": list(EXPECTED_CASES), "status": "PASS"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    print("PASS: validated host import sink recovery protocol")


def main(argv=None):
    report = None
    try:
        fixture, matrix, report = _parse_arguments(
            sys.argv[1:] if argv is None else argv
        )
        check(fixture, matrix, report)
    except (OSError, SinkQaError, json.JSONDecodeError) as error:
        if report is not None:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps(
                    {"errors": [str(error)], "status": "REJECTED"},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
        print(f"REJECTED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
