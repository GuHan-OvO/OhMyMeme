"""Build canonical external evidence for the Todo 6 package smoke contracts."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scripts.baseline_contracts import canonical_bytes, canonical_load, is_reparse_or_symlink, sha256_path
from scripts.package_smoke_inputs import PackageInputViolation, target_matrix


GENERATOR_VERSION = "todo-6-package-smoke/1"
SOURCE_FILES = (
    "mise.lock",
    "requirements.txt",
    "scripts/package_lifecycle.py",
    "scripts/package_smoke.py",
    "scripts/package_smoke_inputs.py",
    "scripts/build_task_6_evidence.py",
    "scripts/installer/windows.iss",
    "scripts/installer/macos/Info.plist",
    "scripts/installer/linux/build.sh",
    ".github/workflows/build.yml",
    ".github/workflows/nightly.yml",
)


class EvidenceBlocked(RuntimeError):
    """Todo 6 evidence cannot prove a release-smoke input."""


@dataclass(frozen=True)
class EvidenceRequest:
    """External paths needed to create one Todo 6 evidence report."""

    repo_root: Path
    plan: Path
    release_catalog: Path
    evidence_root: Path


def timestamp():
    """Return the evidence timestamp in the shared UTC format."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_request(request):
    """Reject mutable, in-tree, or incomplete evidence destinations."""
    if (
        not request.evidence_root.is_dir()
        or request.evidence_root.is_relative_to(request.repo_root)
        or is_reparse_or_symlink(request.evidence_root)
    ):
        raise EvidenceBlocked("BLOCKED_INVALID_EVIDENCE_ROOT")
    if not request.plan.is_file() or not request.release_catalog.is_file():
        raise EvidenceBlocked("BLOCKED_MISSING_EVIDENCE_INPUT")
    if (request.evidence_root / "task-6.json").exists():
        raise EvidenceBlocked("BLOCKED_EVIDENCE_EXISTS")


def run_contract_smoke(request):
    """Run the public contract-only surface without executing lifecycle commands."""
    try:
        result = subprocess.run(
            ["mise", "run", "package-smoke", "--", "--contract-only"],
            cwd=request.repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        raise EvidenceBlocked("BLOCKED_CONTRACT_SMOKE_TIMEOUT") from error
    if result.returncode:
        raise EvidenceBlocked("BLOCKED_CONTRACT_SMOKE")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise EvidenceBlocked("BLOCKED_INVALID_CONTRACT_REPORT") from error
    if not isinstance(report, dict):
        raise EvidenceBlocked("BLOCKED_INVALID_CONTRACT_REPORT")
    return report


def load_catalog(request):
    """Load the canonical Todo 2 source that supplies immutable rollback inputs."""
    try:
        catalog = canonical_load(request.release_catalog.read_bytes())
    except (OSError, ValueError) as error:
        raise EvidenceBlocked("BLOCKED_INVALID_RELEASE_CATALOG") from error
    if not isinstance(catalog, dict) or catalog.get("verdict") != "pass":
        raise EvidenceBlocked("BLOCKED_INVALID_RELEASE_CATALOG")
    releases = catalog.get("releases")
    if not isinstance(releases, list) or len(releases) < 2:
        raise EvidenceBlocked("BLOCKED_MISSING_RELEASE_ASSET")
    return catalog


def build_evidence(request):
    """Create one canonical contract-only evidence document outside the worktree."""
    ensure_request(request)
    catalog = load_catalog(request)
    report = run_contract_smoke(request)
    try:
        matrix = target_matrix(report, catalog)
    except PackageInputViolation as error:
        raise EvidenceBlocked(str(error)) from error
    commit_result = subprocess.run(
        ["git", "-C", str(request.repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    commit = commit_result.stdout.strip() if commit_result.returncode == 0 else ""
    if re.fullmatch(r"[0-9a-f]{40}", commit or "") is None:
        raise EvidenceBlocked("BLOCKED_BASELINE_COMMIT")
    status_result = subprocess.run(
        ["git", "-C", str(request.repo_root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    worktree_state = (
        "unavailable"
        if status_result.returncode
        else "dirty" if status_result.stdout else "clean"
    )
    output = {
        "schema_version": 1,
        "plan_sha256": hashlib.sha256(request.plan.read_bytes()).hexdigest(),
        "baseline_commit": commit,
        "generated_at_utc": timestamp(),
        "generator_version": GENERATOR_VERSION,
        "verdict": "pass",
        "evidence_files": [
            {"scope": "evidence_root", "path": "task-6.json"},
            {"scope": "evidence_root", "path": request.release_catalog.name},
        ],
        "release_catalog_sha256": hashlib.sha256(request.release_catalog.read_bytes()).hexdigest(),
        "contract_report_sha256": hashlib.sha256(canonical_bytes(report)).hexdigest(),
        "source_provenance": {"worktree_state": worktree_state},
        "source_sha256": {path: sha256_path(request.repo_root / path) for path in SOURCE_FILES},
        "target_matrix": matrix,
        "manual_inspection": {
            "command": "mise run package-smoke -- --contract-only",
            "contract_only": report.get("contract_only") is True,
            "target_count": len(matrix),
            "artifact_execution": "not-run",
        },
        "cleanup_receipt": {
            "artifact_execution": "not-run",
            "temporary_workspace": "not-created",
            "lifecycle_cleanup": {
                row["target"]: [probe["cleanup"] for probe in row["lifecycle_probes"]]
                for row in matrix
            },
        },
    }
    report_path = request.evidence_root / "task-6.json"
    report_path.write_bytes(canonical_bytes(output))
    return report_path


def main(argv=None):
    """Parse the evidence command and print its one created report path."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--plan", default=".omo/plans/full-project-modular-refactor.md")
    parser.add_argument("--release-catalog", required=True)
    parser.add_argument("--evidence-root", required=True)
    args = parser.parse_args(argv)
    request = EvidenceRequest(
        Path(args.repo_root).resolve(),
        Path(args.plan).resolve(),
        Path(args.release_catalog).resolve(),
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
