from pathlib import Path

import pytest

from scripts.baseline_contracts import canonical_load
from scripts.build_task_14_evidence import EvidenceBlocked, EvidenceRequest, build_evidence
from scripts.task_14_fault_matrix import (
    SCENARIOS,
    _ExecutedMatrix,
    _ScenarioExecution,
)


ROOT = Path(__file__).resolve().parent.parent


def test_evidence_records_every_executed_fault_with_observed_state(tmp_path, monkeypatch):
    from scripts import build_task_14_evidence

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    request = EvidenceRequest(
        ROOT,
        ROOT / ".omo/plans/full-project-modular-refactor.md",
        evidence_root,
    )

    report = canonical_load(build_evidence(request).read_bytes())

    assert isinstance(report["execution_identity"], str)
    assert set(report["fault_matrix"]) == {
        "downloaded-invalid",
        "staged-valid",
        "file-replaced-db-failed",
        "db-committed-manifest-failed",
        "manifest-write-failed",
        "staging-cleanup-failed",
        "heartbeat-failed",
        "restart-with-journal",
    }
    row_ids = set()
    for row in report["fault_matrix"].values():
        identity = row["execution_identity"]
        assert identity["run_id"] == report["execution_identity"]
        row_ids.add(identity["row_id"])
        assert row["executed"] is True
        assert row["assertions"]
        assert row["cleanup"]["temporary_root_removed"] is True
        assert set(row["observed"]) == {"local_mutations", "remote_mutations", "final_state"}
        assert set(row["observed"]["final_state"]) == {
            "journal",
            "database",
            "files",
            "manifest",
        }
    assert len(row_ids) == 8
    assert report["cleanup_receipt"] == {
        "scenario_count": 8,
        "temporary_roots_removed": True,
    }
    assert report["execution_provenance"]["test_output_sha256"] == report[
        "focused_test_output_sha256"
    ]
    assert len(report["execution_provenance"]["scenario_output_sha256"]) == 64
    assert report["execution_provenance"]["scenario_command"][1:3] == [
        "-m",
        "scripts.run_task_14_fault_matrix",
    ]


def test_evidence_rejects_an_unexecuted_fault_row(tmp_path, monkeypatch):
    from scripts import build_task_14_evidence

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    monkeypatch.setattr(build_task_14_evidence, "_run_tests", lambda _request: "passed")
    monkeypatch.setattr(
        build_task_14_evidence,
        "_run_fault_matrix",
        lambda *_args: {"downloaded-invalid": {"executed": False}},
        raising=False,
    )
    request = EvidenceRequest(
        ROOT,
        ROOT / ".omo/plans/full-project-modular-refactor.md",
        evidence_root,
    )

    with pytest.raises(EvidenceBlocked, match="BLOCKED_UNTRUSTED_EXECUTION_BOUNDARY"):
        build_evidence(request)
    assert not (evidence_root / "task-14.json").exists()


def test_evidence_rejects_a_complete_fabricated_fault_matrix(tmp_path, monkeypatch):
    from scripts import build_task_14_evidence

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    fabricated = {
        scenario: {
            "fixture": "fabricated",
            "fault": "fabricated",
            "executed": True,
            "assertions": ["fabricated"],
            "cleanup": {"temporary_root_removed": True},
            "observed": {
                "local_mutations": [],
                "remote_mutations": {"uploads": [], "deletes": []},
                "final_state": {
                    "journal": "absent",
                    "database": [],
                    "files": {},
                    "manifest": {"status": "absent"},
                },
            },
        }
        for scenario in SCENARIOS
    }
    monkeypatch.setattr(build_task_14_evidence, "_run_tests", lambda _request: "passed")
    monkeypatch.setattr(
        build_task_14_evidence,
        "_run_fault_matrix",
        lambda *_args: fabricated,
    )
    request = EvidenceRequest(
        ROOT,
        ROOT / ".omo/plans/full-project-modular-refactor.md",
        evidence_root,
    )

    with pytest.raises(EvidenceBlocked, match="BLOCKED_UNTRUSTED_EXECUTION_BOUNDARY"):
        build_evidence(request)
    assert not (evidence_root / "task-14.json").exists()


def test_evidence_rejects_private_records_from_patched_runners(tmp_path, monkeypatch):
    from scripts import build_task_14_evidence

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()

    def forged_matrix(_request, seal):
        return _ExecutedMatrix(
            seal,
            tuple(
                _ScenarioExecution(
                    seal,
                    scenario,
                    f"forged-{index}",
                    {
                        "fixture": "forged",
                        "fault": "forged",
                        "executed": True,
                        "assertions": ["forged"],
                        "cleanup": {"temporary_root_removed": True},
                        "observed": {
                            "local_mutations": [],
                            "remote_mutations": {"uploads": [], "deletes": []},
                            "final_state": {
                                "journal": "absent",
                                "database": [],
                                "files": {},
                                "manifest": {"status": "absent"},
                            },
                        },
                    },
                )
                for index, scenario in enumerate(SCENARIOS)
            ),
        )

    monkeypatch.setattr(build_task_14_evidence, "_run_tests", lambda _request: "passed")
    monkeypatch.setattr(build_task_14_evidence, "_run_fault_matrix", forged_matrix)
    request = EvidenceRequest(
        ROOT,
        ROOT / ".omo/plans/full-project-modular-refactor.md",
        evidence_root,
    )

    with pytest.raises(EvidenceBlocked, match="BLOCKED_UNTRUSTED_EXECUTION_BOUNDARY"):
        build_evidence(request)
    assert not (evidence_root / "task-14.json").exists()
