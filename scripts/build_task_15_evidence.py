"""Build execution-bound external evidence for Todo 15 atomic image import."""

import argparse
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scripts.baseline_contracts import canonical_bytes, canonical_load, is_reparse_or_symlink, sha256_path
from scripts.run_task_15_import_evidence import SCENARIOS


TESTS = (
    "tests/application/test_import_service.py",
    "tests/application/test_manifest_service.py",
    "tests/application/test_pull_commit_service.py",
    "tests/test_sync.py",
    "tests/test_qqnt_import_boundary.py",
)
SOURCES = (
    "mise.toml",
    "src/ohmymeme/core/gif_stego.py",
    "src/ohmymeme/core/imports.py",
    "src/ohmymeme/app/container.py",
    "src/ohmymeme/app/manifest_service.py",
    "src/ohmymeme/app/pull_commit_service.py",
    "src/ohmymeme/presentation/desktop/import_workers.py",
    "src/ohmymeme/presentation/desktop/window_manager.py",
    "src/ohmymeme/integrations/imports/qqnt.py",
    "src/ohmymeme/services/lan/commands.py",
    "src/ohmymeme/services/sync/service.py",
    "scripts/run_task_15_import_evidence.py",
    "scripts/build_task_15_evidence.py",
    *TESTS,
    "tests/test_task_15_evidence.py",
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
    if (request.evidence_root / "task-15.json").exists():
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
        raise EvidenceBlocked("BLOCKED_IMPORT_QA_TIMEOUT") from error
    if result.returncode:
        raise EvidenceBlocked("BLOCKED_IMPORT_QA")
    return result.stdout


def _run_scenarios(request):
    command = [sys.executable, "-m", "scripts.run_task_15_import_evidence", "--evidence-root", str(request.evidence_root)]
    try:
        result = subprocess.run(command, cwd=request.repo_root, capture_output=True, text=True, encoding="utf-8", check=False, timeout=120)
    except subprocess.TimeoutExpired as error:
        raise EvidenceBlocked("BLOCKED_IMPORT_SCENARIOS_TIMEOUT") from error
    if result.returncode:
        raise EvidenceBlocked("BLOCKED_IMPORT_SCENARIOS")
    try:
        execution = canonical_load(result.stdout.encode("utf-8"))
    except (UnicodeEncodeError, ValueError) as error:
        raise EvidenceBlocked("BLOCKED_IMPORT_SCENARIOS") from error
    if not _valid_execution(execution):
        raise EvidenceBlocked("BLOCKED_IMPORT_SCENARIOS")
    return execution, command, result.stdout


def _valid_execution(execution):
    if not isinstance(execution, dict) or set(execution) != {"execution_identity", "scenarios"}:
        return False
    run_id = execution["execution_identity"]
    rows = execution["scenarios"]
    if not isinstance(run_id, str) or not run_id or not isinstance(rows, dict) or set(rows) != set(SCENARIOS):
        return False
    row_ids = set()
    for row in rows.values():
        identity = row.get("execution_identity") if isinstance(row, dict) else None
        if (
            not isinstance(identity, dict)
            or identity.get("run_id") != run_id
            or not isinstance(identity.get("row_id"), str)
            or not identity["row_id"]
            or identity["row_id"] in row_ids
            or row.get("executed") is not True
            or not row.get("assertions")
            or not isinstance(row.get("observed"), dict)
            or row.get("cleanup", {}).get("temporary_root_removed") is not True
        ):
            return False
        row_ids.add(identity["row_id"])
    return True


_TRUSTED_TEST_RUNNER = _run_tests
_TRUSTED_SCENARIO_RUNNER = _run_scenarios


def build_evidence(request):
    _validate(request)
    if _run_tests is not _TRUSTED_TEST_RUNNER or _run_scenarios is not _TRUSTED_SCENARIO_RUNNER:
        raise EvidenceBlocked("BLOCKED_UNTRUSTED_EXECUTION_BOUNDARY")
    test_output = _run_tests(request)
    execution, command, scenario_output = _run_scenarios(request)
    commit = subprocess.run(["git", "-C", str(request.repo_root), "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8", check=False).stdout.strip()
    if len(commit) != 40:
        raise EvidenceBlocked("BLOCKED_BASELINE_COMMIT")
    report = {
        "schema_version": 1,
        "generator_version": "todo-15-atomic-image-import/1",
        "verdict": "pass",
        "baseline_commit": commit,
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "plan_sha256": hashlib.sha256(request.plan.read_bytes()).hexdigest(),
        "source_sha256": {path: sha256_path(request.repo_root / path) for path in SOURCES},
        "execution_identity": execution["execution_identity"],
        "scenario_matrix": execution["scenarios"],
        "execution_provenance": {
            "scenario_command": command,
            "scenario_output_sha256": hashlib.sha256(scenario_output.encode("utf-8")).hexdigest(),
            "test_output_sha256": hashlib.sha256(test_output.encode("utf-8")).hexdigest(),
        },
        "cleanup_receipt": {"scenario_count": len(execution["scenarios"]), "temporary_roots_removed": True},
    }
    destination = request.evidence_root / "task-15.json"
    destination.write_bytes(canonical_bytes(report))
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--plan", default=".omo/plans/full-project-modular-refactor.md")
    parser.add_argument("--evidence-root", required=True)
    args = parser.parse_args(argv)
    request = EvidenceRequest(Path(args.repo_root).resolve(), Path(args.plan).resolve(), Path(args.evidence_root).resolve())
    try:
        print(build_evidence(request))
    except EvidenceBlocked as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
