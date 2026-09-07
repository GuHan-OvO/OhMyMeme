"""Create Todo14 external evidence."""

import argparse
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scripts.baseline_contracts import (
    canonical_bytes,
    canonical_load,
    is_reparse_or_symlink,
    sha256_path,
)
from scripts.task_14_fault_matrix import SCENARIOS


ROOT = Path(__file__).resolve().parent.parent

TESTS = (
    "tests/application/test_manifest_service.py",
    "tests/application/test_pull_commit_service.py",
    "tests/test_sync.py",
    "tests/test_lan.py",
)
SOURCES = (
    "src/ohmymeme/app/manifest_service.py",
    "src/ohmymeme/app/pull_commit_service.py",
    "src/ohmymeme/core/manifest.py",
    "src/ohmymeme/core/database.py",
    "src/ohmymeme/services/sync/planning.py",
    "src/ohmymeme/services/sync/service.py",
    "src/ohmymeme/services/lan/commands.py",
    "src/ohmymeme/app/container.py",
    "scripts/build_task_14_evidence.py",
    "scripts/task_14_fault_matrix.py",
    "scripts/task_14_fault_scenarios.py",
    "scripts/run_task_14_fault_matrix.py",
    *TESTS,
    "tests/test_task_14_evidence.py",
)


class EvidenceBlocked(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    repo_root: Path
    plan: Path
    evidence_root: Path


def _validate(request):
    if (
        not request.evidence_root.is_dir()
        or request.evidence_root.is_relative_to(request.repo_root)
        or is_reparse_or_symlink(request.evidence_root)
    ):
        raise EvidenceBlocked("BLOCKED_INVALID_EVIDENCE_ROOT")
    if not request.plan.is_file():
        raise EvidenceBlocked("BLOCKED_MISSING_PLAN")
    if (request.evidence_root / "task-14.json").exists():
        raise EvidenceBlocked("BLOCKED_EVIDENCE_EXISTS")


def _run_tests(request):
    try:
        result = subprocess.run(
            ["mise", "run", "test-python", "--", *TESTS, "-q"],
            cwd=request.repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=180,
        )
    except subprocess.TimeoutExpired as error:
        raise EvidenceBlocked("BLOCKED_MANIFEST_QA_TIMEOUT") from error
    if result.returncode:
        raise EvidenceBlocked("BLOCKED_MANIFEST_QA")
    return result.stdout


def _run_fault_matrix(request):
    command = [
        sys.executable,
        "-m",
        "scripts.run_task_14_fault_matrix",
        "--evidence-root",
        str(request.evidence_root),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=request.repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=180,
        )
    except subprocess.TimeoutExpired as error:
        raise EvidenceBlocked("BLOCKED_FAULT_MATRIX_TIMEOUT") from error
    if result.returncode:
        raise EvidenceBlocked("BLOCKED_FAULT_MATRIX")
    try:
        payload = canonical_load(result.stdout.encode("utf-8"))
    except (UnicodeEncodeError, ValueError) as error:
        raise EvidenceBlocked("BLOCKED_FAULT_MATRIX") from error
    if not _valid_execution_payload(payload):
        raise EvidenceBlocked("BLOCKED_FAULT_MATRIX")
    return payload, {"command": command, "output_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()}


_TRUSTED_TEST_RUNNER = _run_tests
_TRUSTED_FAULT_RUNNER = _run_fault_matrix


def _valid_execution_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {"execution_identity", "fault_matrix"}:
        return False
    run_id = payload["execution_identity"]
    rows = payload["fault_matrix"]
    if not isinstance(run_id, str) or not run_id or not isinstance(rows, dict) or set(rows) != set(SCENARIOS):
        return False
    row_ids = set()
    for row in rows.values():
        if not isinstance(row, dict):
            return False
        identity = row.get("execution_identity")
        observed = row.get("observed")
        if (
            not isinstance(identity, dict)
            or identity.get("run_id") != run_id
            or not isinstance(identity.get("row_id"), str)
            or not identity["row_id"]
            or identity["row_id"] in row_ids
            or not row.get("executed")
            or not row.get("assertions")
            or not isinstance(observed, dict)
            or set(observed) != {"local_mutations", "remote_mutations", "final_state"}
            or not isinstance(observed["final_state"], dict)
            or set(observed["final_state"]) != {"journal", "database", "files", "manifest"}
            or row.get("cleanup", {}).get("temporary_root_removed") is not True
        ):
            return False
        row_ids.add(identity["row_id"])
    return True


def build_evidence(request):
    _validate(request)
    if _run_tests is not _TRUSTED_TEST_RUNNER or _run_fault_matrix is not _TRUSTED_FAULT_RUNNER:
        raise EvidenceBlocked("BLOCKED_UNTRUSTED_EXECUTION_BOUNDARY")
    output = _run_tests(request)
    execution, provenance = _run_fault_matrix(request)
    rows = execution["fault_matrix"]
    commit = subprocess.run(
        ["git", "-C", str(request.repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    ).stdout.strip()
    if len(commit) != 40:
        raise EvidenceBlocked("BLOCKED_BASELINE_COMMIT")
    status = subprocess.run(
        ["git", "-C", str(request.repo_root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    report = {
        "schema_version": 1,
        "generator_version": "todo-14-manifest-pull/4",
        "verdict": "pass",
        "baseline_commit": commit,
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "plan_sha256": hashlib.sha256(request.plan.read_bytes()).hexdigest(),
        "source_provenance": {"worktree_state": "dirty" if status.stdout else "clean"},
        "source_sha256": {path: sha256_path(request.repo_root / path) for path in SOURCES},
        "canonical_fixtures": ["v2-flat", "v3-three-level", "duplicate-key", "unicode-casefold"],
        "mutation_set": ["stage", "replace", "metadata", "manifest", "cleanup"],
        "journal_recovery": ["staged", "files_replaced", "needs_recovery", "db_committed", "cleanup_pending"],
        "execution_identity": execution["execution_identity"],
        "execution_provenance": {
            "scenario_command": provenance["command"],
            "scenario_output_sha256": provenance["output_sha256"],
            "test_output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        },
        "fault_matrix": rows,
        "focused_test_output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "cleanup_receipt": {
            "scenario_count": len(rows),
            "temporary_roots_removed": all(
                row["cleanup"]["temporary_root_removed"] for row in rows.values()
            ),
        },
    }
    destination = request.evidence_root / "task-14.json"
    destination.write_bytes(canonical_bytes(report))
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--plan", default=".omo/plans/full-project-modular-refactor.md")
    parser.add_argument("--evidence-root", required=True)
    args = parser.parse_args(argv)
    request = EvidenceRequest(
        Path(args.repo_root).resolve(), Path(args.plan).resolve(), Path(args.evidence_root).resolve()
    )
    try:
        print(build_evidence(request))
    except EvidenceBlocked as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
