from pathlib import Path

import pytest

from scripts.baseline_contracts import canonical_load
from scripts.build_task_19_evidence import EvidenceBlocked, build_evidence

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / ".omo/plans/full-project-modular-refactor.md"


def test_task_19_evidence_binds_generated_schema_abi_and_cleanup(tmp_path):
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()

    report = canonical_load(build_evidence(ROOT, PLAN, evidence_root).read_bytes())

    assert report["verdict"] == "pass"
    assert set(report["abi_matrix"]) == {"JsApi", "SettingsApi"}
    assert report["schema_hashes"]["schemas/bridge/bridge.schema.json"]
    assert (
        report["generated_dto_sha256"]["main"]
        == report["generated_dto_sha256"]["settings"]
    )
    assert report["generated_diff_proof"]["matched"] is True
    assert all(row["executed"] for row in report["adversarial_matrix"].values())
    binding = report["source_hash_binding"]
    assert set(binding) == {"before", "during_before", "during_after", "after"}
    assert binding["before"] == binding["during_before"]
    assert binding["during_before"] == binding["during_after"]
    assert binding["during_after"] == binding["after"]
    assert report["cleanup_receipt"] == {
        "temporary_root_created": True,
        "temporary_root_removed": True,
        "marker_removed": True,
        "socket_closed": True,
        "runner_process_exited": True,
        "generated_files_unchanged": True,
        "generated_files_hand_edited": False,
    }


def test_task_19_evidence_refuses_replacement(tmp_path):
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    (evidence_root / "task-19.json").write_text("{}", encoding="utf-8")

    with pytest.raises(EvidenceBlocked, match="BLOCKED_EVIDENCE_EXISTS"):
        build_evidence(ROOT, PLAN, evidence_root)


def test_task_19_evidence_rejects_static_adversarial_matrix(tmp_path):
    import scripts.build_task_19_evidence as evidence

    forged = {"execution_identity": "forged", "scenarios": {}}
    assert evidence._valid_adversarial_execution(forged, "challenge", ROOT) is False


def test_task_19_evidence_rejects_fabricated_runner_output(tmp_path):
    import scripts.build_task_19_evidence as evidence

    source = (ROOT / "scripts/build_task_19_evidence.py").read_text(encoding="utf-8")
    assert "_TRUSTED_ADVERSARIAL_RUNNER" not in source
    assert "_REAL_ADVERSARIAL_RUNNER" not in source


def test_task_19_evidence_has_no_matrix_object_injection_boundary():
    source = (
        ROOT / "scripts/build_task_19_evidence.py"
    ).read_text(encoding="utf-8")
    assert "_execution_projection" not in source
    assert "trusted_result" not in source
