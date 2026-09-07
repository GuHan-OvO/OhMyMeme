"""Record Todo 11 configuration compatibility evidence outside the worktree."""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from scripts.baseline_contracts import canonical_bytes, is_reparse_or_symlink, sha256_path

_GENERATOR_VERSION = "todo-11-config-compatibility/1"
_SOURCE_FILES = (
    "src/ohmymeme/core/config.py",
    "src/ohmymeme/core/crypto.py",
    "src/ohmymeme/app/settings.py",
    "tests/migration/test_config_upgrade.py",
    "tests/application/test_settings_service.py",
    "scripts/build_task_11_evidence.py",
    "mise.toml",
)
_FAULT_MATRIX = {
    "corrupt_json": "test_corrupt_json_when_config_opens_then_file_is_retained_and_error_is_deterministic",
    "interrupted_write": "test_write_interruption_when_fsync_fails_then_original_file_and_dirty_snapshot_remain",
    "replace_failure": "TestConfig.test_save_replace_failure_preserves_original_bytes_and_cleans_temp",
    "wrong_machine_id": "test_ciphertext_when_machine_id_is_wrong_then_decryption_fails_without_disclosing_value",
    "concurrent_snapshots": "test_concurrent_instances_when_saving_distinct_fields_then_neither_update_is_lost",
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
    from ohmymeme.app.settings import Settings
    from ohmymeme.core.config import Config

    with tempfile.TemporaryDirectory(prefix="ohmymeme-task-11-") as temporary:
        root = Path(temporary)
        config_path = root / "config.json"
        cache_dir = root / "external-cache"
        secret = hashlib.sha256(b"task-11-manual-probe").hexdigest()
        config = Config(config_path, root / "data")
        config.set("cache_dir", str(cache_dir))
        config.set("s3_secret_key", secret)
        config.save()
        raw = config_path.read_bytes()
        reopened_before_reset = Config(config_path, root / "data")
        callbacks: list[bool] = []
        settings = Settings(config, lambda: False, callbacks.append)
        reset = settings.reset_settings()
        reopened = Config(config_path, root / "data")
        if (
            secret.encode() in raw
            or reopened_before_reset.get("s3_secret_key") != secret
            or reopened.get("cache_dir") != str(cache_dir)
            or reopened.get("s3_secret_key") != ""
            or reset["cache_dir"] != str(cache_dir)
            or callbacks != [False]
        ):
            raise RuntimeError("manual configuration probe failed")
        return {
            "encrypted_write_reopened": True,
            "plaintext_secret_absent": True,
            "reset_preserves_cache_dir": True,
            "settings_auto_start_reset": True,
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "temporary_root_cleanup": "TemporaryDirectory",
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
    report_path = evidence_root / "task-11.json"
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
            "tests/migration/test_config_upgrade.py",
            "tests/application/test_settings_service.py",
            "tests/test_core.py",
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
        "evidence_files": [{"scope": "evidence_root", "path": "task-11.json"}],
        "commands": [
            "mise run test-python -- tests/migration/test_config_upgrade.py tests/application/test_settings_service.py tests/test_core.py tests/recovery/test_storage_recovery.py -q"
        ],
        "test_output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "fixture_hashes": {path: sha256_path(repo_root / path) for path in _SOURCE_FILES},
        "fault_matrix": _FAULT_MATRIX,
        "manual_configuration_evidence": _manual_probe(),
        "cleanup_receipt": {
            "temporary_snapshots": "failed fsync and replace cases remove config.json.tmp",
            "stale_config": "corrupt JSON raises ConfigCorrupt without replacing the original file",
            "secret_leakage": "manual evidence contains hashes and booleans only",
            "manual_temp_root": "TemporaryDirectory removes the encrypted-write probe root",
        },
    }
    report_path.write_bytes(canonical_bytes(report))
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
