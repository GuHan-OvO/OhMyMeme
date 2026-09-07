"""Build external, source-bound evidence for the pywebview bridge contract."""

import argparse
import hashlib
import inspect
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from subprocess import Popen
from uuid import uuid4

from scripts.baseline_contracts import (
    canonical_bytes,
    canonical_load,
    is_reparse_or_symlink,
    sha256_path,
)

SOURCES = (
    "mise.toml",
    "requirements.txt",
    "package.json",
    "package-lock.json",
    "scripts/generate_bridge_schemas.py",
    "scripts/build_task_19_evidence.py",
    "scripts/run_task_19_bridge_matrix.py",
    "src/ohmymeme/core/schemas/bridge.py",
    "src/ohmymeme/presentation/desktop/api/facade_base.py",
    "src/ohmymeme/presentation/desktop/api/main_facade.py",
    "src/ohmymeme/presentation/desktop/api/settings_facade.py",
    "src/ohmymeme/presentation/desktop/api/pywebview_adapter.py",
    "src/ohmymeme/presentation/desktop/window_manager.py",
    "schemas/bridge/bridge.schema.json",
    "src/ohmymeme/presentation/frontend/main/shared/generated/bridge.ts",
    "src/ohmymeme/presentation/frontend/settings/shared/generated/bridge.ts",
    "src/ohmymeme/presentation/frontend/settings/shared/decoder.js",
    "tests/contracts/test_pywebview_bridge.py",
    "tests/frontend/bridge_decoder.test.js",
)
SCHEMA_SOURCE = "schemas/bridge/bridge.schema.json"
MAIN_DTO_SOURCE = "src/ohmymeme/presentation/frontend/main/shared/generated/bridge.ts"
SETTINGS_DTO_SOURCE = (
    "src/ohmymeme/presentation/frontend/settings/shared/generated/bridge.ts"
)
TESTS = (
    "tests/contracts/test_pywebview_bridge.py",
    "tests/application/test_library_bridge.py",
    "tests/test_startup.py",
)
FRONTEND_TESTS = ("tests/frontend/bridge_decoder.test.js",)
ADVERSARIAL_SCENARIOS = (
    "save-settings-valid",
    "save-settings-unknown",
    "malformed-search",
    "malformed-init",
    "boolean-and-void",
    "runtime-error-mapping",
    "schema-root",
)
BOUND_HASH_PATHS = (
    "src/ohmymeme/core/schemas/bridge.py",
    "src/ohmymeme/presentation/desktop/api/facade_base.py",
    "src/ohmymeme/presentation/desktop/api/main_facade.py",
    "src/ohmymeme/presentation/desktop/api/settings_facade.py",
    "src/ohmymeme/presentation/frontend/settings/shared/decoder.js",
    SCHEMA_SOURCE,
    MAIN_DTO_SOURCE,
    SETTINGS_DTO_SOURCE,
    "tests/contracts/test_pywebview_bridge.py",
    "tests/frontend/bridge_decoder.test.js",
)


class EvidenceBlocked(RuntimeError):
    """Raised when evidence cannot truthfully be materialized."""


def _validate(repo_root: Path, plan: Path, evidence_root: Path) -> None:
    if (
        not evidence_root.is_dir()
        or evidence_root.is_relative_to(repo_root)
        or is_reparse_or_symlink(evidence_root)
    ):
        raise EvidenceBlocked("BLOCKED_INVALID_EVIDENCE_ROOT")
    if not plan.is_file():
        raise EvidenceBlocked("BLOCKED_MISSING_PLAN")
    if (evidence_root / "task-19.json").exists():
        raise EvidenceBlocked("BLOCKED_EVIDENCE_EXISTS")
    if any(not (repo_root / source).is_file() for source in SOURCES):
        raise EvidenceBlocked("BLOCKED_MISSING_SOURCE")


def _run(command: list[str], repo_root: Path) -> str:
    result = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=240,
    )
    if result.returncode:
        raise EvidenceBlocked("BLOCKED_BRIDGE_QA")
    return result.stdout


def _valid_adversarial_execution(
    raw_output: bytes, challenge: str, repo_root: Path
) -> bool:
    """Validate only bytes captured from the isolated child process."""
    if not isinstance(raw_output, bytes):
        return False
    try:
        execution = canonical_load(raw_output.rstrip(b"\r\n"))
    except (UnicodeDecodeError, ValueError):
        return False
    if set(execution) != {
        "challenge",
        "execution_identity",
        "scenarios",
        "hashes_before",
        "hashes_after",
        "cleanup",
        "runner_sha256",
        "verdict",
    }:
        return False
    run_id = execution["execution_identity"]
    scenarios = execution["scenarios"]
    if (
        not isinstance(execution["challenge"], str)
        or not execution["challenge"]
        or not isinstance(run_id, str)
        or not run_id
        or not isinstance(scenarios, dict)
        or set(scenarios) != set(ADVERSARIAL_SCENARIOS)
        or not isinstance(execution["hashes_before"], dict)
        or not isinstance(execution["hashes_after"], dict)
        or execution["hashes_before"] != execution["hashes_after"]
        or set(execution["hashes_before"]) != set(BOUND_HASH_PATHS)
        or not isinstance(execution["cleanup"], dict)
        or set(execution["cleanup"])
        != {
            "temporary_root_created",
            "temporary_root_removed",
            "marker_removed",
            "socket_closed",
        }
        or not all(value is True for value in execution["cleanup"].values())
        or not isinstance(execution["runner_sha256"], str)
        or len(execution["runner_sha256"]) != 64
        or execution["verdict"] != "pass"
    ):
        return False
    row_ids = set()
    for row in scenarios.values():
        if not isinstance(row, dict):
            return False
        identity = row.get("execution_identity")
        if (
            not isinstance(identity, dict)
            or identity.get("run_id") != run_id
            or not isinstance(identity.get("row_id"), str)
            or identity["row_id"] in row_ids
            or row.get("executed") is not True
            or row.get("status") != "pass"
            or not row.get("assertions")
        ):
            return False
        row_ids.add(identity["row_id"])
    expected_runner_hash = hashlib.sha256(
        (repo_root / "scripts/run_task_19_bridge_matrix.py").read_bytes()
    ).hexdigest()
    return (
        len(row_ids) == len(scenarios)
        and execution["challenge"] == challenge
        and execution["runner_sha256"] == expected_runner_hash
    )


def _decode_adversarial_execution(
    raw_output: bytes, challenge: str, repo_root: Path
) -> dict:
    """Decode and validate an isolated child output for report projection."""
    if not _valid_adversarial_execution(raw_output, challenge, repo_root):
        raise EvidenceBlocked("BLOCKED_ADVERSARIAL_MATRIX")
    return canonical_load(raw_output.rstrip(b"\r\n"))


def _abi_matrix() -> dict[str, dict[str, str]]:
    from ohmymeme.presentation.desktop.window_manager import JsApi, SettingsApi

    return {
        public_name: {
            name: str(inspect.signature(getattr(class_, name)))
            for name in sorted(
                name
                for name in dir(class_)
                if not name.startswith("_") and callable(getattr(class_, name))
            )
        }
        for public_name, class_ in (("JsApi", JsApi), ("SettingsApi", SettingsApi))
    }


def build_evidence(repo_root: Path, plan: Path, evidence_root: Path) -> Path:
    """Execute bridge checks and write one canonical, external report."""
    _validate(repo_root, plan, evidence_root)
    before = {source: sha256_path(repo_root / source) for source in SOURCES}
    generation_output = _run(
        ["mise", "run", "generate-schemas", "--", "--check"], repo_root
    )
    test_output = _run(["mise", "run", "test-python", "--", *TESTS, "-q"], repo_root)
    frontend_output = _run(
        ["mise", "run", "test-frontend", "--", *FRONTEND_TESTS], repo_root
    )
    challenge = uuid4().hex
    command = [sys.executable, "-m", "scripts.run_task_19_bridge_matrix"]
    environment = dict(os.environ)
    environment["OHMYMEME_TASK19_CHALLENGE"] = challenge
    with tempfile.TemporaryDirectory(prefix="ohmymeme-task19-runner-") as directory:
        output_path = Path(directory) / "stdout"
        error_path = Path(directory) / "stderr"
        with output_path.open("wb") as output_file, error_path.open("wb") as error_file:
            process = Popen(
                command,
                cwd=repo_root,
                env=environment,
                stdout=output_file,
                stderr=error_file,
            )
            try:
                return_code = process.wait(timeout=240)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait()
                raise EvidenceBlocked("BLOCKED_ADVERSARIAL_MATRIX") from error
        if return_code:
            raise EvidenceBlocked("BLOCKED_ADVERSARIAL_MATRIX")
        adversarial_output = output_path.read_bytes()
    adversarial = _decode_adversarial_execution(
        adversarial_output, challenge, repo_root
    )
    runner_exited = return_code is not None
    after = {source: sha256_path(repo_root / source) for source in SOURCES}
    if before != after:
        raise EvidenceBlocked("BLOCKED_SOURCE_DRIFT")
    if any(
        before.get(path) != adversarial["hashes_before"].get(path)
        for path in BOUND_HASH_PATHS
    ) or any(
        after.get(path) != adversarial["hashes_after"].get(path)
        for path in BOUND_HASH_PATHS
    ):
        raise EvidenceBlocked("BLOCKED_SOURCE_DRIFT")
    report = {
        "schema_version": 1,
        "generator_version": "todo-19-bridge-contract/1",
        "verdict": adversarial["verdict"],
        "baseline_commit": _run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"], repo_root
        ).strip(),
        "generated_at_utc": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "evidence_files": [{"scope": "evidence_root", "path": "task-19.json"}],
        "source_sha256": before,
        "source_hash_binding": {
            "before": {path: before[path] for path in BOUND_HASH_PATHS},
            "during_before": adversarial["hashes_before"],
            "during_after": adversarial["hashes_after"],
            "after": {path: after[path] for path in BOUND_HASH_PATHS},
        },
        "schema_sha256": hashlib.sha256(
            (repo_root / SCHEMA_SOURCE).read_bytes()
        ).hexdigest(),
        "schema_hashes": {
            SCHEMA_SOURCE: hashlib.sha256(
                (repo_root / SCHEMA_SOURCE).read_bytes()
            ).hexdigest()
        },
        "generated_dto_sha256": {
            "main": hashlib.sha256(
                (repo_root / MAIN_DTO_SOURCE).read_bytes()
            ).hexdigest(),
            "settings": hashlib.sha256(
                (repo_root / SETTINGS_DTO_SOURCE).read_bytes()
            ).hexdigest(),
        },
        "generated_diff_proof": {
            "command": ["mise", "run", "generate-schemas", "--", "--check"],
            "stdout_sha256": hashlib.sha256(
                generation_output.encode("utf-8")
            ).hexdigest(),
            "matched": all(
                before[path] == after[path]
                for path in (SCHEMA_SOURCE, MAIN_DTO_SOURCE, SETTINGS_DTO_SOURCE)
            ),
        },
        "generated_file_diff_proof": {
            "matched": all(
                before[path] == after[path]
                for path in (SCHEMA_SOURCE, MAIN_DTO_SOURCE, SETTINGS_DTO_SOURCE)
            )
        },
        "abi_matrix": _abi_matrix(),
        "adversarial_matrix": adversarial["scenarios"],
        "adversarial_provenance": {
            "command": command,
            "execution_identity": adversarial["execution_identity"],
            "stdout_sha256": hashlib.sha256(
                adversarial_output
            ).hexdigest(),
            "runner_sha256": hashlib.sha256(
                (repo_root / "scripts/run_task_19_bridge_matrix.py").read_bytes()
            ).hexdigest(),
        },
        "verification": {
            "command": ["mise", "run", "test-python", "--", *TESTS, "-q"],
            "stdout_sha256": hashlib.sha256(test_output.encode("utf-8")).hexdigest(),
            "frontend_command": ["mise", "run", "test-frontend", "--", *FRONTEND_TESTS],
            "frontend_stdout_sha256": hashlib.sha256(
                frontend_output.encode("utf-8")
            ).hexdigest(),
        },
        "cleanup_receipt": {
            **adversarial["cleanup"],
            "runner_process_exited": runner_exited,
            "generated_files_unchanged": (
                before[SCHEMA_SOURCE] == after[SCHEMA_SOURCE]
                and before[MAIN_DTO_SOURCE] == after[MAIN_DTO_SOURCE]
                and before[SETTINGS_DTO_SOURCE] == after[SETTINGS_DTO_SOURCE]
            ),
            "generated_files_hand_edited": not (
                before[SCHEMA_SOURCE] == after[SCHEMA_SOURCE]
                and before[MAIN_DTO_SOURCE] == after[MAIN_DTO_SOURCE]
                and before[SETTINGS_DTO_SOURCE] == after[SETTINGS_DTO_SOURCE]
            ),
        },
    }
    destination = evidence_root / "task-19.json"
    destination.write_bytes(canonical_bytes(report))
    return destination


def main() -> int:
    """Run the external evidence generator."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--plan", default=".omo/plans/full-project-modular-refactor.md")
    parser.add_argument("--evidence-root", required=True)
    args = parser.parse_args()
    try:
        print(
            build_evidence(
                Path(args.repo_root).resolve(),
                Path(args.plan).resolve(),
                Path(args.evidence_root).resolve(),
            )
        )
    except (EvidenceBlocked, subprocess.TimeoutExpired) as error:
        print(error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
