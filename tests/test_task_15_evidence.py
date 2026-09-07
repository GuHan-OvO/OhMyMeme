from pathlib import Path

import pytest

from scripts.baseline_contracts import canonical_load
from scripts.build_task_15_evidence import EvidenceBlocked, EvidenceRequest, build_evidence


ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / ".omo/plans/full-project-modular-refactor.md"


def test_evidence_records_live_unique_import_scenarios(tmp_path):
    # Given: an external empty evidence root
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()

    # When: Todo15 builds its owned live record
    report = canonical_load(
        build_evidence(EvidenceRequest(ROOT, PLAN, evidence_root)).read_bytes()
    )

    # Then: every scenario is execution-bound, observed and source-bound
    assert report["verdict"] == "pass"
    assert set(report["scenario_matrix"]) == {
        "bytes-path-batch",
        "malformed-stg3",
        "cancelled",
        "qqnt-cache",
        "sync-legacy-worker",
        "sync-production-cancel",
    }
    row_ids = {
        row["execution_identity"]["row_id"]
        for row in report["scenario_matrix"].values()
    }
    assert len(row_ids) == 6
    assert all(
        row["execution_identity"]["run_id"] == report["execution_identity"]
        and row["executed"] is True
        and row["assertions"]
        and row["cleanup"]["temporary_root_removed"] is True
        for row in report["scenario_matrix"].values()
    )
    assert len(report["execution_provenance"]["test_output_sha256"]) == 64
    assert len(report["execution_provenance"]["scenario_output_sha256"]) == 64
    assert len(report["source_sha256"]) >= 10


def test_evidence_rejects_patched_or_stale_execution_boundary(tmp_path, monkeypatch):
    # Given: a builder whose live scenario runner was replaced by static data
    from scripts import build_task_15_evidence

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    monkeypatch.setattr(
        build_task_15_evidence,
        "_run_scenarios",
        lambda _request: ({"execution_identity": "stale", "scenarios": {}}, [], "{}"),
    )

    # When/Then: the trusted execution identity rejects the static substitute
    with pytest.raises(EvidenceBlocked, match="BLOCKED_UNTRUSTED_EXECUTION_BOUNDARY"):
        build_evidence(EvidenceRequest(ROOT, PLAN, evidence_root))
    assert not (evidence_root / "task-15.json").exists()


def test_evidence_rejects_existing_report(tmp_path):
    # Given: a report path already occupied by stale evidence
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    (evidence_root / "task-15.json").write_text("{}", encoding="utf-8")

    # When/Then: replacement is fail-closed
    with pytest.raises(EvidenceBlocked, match="BLOCKED_EVIDENCE_EXISTS"):
        build_evidence(EvidenceRequest(ROOT, PLAN, evidence_root))
