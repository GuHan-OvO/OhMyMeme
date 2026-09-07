"""Record Todo 10 atomic filesystem evidence outside the worktree."""

import argparse
import hashlib
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from scripts.baseline_contracts import canonical_bytes, is_reparse_or_symlink, sha256_path


_GENERATOR_VERSION = "todo-10-atomic-file-repository/3"
_SOURCE_FILES = (
    "src/ohmymeme/core/adapters/filesystem/atomic_repository.py",
    "src/ohmymeme/core/imports.py",
    "tests/adapters/filesystem/test_atomic_repository.py",
    "tests/application/test_import_service.py",
    "scripts/build_task_10_evidence.py",
    "mise.toml",
)
_FAULT_MATRIX = {
    "disk_full": "test_commit_bytes_when_filesystem_commit_fails_leaves_no_asset_or_temp[fsync]",
    "rename_failure": "test_commit_bytes_when_filesystem_commit_fails_leaves_no_asset_or_temp[rename]",
    "interrupted_temp": "test_recover_when_interrupted_before_atomic_replace_removes_private_temp",
    "duplicate": "test_commit_bytes_when_duplicate_exists_does_not_replace_live_file",
    "cross_root": "test_commit_path_when_cross_root_copies_without_consuming_source",
    "db_manifest_compensation": "test_import_transaction_faults_compensate_files_and_rows",
    "files_copied_restart": "test_cross_volume_recovery_when_interrupted_after_file_copy_rolls_back_before_metadata",
    "metadata_restart": "test_cross_volume_recovery_when_metadata_target_persisted_forwards_before_manifest",
    "manifest_restart": "test_cross_volume_migration_when_manifest_interrupts_restarts_from_db_committed",
    "marker_preservation": "test_recover_when_todo4_marker_exists_preserves_private_temp_for_unified_recovery",
}


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    commit = result.stdout.strip()
    if result.returncode or len(commit) != 40:
        raise RuntimeError("BLOCKED_BASELINE_COMMIT")
    return commit


def _manual_probe() -> dict[str, str | int | bool]:
    from ohmymeme.core.adapters.filesystem.atomic_repository import AtomicFileRepository
    from ohmymeme.core.assets import AssetPaths

    with tempfile.TemporaryDirectory(prefix="ohmymeme-task-10-") as temporary:
        root = Path(temporary)
        source = root / "incoming" / "clipboard-source.gif"
        source.parent.mkdir()
        source.write_bytes(b"clipboard-source")
        assets = AssetPaths(root / "data", root / "custom-cache")
        repository = AtomicFileRepository(assets)
        committed = repository.commit_path(source, ".gif")
        assets.thumbnail_dir.mkdir(parents=True)
        (assets.thumbnail_dir / "7_150.png").write_bytes(b"stale-thumbnail")
        target_root = root / "recovered-cache"
        metadata = []
        copies = []
        copy_and_verify = repository._recovery._copy_and_verify

        def record_copy(source_path, destination_path, expected_hash):
            copies.append((Path(source_path), Path(destination_path), expected_hash))
            return copy_and_verify(source_path, destination_path, expected_hash)

        repository._recovery._same_volume = lambda *_paths: False
        repository._recovery._copy_and_verify = record_copy
        try:
            repository.migrate_to(
                target_root,
                lambda: metadata.append(str(target_root)),
                lambda: (_ for _ in ()).throw(OSError("manual manifest interruption")),
            )
        except OSError as error:
            if str(error) != "manual manifest interruption":
                raise
        reopened = AtomicFileRepository(assets)
        manifests = []
        recovered = reopened.recover_migration(target_root, lambda: manifests.append("manifest"))
        located = AtomicFileRepository(AssetPaths(root / "data", target_root)).locate(
            committed.filename
        )
        reopened.invalidate_thumbnails(7)
        if (
            source.read_bytes() != b"clipboard-source"
            or metadata != [str(target_root)]
            or manifests != ["manifest"]
            or not recovered
            or located is None
            or copies
            != [(committed.path, target_root / committed.filename, committed.content_hash)]
            or assets.thumbnail_dir.joinpath("7_150.png").exists()
            or repository._recovery.marker_path.exists()
        ):
            raise RuntimeError("manual atomic repository probe failed")
        return {
            "root_kind": "TemporaryDirectory",
            "custom_cache_root": True,
            "source_lifetime_preserved": True,
            "thumbnail_invalidated": True,
            "unified_manifest_recovery": True,
            "strategy": "forced_copy_hash_verify_commit",
            "copy_calls": len(copies),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--plan", default=".omo/plans/full-project-modular-refactor.md")
    parser.add_argument("--evidence-root", required=True)
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    plan = Path(args.plan).resolve()
    evidence_root = Path(args.evidence_root).resolve()
    report_path = evidence_root / "task-10.json"
    if (
        not evidence_root.is_dir()
        or evidence_root.is_relative_to(repo_root)
        or is_reparse_or_symlink(evidence_root)
        or report_path.exists()
    ):
        print("BLOCKED_INVALID_EVIDENCE_ROOT", file=sys.stderr)
        return 2
    result = subprocess.run(
        [
            "mise",
            "run",
            "test-python",
            "--",
            "tests/adapters/filesystem/test_atomic_repository.py",
            "tests/application/test_import_service.py",
            "tests/recovery/test_storage_recovery.py",
            "-q",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    if result.returncode:
        print(result.stdout + result.stderr, end="", file=sys.stderr)
        return result.returncode
    output = result.stdout + result.stderr
    report = {
        "schema_version": 1,
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "baseline_commit": _commit(repo_root),
        "generated_at_utc": _timestamp(),
        "generator_version": _GENERATOR_VERSION,
        "verdict": "pass",
        "evidence_files": [{"scope": "evidence_root", "path": "task-10.json"}],
        "commands": [
            "mise run test-python -- tests/adapters/filesystem/test_atomic_repository.py tests/application/test_import_service.py tests/recovery/test_storage_recovery.py -q"
        ],
        "test_output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "fault_matrix": _FAULT_MATRIX,
        "manual_filesystem_evidence": _manual_probe(),
        "source_sha256": {
            path: sha256_path(repo_root / path) for path in _SOURCE_FILES
        },
        "cleanup_receipt": {
            "private_temporaries": "recover removes .atomic-*.tmp after interruption",
            "failed_commits": "fsync and rename failures remove private temporary files",
            "unified_recovery": "manual probe restarts a db_committed StorageRecovery transaction and clears its marker after manifest completion",
            "cross_volume": "manual probe forces StorageRecovery._same_volume false and records the real copy/hash/verify call",
            "clipboard_source": "commit_path copies without consuming caller source",
        },
    }
    report_path.write_bytes(canonical_bytes(report))
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
