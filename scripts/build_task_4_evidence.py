"""Run Todo 4 recovery tests and record external canonical evidence."""

import argparse
import hashlib
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from baseline_contracts import canonical_bytes, sha256_path
from baseline_validation import baseline_commit
from ohmymeme.core.recovery import StorageRecovery


_GENERATOR_VERSION = "todo-4-recovery/1"
_FAULT_MATRIX = {
    "disk_full": "test_pre_database_copy_failure_restores_original_file",
    "rename_commit": "test_same_volume_rename_failure_preserves_pre_database_source",
    "cache_disconnect": "test_disconnected_cache_root_fails_closed_before_database",
    "interruption": "test_stale_pre_database_marker_rolls_back_completed_cross_root_copy",
    "post_database": "test_post_database_manifest_failure_is_forward_recovered_on_restart",
    "metadata_marker_write": "test_metadata_committing_marker_forwards_when_config_persisted",
    "target_directory_collision": "test_target_directory_collision_stays_marked_and_fails_closed",
    "startup": "test_container_finishes_post_database_recovery_before_exposing_storage",
    "process_lock": "test_process_lock_rejects_concurrent_storage_mutation",
}


def _timestamp():
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _manual_fault_probe():
    """Exercise post-metadata recovery on actual isolated filesystem roots."""
    with tempfile.TemporaryDirectory(prefix="ohmymeme-task-4-") as temporary:
        root = Path(temporary)
        data_root = root / "data"
        source_root = root / "cache"
        target_root = root / "external-cache"
        data_root.mkdir()
        source_root.mkdir()
        source = source_root / "meme.bin"
        source.write_bytes(b"manual-fault-payload")
        events = []
        recovery = StorageRecovery(data_root, source_root)
        try:
            recovery.migrate(
                target_root,
                True,
                lambda: events.append("database"),
                lambda: (_ for _ in ()).throw(OSError("manual manifest failure")),
            )
        except OSError as error:
            if str(error) != "manual manifest failure":
                raise
        recovered = StorageRecovery(data_root, target_root)
        recovered.recover_before_database()
        recovered.finish_manifest(lambda: events.append("manifest"))
        if (
            events != ["database", "manifest"]
            or target_root.joinpath("meme.bin").read_bytes() != b"manual-fault-payload"
            or source.exists()
            or recovery.marker_path.exists()
        ):
            raise RuntimeError("manual recovery probe failed")
        return {"root_kind": "TemporaryDirectory", "events": events, "marker_cleared": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--plan", default=".omo/plans/full-project-modular-refactor.md")
    parser.add_argument("--evidence-root", required=True)
    args = parser.parse_args()
    repo_root = Path(args.repo_root).resolve()
    evidence_root = Path(args.evidence_root).resolve()
    plan = Path(args.plan).resolve()
    if not evidence_root.is_dir() or evidence_root.is_relative_to(repo_root):
        print("BLOCKED_INVALID_EVIDENCE_ROOT", file=sys.stderr)
        return 2
    report_path = evidence_root / "task-4.json"
    if report_path.exists():
        print("BLOCKED_EVIDENCE_EXISTS", file=sys.stderr)
        return 2
    result = subprocess.run(
        ["mise", "run", "test-python", "--", "tests/recovery", "-q"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        print(result.stdout, end="", file=sys.stderr)
        print(result.stderr, end="", file=sys.stderr)
        return result.returncode
    manual_probe = _manual_fault_probe()
    commit = baseline_commit(repo_root)
    if not commit:
        print("BLOCKED_BASELINE_COMMIT", file=sys.stderr)
        return 2
    output = result.stdout + result.stderr
    report = {
        "schema_version": 1,
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "baseline_commit": commit,
        "generated_at_utc": _timestamp(),
        "generator_version": _GENERATOR_VERSION,
        "verdict": "pass",
        "evidence_files": [{"scope": "evidence_root", "path": "task-4.json"}],
        "commands": ["mise run test-python -- tests/recovery -q"],
        "test_output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "fault_matrix": _FAULT_MATRIX,
        "source_sha256": {
            path: sha256_path(repo_root / path)
            for path in (
                "src/ohmymeme/core/recovery.py",
                "src/ohmymeme/app/container.py",
                "src/ohmymeme/core/manifest.py",
                "src/ohmymeme/presentation/desktop/window_manager.py",
                "tests/recovery/test_storage_recovery.py",
            )
        },
        "cleanup_evidence": {
            "durable_markers": "recovery tests assert marker removal after rollback and forward completion",
            "temporary_copies": "recovery tests use isolated pytest roots and assert no destination orphan after failure",
            "manual_fault_probe": manual_probe,
        },
    }
    report_path.write_bytes(canonical_bytes(report))
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
