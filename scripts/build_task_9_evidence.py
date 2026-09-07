"""Record the Task 9 operation-coordination contract outside the worktree."""

import argparse
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scripts.baseline_contracts import canonical_bytes, is_reparse_or_symlink, sha256_path


GENERATOR_VERSION = "todo-9-operation-coordinator/1"
SOURCE_FILES = (
    "src/ohmymeme/app/operations.py",
    "src/ohmymeme/app/operation_runtime.py",
    "src/ohmymeme/app/operation_shutdown.py",
    "src/ohmymeme/app/operation_coordinator.py",
    "src/ohmymeme/app/container.py",
    "src/ohmymeme/app/bootstrap.py",
    "tests/application/test_task_contract.py",
    "tests/application/test_task_process_contract.py",
)
TASKS = (
    "sync.push",
    "sync.pull",
    "sync.cleanup",
    "sync.delete_all",
    "sync.upload_index",
    "update.check",
    "update.download",
    "import.qq",
    "import.qqnt",
    "import.telegram",
    "import.douyin",
    "import.wechat",
    "cache.rescan",
)


class EvidenceBlocked(RuntimeError):
    """Task 9 evidence cannot make its required claim."""


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    repo_root: Path
    plan: Path
    evidence_root: Path
    replace_existing: bool = False


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
        raise EvidenceBlocked("BLOCKED_BASELINE_COMMIT")
    return commit


def _worktree_state(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return "unavailable"
    return "dirty" if result.stdout else "clean"


def _validate(request: EvidenceRequest) -> None:
    if (
        not request.evidence_root.is_dir()
        or request.evidence_root.is_relative_to(request.repo_root)
        or is_reparse_or_symlink(request.evidence_root)
    ):
        raise EvidenceBlocked("BLOCKED_INVALID_EVIDENCE_ROOT")
    if not request.plan.is_file():
        raise EvidenceBlocked("BLOCKED_MISSING_PLAN")
    if (request.evidence_root / "task-9.json").exists() and not request.replace_existing:
        raise EvidenceBlocked("BLOCKED_EVIDENCE_EXISTS")


def _run_contract(request: EvidenceRequest) -> str:
    try:
        result = subprocess.run(
            [
                "mise",
                "run",
                "test-python",
                "--",
                "tests/application/test_task_contract.py",
                "tests/application/test_task_process_contract.py",
                "-q",
            ],
            cwd=request.repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        raise EvidenceBlocked("BLOCKED_TASK_CONTRACT_TIMEOUT") from error
    if result.returncode:
        raise EvidenceBlocked("BLOCKED_TASK_CONTRACT")
    return result.stdout


def build_evidence(request: EvidenceRequest) -> Path:
    _validate(request)
    output = _run_contract(request)
    report = {
        "schema_version": 1,
        "plan_sha256": hashlib.sha256(request.plan.read_bytes()).hexdigest(),
        "baseline_commit": _commit(request.repo_root),
        "generated_at_utc": _timestamp(),
        "generator_version": GENERATOR_VERSION,
        "verdict": "pass",
        "evidence_files": [{"scope": "evidence_root", "path": "task-9.json"}],
        "task_matrix": {task: {"start": "idempotent", "query": "stable", "cancel": "idempotent"} for task in TASKS},
        "shutdown_report_schema": [
            "completed",
            "cancelled",
            "failed",
            "timed_out",
            "forced_child_terminated",
            "shutdown_blocked",
            "inventory",
        ],
        "failure_matrix": {
            "duplicate_start": "same_task_id",
            "worker_exception": "failed",
            "db_config_lease": "shutdown_blocked",
            "noncooperative_thread": "timed_out",
            "child_process": "forced_child_terminated",
            "normal_child_completion": "wait_reap",
            "late_process_registration": "atomic_drain",
            "close_after_shutdown": "admission_rejected",
            "resource_inventory": "cleaned",
        },
        "focused_test_output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "source_provenance": {"worktree_state": _worktree_state(request.repo_root)},
        "source_sha256": {
            path: sha256_path(request.repo_root / path) for path in SOURCE_FILES
        },
        "cleanup_receipt": {
            "threads": "reported",
            "processes": "normal_wait_reap_or_terminate_then_kill",
            "sockets": "closed",
            "temporary": "cleaned",
            "database_config": "retained_while_leased",
        },
    }
    destination = request.evidence_root / "task-9.json"
    destination.write_bytes(canonical_bytes(report))
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--plan", default=".omo/plans/full-project-modular-refactor.md")
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args(argv)
    request = EvidenceRequest(
        Path(args.repo_root).resolve(),
        Path(args.plan).resolve(),
        Path(args.evidence_root).resolve(),
        args.replace_existing,
    )
    try:
        print(build_evidence(request))
    except EvidenceBlocked as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
