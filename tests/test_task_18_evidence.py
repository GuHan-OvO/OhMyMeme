from pathlib import Path

import pytest

from scripts.baseline_contracts import canonical_load
from scripts.build_task_18_evidence import EvidenceBlocked, EvidenceRequest, build_evidence

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / ".omo/plans/full-project-modular-refactor.md"


def test_evidence_records_fetch_traces_importers_and_cleanup(tmp_path):
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()

    report = canonical_load(
        build_evidence(EvidenceRequest(ROOT, PLAN, evidence_root)).read_bytes()
    )

    assert report["verdict"] == "pass"
    assert report["environment"] == {
        "external_network": "not_attempted",
        "external_network_reason": "offline-safe evidence run; no external DNS/API/CDN access",
        "local_http_server": "exercised",
    }
    assert set(report["scenario_matrix"]) == {
        "public-pinned-redirect",
        "unsafe-address-set",
        "local-server-unsafe-dns",
        "local-server-connector",
        "proxy-bypass",
        "trusted-cdn",
        "oversize",
        "bad-image",
        "adapter-matrix",
    }
    assert report["cleanup_receipt"]["temporary_roots_removed"] is True
    assert report["cleanup_receipt"]["proxy_environment_restored"] is True
    assert report["cleanup_receipt"]["temporary_downloads_removed"] is True
    assert report["cleanup_receipt"]["credentials_recorded"] is False
    assert report["cleanup_receipt"]["proxy_values_recorded"] is False
    assert report["cleanup_receipt"]["payloads_recorded"] is False
    assert report["cleanup_receipt"]["payloads_removed"] is True
    assert report["source_binding"] == {
        "captured_before_execution": True,
        "runner_captured_source_sha256": True,
        "unchanged_after_execution": True,
    }
    assert (
        report["scenario_matrix"]["local-server-unsafe-dns"]["observed"][
            "local_server_requests"
        ]
        == 0
    )
    assert report["socket_peer_pin_evidence"][0]["peer"] == "93.184.216.34"
    assert report["socket_peer_pin_evidence"][1]["peer"] == "93.184.216.35"
    assert report["importer_matrix"]["wechat_image"] is True
    assert report["local_server_driver"]["local_server_requests"] == 1


def test_evidence_rejects_existing_report(tmp_path):
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    (evidence_root / "task-18.json").write_text("{}", encoding="utf-8")

    with pytest.raises(EvidenceBlocked, match="BLOCKED_EVIDENCE_EXISTS"):
        build_evidence(EvidenceRequest(ROOT, PLAN, evidence_root))


def test_evidence_rejects_patched_execution_runner(tmp_path, monkeypatch):
    from scripts import build_task_18_evidence

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    monkeypatch.setattr(build_task_18_evidence, "_run_scenarios", lambda _request: {})

    with pytest.raises(EvidenceBlocked, match="BLOCKED_UNTRUSTED_EXECUTION_BOUNDARY"):
        build_task_18_evidence.build_evidence(
            EvidenceRequest(ROOT, PLAN, evidence_root)
        )
    assert not (evidence_root / "task-18.json").exists()
