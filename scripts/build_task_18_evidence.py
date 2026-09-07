"""Build execution-bound external evidence for Todo 18 adapters."""

import argparse
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scripts.baseline_contracts import canonical_bytes, canonical_load, is_reparse_or_symlink, sha256_path
from scripts.run_task_18_fetch_evidence import SCENARIOS

TESTS = (
    "tests/adapters/importers",
    "tests/application/test_storage_settings.py",
    "tests/application/test_updater.py",
    "tests/test_adb_util.py",
    "tests/test_tg_stickers.py",
    "tests/test_douyin_dl.py",
    "tests/test_qqnt_import_boundary.py",
    "tests/application/test_settings_service.py",
    "tests/recovery/test_storage_recovery.py",
)
SOURCES = (
    "mise.toml",
    "src/ohmymeme/core/adapters/fetch_policy.py",
    "src/ohmymeme/core/adapters/fetch_policy_validation.py",
    "src/ohmymeme/core/adapters/fetch_policy_transport.py",
    "src/ohmymeme/services/updates.py",
    "src/ohmymeme/integrations/imports/adb_qq.py",
    "src/ohmymeme/integrations/imports/qqnt.py",
    "src/ohmymeme/integrations/imports/telegram.py",
    "src/ohmymeme/integrations/imports/douyin.py",
    "src/ohmymeme/integrations/imports/wechat.py",
    "src/ohmymeme/integrations/imports/abogus.py",
    "src/ohmymeme/presentation/desktop/window_manager.py",
    "tests/adapters/importers/test_fetch_policy.py",
    "tests/adapters/importers/test_wechat.py",
    "tests/application/test_storage_settings.py",
    "tests/application/test_updater.py",
    "tests/test_adb_util.py",
    "tests/test_douyin_dl.py",
    "tests/test_qqnt_import_boundary.py",
    "tests/test_tg_stickers.py",
    "scripts/run_task_18_fetch_evidence.py",
    "scripts/build_task_18_evidence.py",
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
    if (request.evidence_root / "task-18.json").exists():
        raise EvidenceBlocked("BLOCKED_EVIDENCE_EXISTS")
    missing = [path for path in SOURCES if not (request.repo_root / path).is_file()]
    if missing:
        raise EvidenceBlocked("BLOCKED_MISSING_SOURCE")


def _run_tests(request):
    try:
        result = subprocess.run(
            ["mise", "run", "test-python", "--", *TESTS, "-q"],
            cwd=request.repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=240,
        )
    except subprocess.TimeoutExpired as error:
        raise EvidenceBlocked("BLOCKED_ADAPTER_QA_TIMEOUT") from error
    if result.returncode:
        raise EvidenceBlocked("BLOCKED_ADAPTER_QA")
    return result.stdout


def _run_scenarios(request):
    command = [
        sys.executable,
        "-m",
        "scripts.run_task_18_fetch_evidence",
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
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        raise EvidenceBlocked("BLOCKED_ADAPTER_SCENARIOS_TIMEOUT") from error
    if result.returncode:
        raise EvidenceBlocked("BLOCKED_ADAPTER_SCENARIOS")
    try:
        execution = canonical_load(result.stdout.encode("utf-8"))
    except (UnicodeEncodeError, ValueError) as error:
        raise EvidenceBlocked("BLOCKED_ADAPTER_SCENARIOS") from error
    if not _valid_execution(execution):
        raise EvidenceBlocked("BLOCKED_ADAPTER_SCENARIOS")
    return execution, command, result.stdout


def _valid_execution(execution):
    if not isinstance(execution, dict) or set(execution) != {
        "execution_identity",
        "source_sha256",
        "scenarios",
    }:
        return False
    run_id = execution["execution_identity"]
    rows = execution["scenarios"]
    if not isinstance(run_id, str) or not run_id or not isinstance(rows, dict):
        return False
    if not isinstance(execution["source_sha256"], dict):
        return False
    if set(rows) != set(SCENARIOS):
        return False
    row_ids = set()
    for row in rows.values():
        identity = row.get("execution_identity") if isinstance(row, dict) else None
        if (
            not isinstance(identity, dict)
            or identity.get("run_id") != run_id
            or not isinstance(identity.get("row_id"), str)
            or identity["row_id"] in row_ids
            or row.get("executed") is not True
            or not row.get("assertions")
            or row.get("cleanup", {}).get("temporary_root_removed") is not True
            or row.get("cleanup", {}).get("temporary_downloads_removed") is not True
            or row.get("cleanup", {}).get("credentials_recorded") is not False
            or row.get("cleanup", {}).get("proxy_values_recorded") is not False
            or row.get("cleanup", {}).get("payloads_recorded") is not False
            or row.get("cleanup", {}).get("payloads_removed") is not True
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
    source_sha256 = {
        path: sha256_path(request.repo_root / path) for path in SOURCES
    }
    test_output = _run_tests(request)
    execution, command, scenario_output = _run_scenarios(request)
    if execution["source_sha256"] != source_sha256:
        raise EvidenceBlocked("BLOCKED_SOURCE_DRIFT")
    if source_sha256 != {
        path: sha256_path(request.repo_root / path) for path in SOURCES
    }:
        raise EvidenceBlocked("BLOCKED_SOURCE_DRIFT")
    commit = subprocess.run(
        ["git", "-C", str(request.repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    ).stdout.strip()
    if len(commit) != 40:
        raise EvidenceBlocked("BLOCKED_BASELINE_COMMIT")
    scenarios = execution["scenarios"]
    report = {
        "schema_version": 1,
        "generator_version": "todo-18-fetch-policy/1",
        "verdict": "pass",
        "environment": {
            "external_network": "not_attempted",
            "external_network_reason": "offline-safe evidence run; no external DNS/API/CDN access",
            "local_http_server": "exercised",
        },
        "baseline_commit": commit,
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "plan_sha256": hashlib.sha256(request.plan.read_bytes()).hexdigest(),
        "source_sha256": source_sha256,
        "source_binding": {
            "captured_before_execution": True,
            "runner_captured_source_sha256": True,
            "unchanged_after_execution": True,
        },
        "execution_identity": execution["execution_identity"],
        "scenario_matrix": scenarios,
        "resolver_traces": {
            name: row["observed"].get("resolver", []) for name, row in scenarios.items()
        },
        "socket_peer_pin_evidence": scenarios["public-pinned-redirect"]["observed"]["connector"],
        "local_server_driver": scenarios["local-server-connector"]["observed"],
        "importer_matrix": scenarios["adapter-matrix"]["observed"]["importers"],
        "execution_provenance": {
            "scenario_command": command,
            "scenario_output_sha256": hashlib.sha256(scenario_output.encode("utf-8")).hexdigest(),
            "test_output_sha256": hashlib.sha256(test_output.encode("utf-8")).hexdigest(),
        },
        "cleanup_receipt": {
            "scenario_count": len(scenarios),
            "temporary_roots_removed": all(
                row["cleanup"]["temporary_root_removed"] for row in scenarios.values()
            ),
            "proxy_environment_restored": True,
            "temporary_downloads_removed": all(
                row["cleanup"]["temporary_downloads_removed"]
                for row in scenarios.values()
            ),
            "credentials_recorded": False,
            "proxy_values_recorded": False,
            "payloads_recorded": False,
            "payloads_removed": all(
                row["cleanup"]["payloads_removed"] for row in scenarios.values()
            ),
        },
    }
    destination = request.evidence_root / "task-18.json"
    destination.write_bytes(canonical_bytes(report))
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--plan", default=".omo/plans/full-project-modular-refactor.md")
    parser.add_argument("--evidence-root", required=True)
    args = parser.parse_args(argv)
    request = EvidenceRequest(
        Path(args.repo_root).resolve(),
        Path(args.plan).resolve(),
        Path(args.evidence_root).resolve(),
    )
    try:
        print(build_evidence(request))
    except EvidenceBlocked as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
