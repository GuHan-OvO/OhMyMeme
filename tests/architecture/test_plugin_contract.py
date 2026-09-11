# pyright: basic

import json
import subprocess
import sys
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "plugin_contract.py"


def _run_contract(arguments):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _manifest_data():
    return json.loads(
        (ROOT / "config" / "plugin-manifest.json").read_text(encoding="utf-8")
    )


def _run_determinism(manifest_path, report):
    return _run_contract(
        [
            "--check-determinism",
            "--manifest",
            str(manifest_path),
            "--report",
            str(report),
        ]
    )


def test_plugin_contexts_are_distinct_and_lan_ports_are_narrow():
    contracts = import_module("ohmymeme.core.plugins.contracts")
    ports = import_module("ohmymeme.core.plugins.ports")

    assert (
        len(
            {
                contracts.ImportPluginContext,
                contracts.SyncPluginContext,
                contracts.LanPluginContext,
            }
        )
        == 3
    )
    assert not hasattr(contracts.LanPluginContext, "security")
    for port_name in (
        "DeviceApprovalPort",
        "LanByteTransportPort",
        "LanSecurityPort",
        "LanCommandPort",
        "SecureSessionPort",
        "ReplayPolicyPort",
    ):
        assert hasattr(ports, port_name)


def test_manifest_validation_rejects_each_contract_drift():
    manifest_module = import_module("ohmymeme.core.plugins.manifest")
    invalid_cases = []

    incompatible_api = _manifest_data()
    incompatible_api["api_version"] = 2
    invalid_cases.append((incompatible_api, "manifest.api_version: expected 1"))

    boolean_api = _manifest_data()
    boolean_api["plugins"][0]["api_version"] = True
    invalid_cases.append((boolean_api, "plugins[0].api_version: expected 1"))

    duplicate_id = _manifest_data()
    duplicate_id["plugins"][1]["id"] = "source.qqnt"
    invalid_cases.append((duplicate_id, "plugins: duplicate plugin ID 'source.qqnt'"))

    unknown_id = _manifest_data()
    unknown_id["plugins"][1]["id"] = "source.unknown"
    invalid_cases.append(
        (unknown_id, "plugins[1].id: unknown plugin ID 'source.unknown'")
    )

    malformed_id = _manifest_data()
    malformed_id["plugins"][1]["id"] = []
    invalid_cases.append((malformed_id, "plugins[1].id: unknown plugin ID []"))

    wrong_package_root = _manifest_data()
    wrong_package_root["plugins"][1]["package_root"] = "wrong_package"
    invalid_cases.append(
        (
            wrong_package_root,
            "plugins[1].package_root: expected ohmymeme_plugin_telegram",
        )
    )

    mismatched_triplet = _manifest_data()
    mismatched_triplet["plugins"][1]["entry_point"]["value"] = "wrong:create_plugin"
    invalid_cases.append(
        (
            mismatched_triplet,
            "plugins[1].entry_point: expected "
            "('ohmymeme.plugins.v1', 'source.telegram', "
            "'ohmymeme_plugin_telegram:create_plugin')",
        )
    )

    for invalid_manifest, expected_error in invalid_cases:
        try:
            manifest_module.validate_manifest(invalid_manifest, "ohmymeme.plugins.v1")
        except ValueError as error:
            assert expected_error in getattr(error, "errors", ())
        else:
            raise AssertionError(
                "expected manifest validation to reject contract drift"
            )


def test_plugin_contract_canonical_manifest_and_determinism(tmp_path):
    validation_report = tmp_path / "contract.json"
    validation = _run_contract(
        [
            "--validate-manifest",
            str(ROOT / "config" / "plugin-manifest.json"),
            "--entry-point-group",
            "ohmymeme.plugins.v1",
            "--report",
            str(validation_report),
        ]
    )
    determinism_report = tmp_path / "determinism.json"
    determinism = _run_contract(
        [
            "--check-determinism",
            "--manifest",
            str(ROOT / "config" / "plugin-manifest.json"),
            "--report",
            str(determinism_report),
        ]
    )

    assert validation.returncode == 0, validation.stderr
    assert determinism.returncode == 0, determinism.stderr
    assert b'"status":"PASS"' in validation_report.read_bytes()
    assert b'"status":"PASS"' in determinism_report.read_bytes()
    report = json.loads(determinism_report.read_text(encoding="utf-8"))
    assert report["manifest_sha256"] == report["serialization_sha256"]


def test_plugin_contract_rejects_undeclared_capability_before_provider_execution(
    tmp_path,
):
    report = tmp_path / "contract-failure.json"

    result = _run_contract(
        [
            "--validate-manifest",
            str(ROOT / "fixtures" / "plugin-parity" / "invalid-descriptor.json"),
            "--entry-point-group",
            "ohmymeme.plugins.v1",
            "--report",
            str(report),
        ]
    )

    assert result.returncode != 0
    assert (
        "plugins[0].capabilities[2]: undeclared capability 'persistence.write' "
        "for source.qqnt"
    ) in result.stderr


def test_determinism_rejects_invalid_descriptor_fixture(tmp_path):
    report = tmp_path / "invalid-descriptor-report.json"

    result = _run_determinism(
        ROOT / "fixtures" / "plugin-parity" / "invalid-descriptor.json", report
    )

    assert result.returncode != 0
    assert (
        "plugins[0].capabilities[2]: undeclared capability 'persistence.write' "
        "for source.qqnt"
    ) in result.stderr
    assert b'"status":"REJECTED"' in report.read_bytes()


def test_determinism_rejects_manifest_without_trailing_newline(tmp_path):
    manifest = tmp_path / "missing-trailing-newline.json"
    report = tmp_path / "missing-trailing-newline-report.json"
    manifest.write_text(
        json.dumps(
            _manifest_data(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        encoding="utf-8",
    )

    result = _run_determinism(manifest, report)

    assert result.returncode != 0
    assert (
        "manifest: expected canonical UTF-8 JSON bytes with trailing newline"
        in result.stderr
    )
    assert b'"status":"REJECTED"' in report.read_bytes()


def test_determinism_rejects_noncanonical_whitespace(tmp_path):
    manifest = tmp_path / "noncanonical-whitespace.json"
    report = tmp_path / "noncanonical-whitespace-report.json"
    manifest.write_text(
        json.dumps(_manifest_data(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    result = _run_determinism(manifest, report)

    assert result.returncode != 0
    assert (
        "manifest: expected canonical UTF-8 JSON bytes with trailing newline"
        in result.stderr
    )
    assert b'"status":"REJECTED"' in report.read_bytes()


def test_determinism_rejects_extra_hook_field(tmp_path):
    manifest = tmp_path / "extra-hook.json"
    report = tmp_path / "extra-hook-report.json"
    value = _manifest_data()
    value["plugins"][0]["hook"] = "unexpected"
    manifest.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )

    result = _run_determinism(manifest, report)

    assert result.returncode != 0
    assert "plugins[0].hook: unexpected property" in result.stderr
    assert b'"status":"REJECTED"' in report.read_bytes()
