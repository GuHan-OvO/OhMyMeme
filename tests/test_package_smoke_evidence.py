"""Todo 6 外部发布 smoke 证据测试。"""

import hashlib
import subprocess
from pathlib import Path

from ohmymeme import __version__
from scripts.baseline_contracts import canonical_bytes, canonical_load
from scripts.build_task_6_evidence import EvidenceRequest, build_evidence
from scripts.package_smoke import contract_report


ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / ".omo" / "plans" / "full-project-modular-refactor.md"


def _assets(version):
    """为一个稳定版本生成六类具有 SHA-256 的原始资产目录。"""
    targets = (
        ("windows-x86_64", f"OhMyMeme-{version}-setup.exe"),
        ("linux-x86_64-appimage", f"OhMyMeme-v{version}-x86_64.AppImage"),
        ("linux-x86_64-deb", f"OhMyMeme-v{version}-amd64.deb"),
        ("linux-x86_64-rpm", f"OhMyMeme-v{version}-x86_64.rpm"),
        ("macos-arm64", f"OhMyMeme-v{version}-arm64.dmg"),
        ("macos-x86_64", f"OhMyMeme-v{version}-x86_64.dmg"),
    )
    return [
        {
            "platform": platform,
            "asset_name": filename,
            "asset_url": f"https://example.invalid/{filename}",
            "sha256": hashlib.sha256(filename.encode("utf-8")).hexdigest(),
            "provenance": "github-release-original",
            "original_asset_available": True,
        }
        for platform, filename in targets
    ]


def _release_catalog():
    """构造 Todo 6 所需的当前与回滚稳定资产证据。"""
    current = tuple(int(value) for value in __version__.split("."))
    versions = [
        ".".join(str(value) for value in (current[0], current[1], current[2] - offset))
        for offset in range(3)
    ]
    return {
        "schema_version": 1,
        "verdict": "pass",
        "releases": [
            {"version": version, "platforms": _assets(version)} for version in versions
        ],
    }


def test_evidence_records_hashed_candidate_and_rollback_inputs(tmp_path, monkeypatch):
    """Given a canonical catalog, when Todo 6 records evidence, then all inputs bind."""
    from scripts import build_task_6_evidence

    catalog_path = tmp_path / "release-catalog.json"
    catalog_path.write_bytes(canonical_bytes(_release_catalog()))
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    monkeypatch.setattr(
        build_task_6_evidence,
        "run_contract_smoke",
        lambda _request: contract_report(),
    )
    request = EvidenceRequest(ROOT, PLAN, catalog_path, evidence_root)

    report_path = build_evidence(request)

    report = canonical_load(report_path.read_bytes())
    assert report["verdict"] == "pass"
    assert report["manual_inspection"]["contract_only"] is True
    assert len(report["target_matrix"]) == 6
    assert all(row["required"] for row in report["target_matrix"])
    assert all(
        len(row["candidate_input"]["sha256"]) == 64
        and len(row["rollback_input"]["sha256"]) == 64
        for row in report["target_matrix"]
    )
    assert report["cleanup_receipt"]["artifact_execution"] == "not-run"
    assert report["source_provenance"]["worktree_state"] in {
        "clean",
        "dirty",
        "unavailable",
    }


def test_evidence_rejects_catalog_without_required_asset(tmp_path, monkeypatch):
    """Given a partial catalog, when Todo 6 records evidence, then it blocks."""
    from scripts import build_task_6_evidence

    catalog = _release_catalog()
    catalog["releases"][0]["platforms"].pop()
    catalog_path = tmp_path / "release-catalog.json"
    catalog_path.write_bytes(canonical_bytes(catalog))
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    monkeypatch.setattr(
        build_task_6_evidence,
        "run_contract_smoke",
        lambda _request: contract_report(),
    )
    request = EvidenceRequest(ROOT, PLAN, catalog_path, evidence_root)

    try:
        build_evidence(request)
    except build_task_6_evidence.EvidenceBlocked as error:
        assert str(error) == "BLOCKED_MISSING_RELEASE_ASSET"
    else:
        raise AssertionError("missing release asset was accepted")


def test_contract_smoke_timeout_blocks_evidence(tmp_path, monkeypatch):
    """Given a stuck contract command, when evidence starts, then it cannot claim pass."""
    from scripts import build_task_6_evidence

    request = EvidenceRequest(ROOT, PLAN, tmp_path / "release-catalog.json", tmp_path)
    monkeypatch.setattr(
        build_task_6_evidence.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("mise", 120)
        ),
    )

    try:
        build_task_6_evidence.run_contract_smoke(request)
    except build_task_6_evidence.EvidenceBlocked as error:
        assert str(error) == "BLOCKED_CONTRACT_SMOKE_TIMEOUT"
    else:
        raise AssertionError("timed out contract smoke was accepted")


def test_evidence_rejects_misleading_non_contract_report(tmp_path, monkeypatch):
    """Given an executable report, when it is not contract-only, then Todo 6 blocks."""
    from scripts import build_task_6_evidence

    catalog_path = tmp_path / "release-catalog.json"
    catalog_path.write_bytes(canonical_bytes(_release_catalog()))
    report = contract_report()
    report["contract_only"] = False
    monkeypatch.setattr(build_task_6_evidence, "run_contract_smoke", lambda _request: report)
    request = EvidenceRequest(ROOT, PLAN, catalog_path, tmp_path)

    try:
        build_evidence(request)
    except build_task_6_evidence.EvidenceBlocked as error:
        assert str(error) == "BLOCKED_INVALID_CONTRACT_REPORT"
    else:
        raise AssertionError("non-contract report was accepted")
