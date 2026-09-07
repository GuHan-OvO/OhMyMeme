import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts.build_task_16_evidence import EvidenceBlocked, EvidenceRequest, build_evidence
from scripts.baseline_contracts import canonical_bytes

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / ".omo" / "plans" / "full-project-modular-refactor.md"


def test_task_16_evidence_records_local_protocol_execution(tmp_path):
    # Given: an external evidence root and the approved loopback-only protocol plan
    request = EvidenceRequest(ROOT, PLAN, tmp_path)

    # When: the trusted local runner builds canonical evidence
    report_path = build_evidence(request)
    report = json.loads(report_path.read_text(encoding="utf-8"))

    # Then: the report contains real local profiles and no cloud success claim
    assert report["verdict"] == "pass"
    assert report["environment"] == {
        "network": "loopback-only",
        "production_endpoints": "not-attempted",
        "python": report["environment"]["python"],
    }
    assert set(report["backend_matrix"]) >= {
        "ftp",
        "ftps-control",
        "webdav",
        "webdav-https",
        "s3-sigv2-virtual",
        "s3-sigv2-path",
        "s3-sigv4-virtual",
        "s3-sigv4-path",
        "r2-region-auto",
    }
    assert report["backend_matrix"]["r2-region-auto"]["cloud_equivalence"] == "not-claimed"
    assert report["backend_matrix"]["ftps-control"]["tls_scope"] == "control-channel"
    assert report["limitations"]["ftps"] == (
        "control-channel TLS only; PROT P data-channel not enabled/verified"
    )
    assert report["backend_matrix"]["s3-sigv4-path"]["signature_verification"] == (
        "differential-only"
    )
    assert report["cleanup_receipt"]["production_endpoint_touched"] is False
    assert report["mutation_transcript"][-1]["event"] == "release"


def test_task_16_evidence_refuses_untrusted_runner(tmp_path, monkeypatch):
    # Given: a patched execution runner that cannot be treated as evidence
    from scripts import build_task_16_evidence

    monkeypatch.setattr(build_task_16_evidence, "_run_tests", lambda _request: "forged")

    # When/Then: the builder fails closed before writing a report
    with pytest.raises(EvidenceBlocked, match="BLOCKED_UNTRUSTED_EXECUTION_BOUNDARY"):
        build_evidence(EvidenceRequest(ROOT, PLAN, tmp_path))
    assert not (tmp_path / "task-16.json").exists()


def test_task_16_evidence_refuses_replacement(tmp_path):
    (tmp_path / "task-16.json").write_text("{}", encoding="utf-8")

    with pytest.raises(EvidenceBlocked, match="BLOCKED_EVIDENCE_EXISTS"):
        build_evidence(EvidenceRequest(ROOT, PLAN, tmp_path))


def test_task_16_evidence_binds_task16_test_and_preexecution_inputs(tmp_path):
    # Given: a newly generated external evidence directory
    request = EvidenceRequest(ROOT, PLAN, tmp_path)

    # When: the trusted builder emits a report
    report = json.loads(build_evidence(request).read_text(encoding="utf-8"))

    # Then: test16 and pre/post input bindings are explicit and unchanged
    binding = report["input_binding"]
    assert "tests/test_task_16_evidence.py" in binding["test_sha256_before"]
    assert binding["source_sha256_before"] == binding["source_sha256_after"]
    assert binding["test_sha256_before"] == binding["test_sha256_after"]
    assert binding["runner_identity"]["sha256"]


def test_task_16_evidence_rejects_input_changed_during_execution(tmp_path, monkeypatch):
    # Given: a trusted subprocess runner that changes a bound test file
    from scripts import build_task_16_evidence

    original = build_task_16_evidence._run_isolated
    target = ROOT / "tests/test_task_16_evidence.py"
    before = target.read_bytes()

    def mutate_then_run(command, cwd, timeout):
        if command[:3] == ["mise", "run", "test-python"]:
            target.write_bytes(before + b"\n")
        return original(command, cwd, timeout)

    monkeypatch.setattr(build_task_16_evidence, "_run_isolated", mutate_then_run)
    try:
        with pytest.raises(EvidenceBlocked, match="BLOCKED_INPUT_CHANGED"):
            build_evidence(EvidenceRequest(ROOT, PLAN, tmp_path))
    finally:
        target.write_bytes(before)


def test_task_16_evidence_rejects_forged_subprocess_payload(tmp_path, monkeypatch):
    # Given: a complete-looking pass payload returned by a patched subprocess.run
    from scripts import build_task_16_evidence
    from scripts.local_remote_contracts import REQUIRED_PROFILES

    profiles = {
        name: {"status": "pass"} for name in REQUIRED_PROFILES
    }
    profiles["coordinator"] = {
        "status": "pass",
        "transcript": [{"event": "release"}],
    }
    profiles["ftps-control"] = {"status": "pass", "tls_scope": "control-channel"}
    payload = {
        "schema_version": 1,
        "verdict": "pass",
        "profiles": profiles,
        "environment": {
            "network": "loopback-only",
            "production_endpoints": "not-attempted",
        },
        "limitations": {
            "ftps": "control-channel TLS only; PROT P data-channel not enabled/verified",
            "s3-signatures": "differential-only; request shape is observed but canonical cryptography is not independently verified",
        },
        "authentication": {
            "status": "pass",
            "ftp": {"rejected": True, "mutations": 0},
            "webdav": {"rejected": True, "mutations": 0},
            "s3": {"rejected": True, "mutations": 0},
        },
        "optional_servers": {},
        "cleanup": {"temporary_root_removed": True},
    }
    assert build_task_16_evidence._valid_execution(payload)

    def forged_run(command, **_kwargs):
        if command[0:3] == ["mise", "run", "test-python"]:
            return SimpleNamespace(returncode=0, stdout="forged-tests", stderr="")
        if command[0:3] == ["mise", "run", "test-remote-contracts-local"]:
            return SimpleNamespace(
                returncode=0,
                stdout=canonical_bytes(payload).decode("utf-8"),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="0" * 40, stderr="")

    monkeypatch.setattr(build_task_16_evidence.subprocess, "run", forged_run)

    # When/Then: same-process forged execution cannot produce evidence
    with pytest.raises(EvidenceBlocked, match="BLOCKED_UNTRUSTED_EXECUTION_BOUNDARY"):
        build_evidence(EvidenceRequest(ROOT, PLAN, tmp_path))
    assert not (tmp_path / "task-16.json").exists()
