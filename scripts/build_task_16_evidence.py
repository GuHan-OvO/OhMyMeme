"""Build execution-bound Todo16 evidence from loopback protocol verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scripts.baseline_contracts import canonical_bytes, canonical_load, is_reparse_or_symlink, sha256_path
from scripts.local_remote_contracts import REQUIRED_PROFILES

ROOT = Path(__file__).resolve().parent.parent
TESTS = (
    "tests/application/test_remote_mutation_coordinator.py",
    "tests/test_remote_contracts_local.py",
    "tests/test_webdav_backend.py",
    "tests/test_sync.py",
    "tests/test_lan.py",
)
EVIDENCE_TESTS = (*TESTS, "tests/test_task_16_evidence.py")
SOURCE_FILES = (
    "mise.toml",
    "src/ohmymeme/app/remote_mutation_coordinator.py",
    "src/ohmymeme/app/remote_mutation_errors.py",
    "src/ohmymeme/app/container.py",
    "src/ohmymeme/app/library.py",
    "src/ohmymeme/core/imports.py",
    "src/ohmymeme/services/sync/backends.py",
    "src/ohmymeme/services/sync/service.py",
    "src/ohmymeme/services/lan/commands.py",
    "src/ohmymeme/services/lan/server.py",
    "src/ohmymeme/presentation/desktop/window_manager.py",
    "scripts/local_remote_servers.py",
    "scripts/local_remote_contracts.py",
    "scripts/build_task_16_evidence.py",
    *EVIDENCE_TESTS,
)


class EvidenceBlocked(RuntimeError):
    """The local contract execution cannot be trusted as evidence."""


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    repo_root: Path
    plan: Path
    evidence_root: Path


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    pid: int


def _validate(request: EvidenceRequest) -> Path:
    if (
        not request.evidence_root.is_dir()
        or request.evidence_root.resolve().is_relative_to(request.repo_root.resolve())
        or is_reparse_or_symlink(request.evidence_root)
    ):
        raise EvidenceBlocked("BLOCKED_INVALID_EVIDENCE_ROOT")
    if not request.plan.is_file():
        raise EvidenceBlocked("BLOCKED_MISSING_PLAN")
    destination = request.evidence_root / "task-16.json"
    if destination.exists():
        raise EvidenceBlocked("BLOCKED_EVIDENCE_EXISTS")
    missing = [path for path in SOURCE_FILES if not (request.repo_root / path).exists()]
    if missing:
        raise EvidenceBlocked(f"BLOCKED_MISSING_SOURCE:{missing[0]}")
    return destination


def _run_isolated(command: list[str], cwd: Path, timeout: int) -> _ProcessResult:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.communicate()
        raise EvidenceBlocked("BLOCKED_ISOLATED_COMMAND_TIMEOUT") from error
    return _ProcessResult(tuple(command), process.returncode, stdout, stderr, process.pid)


def _run_tests(request: EvidenceRequest) -> _ProcessResult:
    result = _run_isolated(
        ["mise", "run", "test-python", "--", *TESTS, "-q"],
        request.repo_root,
        240,
    )
    if result.returncode:
        raise EvidenceBlocked("BLOCKED_REMOTE_LOCAL_FOCUSED_TESTS")
    return result


def _run_contract_command(request: EvidenceRequest) -> dict:
    result = _run_isolated(
        ["mise", "run", "test-remote-contracts-local"],
        request.repo_root,
        240,
    )
    if result.returncode:
        raise EvidenceBlocked("BLOCKED_REMOTE_LOCAL_CONTRACTS")
    try:
        payload = canonical_load(result.stdout.encode("utf-8"))
    except (UnicodeEncodeError, ValueError) as error:
        raise EvidenceBlocked("BLOCKED_REMOTE_LOCAL_CONTRACTS_OUTPUT") from error
    if not _valid_execution(payload):
        raise EvidenceBlocked("BLOCKED_REMOTE_LOCAL_CONTRACTS_OUTPUT")
    return {
        "command": list(result.command),
        "process_id": result.pid,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "output_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
        "execution": payload,
    }


def _hash_inputs(request: EvidenceRequest) -> dict[str, dict[str, str]]:
    return {
        "source": {
            path: sha256_path(request.repo_root / path) for path in SOURCE_FILES
        },
        "test": {
            path: sha256_path(request.repo_root / path) for path in EVIDENCE_TESTS
        },
        "plan": {"path": sha256_path(request.plan)},
    }


def _valid_execution(payload: dict) -> bool:
    if payload.get("schema_version") != 1 or payload.get("verdict") != "pass":
        return False
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        return False
    for name in REQUIRED_PROFILES:
        row = profiles.get(name)
        if not isinstance(row, dict) or row.get("status") != "pass":
            return False
    coordinator = profiles.get("coordinator")
    transcript = coordinator.get("transcript") if isinstance(coordinator, dict) else None
    return (
        isinstance(coordinator, dict)
        and isinstance(transcript, list)
        and bool(transcript)
        and isinstance(transcript[-1], dict)
        and transcript[-1].get("event") == "release"
        and payload.get("environment", {}).get("network") == "loopback-only"
        and payload.get("environment", {}).get("production_endpoints") == "not-attempted"
        and profiles.get("ftps-control", {}).get("tls_scope") == "control-channel"
        and payload.get("limitations", {}).get("ftps") == (
            "control-channel TLS only; PROT P data-channel not enabled/verified"
        )
        and payload.get("limitations", {}).get("s3-signatures") == (
            "differential-only; request shape is observed but canonical cryptography is not independently verified"
        )
        and payload.get("authentication", {}).get("status") == "pass"
        and all(
            payload.get("authentication", {}).get(name, {}).get("rejected") is True
            and payload.get("authentication", {}).get(name, {}).get("mutations") == 0
            for name in ("ftp", "webdav", "s3")
        )
        and payload.get("cleanup", {}).get("temporary_root_removed") is True
    )


_TRUSTED_TEST_RUNNER = _run_tests
_TRUSTED_CONTRACT_RUNNER = _run_contract_command
_TRUSTED_SUBPROCESS_RUN = subprocess.run


def build_evidence(request: EvidenceRequest) -> Path:
    destination = _validate(request)
    if (
        _run_tests is not _TRUSTED_TEST_RUNNER
        or _run_contract_command is not _TRUSTED_CONTRACT_RUNNER
        or subprocess.run is not _TRUSTED_SUBPROCESS_RUN
    ):
        raise EvidenceBlocked("BLOCKED_UNTRUSTED_EXECUTION_BOUNDARY")
    before = _hash_inputs(request)
    runner_identity = {
        "path": "scripts/build_task_16_evidence.py",
        "sha256": before["source"]["scripts/build_task_16_evidence.py"],
    }
    test_result = _run_tests(request)
    contract = _run_contract_command(request)
    execution = contract.pop("execution")
    after = _hash_inputs(request)
    if before != after:
        raise EvidenceBlocked("BLOCKED_INPUT_CHANGED")
    commit_result = _run_isolated(
        ["git", "-C", str(request.repo_root), "rev-parse", "HEAD"],
        request.repo_root,
        30,
    )
    commit = commit_result.stdout.strip()
    if len(commit) != 40:
        raise EvidenceBlocked("BLOCKED_BASELINE_COMMIT")
    report = {
        "schema_version": 1,
        "generator_version": "todo-16-remote-local/1",
        "verdict": "pass",
        "baseline_commit": commit,
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "plan_sha256": hashlib.sha256(request.plan.read_bytes()).hexdigest(),
        "evidence_files": ["task-16.json", *EVIDENCE_TESTS],
        "source_sha256": before["source"],
        "input_binding": {
            "source_sha256_before": before["source"],
            "source_sha256_after": after["source"],
            "test_sha256_before": before["test"],
            "test_sha256_after": after["test"],
            "plan_sha256_before": before["plan"]["path"],
            "plan_sha256_after": after["plan"]["path"],
            "test_files": list(EVIDENCE_TESTS),
            "runner_identity": runner_identity,
        },
        "execution_provenance": {
            "focused_test_output_sha256": hashlib.sha256(test_result.stdout.encode("utf-8")).hexdigest(),
            "contract_output_sha256": contract["output_sha256"],
            "contract_command": contract["command"],
            "isolated_processes": [
                {
                    "pid": test_result.pid,
                    "command": list(test_result.command),
                    "output_sha256": hashlib.sha256(test_result.stdout.encode("utf-8")).hexdigest(),
                },
                {
                    "pid": contract["process_id"],
                    "command": contract["command"],
                    "output_sha256": contract["output_sha256"],
                },
                {
                    "pid": commit_result.pid,
                    "command": list(commit_result.command),
                    "output_sha256": hashlib.sha256(commit_result.stdout.encode("utf-8")).hexdigest(),
                },
            ],
            "isolated_runner": "subprocess.Popen",
        },
        "contract_command": contract,
        "environment": execution["environment"],
        "backend_matrix": execution["profiles"],
        "limitations": execution["limitations"],
        "authentication": execution["authentication"],
        "optional_server_matrix": execution["optional_servers"],
        "mutation_transcript": execution["profiles"]["coordinator"]["transcript"],
        "cleanup_receipt": {
            "production_endpoint_touched": False,
            "external_network": "not-attempted",
            "temporary_root_removed": execution["cleanup"]["temporary_root_removed"],
            "credentials_recorded": False,
        },
    }
    destination.write_bytes(canonical_bytes(report))
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".", type=Path)
    parser.add_argument("--plan", default=".omo/plans/full-project-modular-refactor.md", type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args(argv)
    request = EvidenceRequest(args.repo_root.resolve(), args.plan.resolve(), args.evidence_root.resolve())
    try:
        print(build_evidence(request))
    except EvidenceBlocked as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
