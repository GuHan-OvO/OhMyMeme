# pyright: basic

import copy
import json
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from ohmymeme.core.plugins.manifest import canonical_descriptor
from ohmymeme.core.plugins.registry import PluginRegistry
from scripts import plugin_license_matrix as licenses

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs/plugin-license-matrix.json"
SCHEMA = ROOT / "schemas/plugin/license-matrix.schema.json"


def documents():
    # Load authored records separately from the source-derived observations.
    return (
        licenses._json(MATRIX.read_bytes()),
        licenses._json(SCHEMA.read_bytes()),
    )


def test_auditable_distribution_native_scope_can_approve_real_release():
    # RECORD-bound wheel evidence is sufficient at distribution scope, not a link map.
    matrix, schema = documents()

    errors, facts, _ = licenses.check_matrix(matrix, schema)

    assert errors == []
    assert all(
        target["excluded_modules"] == ["gmssl"]
        for target in facts["release_scope"]["targets"]
    )
    native = {row["id"]: row for row in facts["native_distributions"]}
    assert native["cryptography-wheel"]["scope"] == "shipped_distribution"
    assert native["pillow-wheel"]["scope"] == "shipped_distribution"
    assert native["curl-cffi-wheel"]["component_status"] == (
        "distribution-covered-unmapped"
    )
    assert facts["owned_source_mapping"]["local_tag"] == "0.6.3"
    assert facts["owned_source_mapping"]["binary_binding"] == "not-reproducible"


def test_gmssl_is_the_only_frozen_disabled_runtime_and_keeps_sentinel_policy():
    # Missing full gmssl license material must disable only that optional provider path.
    matrix, schema = documents()

    errors, facts, _ = licenses.check_matrix(matrix, schema)

    assert errors == []
    external = {row["id"]: row for row in facts["release_scope"]["components"]}
    assert external["gmssl-distribution"]["scope"] == "external_runtime_not_shipped"
    assert external["gmssl-distribution"]["notice_status"] == "NOASSERTION"
    assert external["gmssl-distribution"]["source_archive"]["sha256"] == (
        "f3d8c8c75dd34cd169f129c017f67fdd80cce2c67a13f9a0e3b1c58f8de6351e"
    )
    assert all(
        feature["status"] == "runtime_unavailable"
        for feature in facts["release_scope"]["feature_availability"]
        if feature["provider"] == "source.douyin"
    )


def test_helper_external_scope_cannot_be_relabelled_as_shipped():
    # A runtime digest is not a source-to-binary attestation or a shipped component.
    matrix, schema = documents()
    helper = next(
        row
        for row in matrix["release_scope"]["components"]
        if row["id"] == "wechat-keyfinder-downloaded-binary"
    )
    helper["scope"] = "shipped_distribution"

    errors, _, _ = licenses.check_matrix(matrix, schema)

    assert any("wechat-keyfinder-downloaded-binary" in error for error in errors)
    assert any(
        "external runtime" in error or "unapproved value" in error for error in errors
    )


def test_source_facts_match_auditable_distribution_level_release_scope():
    # Complete wheel RECORD, notice, and SBOM evidence permits the real release scope.
    matrix, schema = documents()
    before = set(sys.modules)
    with patch.object(subprocess, "Popen") as process, patch.object(
        socket, "create_connection"
    ) as network:
        errors, facts, hashes = licenses.check_matrix(matrix, schema)
    assert licenses._schema(matrix, schema, schema) == []
    assert errors == []
    assert len(facts["distributions"]) == 9
    assert facts["provider_extras"]["source.douyin"] == [
        "curl_cffi>=0.7.4",
        "gmssl>=3.2.2",
    ]
    assert "requirements.txt" in hashes and "setup.py" in hashes
    assert not process.called and not network.called
    assert not any(
        name.startswith("ohmymeme_plugin_") for name in set(sys.modules) - before
    )


def test_invalid_notice_bundle_fixture_is_rejected_before_release(tmp_path):
    # A separate malformed bundle cannot inherit approval from the matrix fixture.
    report = tmp_path / "report.json"
    result = licenses.main(
        [
            "--check",
            "--schema",
            str(SCHEMA),
            "--matrix",
            str(MATRIX),
            "--bundle-index",
            "fixtures/plugin-parity/notice-bundle-invalid.json",
            "--report",
            str(report),
        ]
    )

    value = json.loads(report.read_text(encoding="utf-8"))
    assert result == 1 and value["status"] == "REJECTED"
    assert any(
        "notice_bundle.index.content_sha256" in error for error in value["errors"]
    )


def test_notice_bundle_rejects_changed_copied_license(monkeypatch, tmp_path):
    # A changed copied notice is rejected even when the matrix claims approval.
    changed = tmp_path / "LICENSE"
    changed.write_text("not the copied license", encoding="utf-8")
    original_path = licenses.build_notice_bundle._path

    def redirected(root, name):
        if name == "THIRD-PARTY-NOTICES/boto3/LICENSE":
            return changed
        return original_path(root, name)

    monkeypatch.setattr(licenses.build_notice_bundle, "_path", redirected)
    errors = licenses.build_notice_bundle.check_bundle()

    assert any(
        "notice_bundle.file[THIRD-PARTY-NOTICES/boto3/LICENSE]: byte/hash mismatch"
        in error
        for error in errors
    )


def test_notice_bundle_rejects_missing_copied_license(monkeypatch, tmp_path):
    # A missing retained notice must fail instead of inheriting index approval.
    missing = tmp_path / "missing-LICENSE"
    original_path = licenses.build_notice_bundle._path

    def redirected(root, name):
        if name == "THIRD-PARTY-NOTICES/boto3/LICENSE":
            return missing
        return original_path(root, name)

    monkeypatch.setattr(licenses.build_notice_bundle, "_path", redirected)
    errors = licenses.build_notice_bundle.check_bundle()

    assert any(
        "notice_bundle.file[THIRD-PARTY-NOTICES/boto3/LICENSE]: missing" in error
        for error in errors
    )


def test_notice_bundle_report_atomically_replaces_stale_pass(monkeypatch, tmp_path):
    # The bundle checker also replaces a stale PASS through same-directory replace.
    report = tmp_path / "bundle.json"
    report.write_text('{"status":"PASS","stale":true}', encoding="utf-8")
    calls = []
    replace = licenses.build_notice_bundle.os.replace

    def tracked_replace(source, destination):
        calls.append((Path(source), Path(destination)))
        replace(source, destination)

    monkeypatch.setattr(licenses.build_notice_bundle.os, "replace", tracked_replace)
    result = licenses.build_notice_bundle.main(["--check", "--report", str(report)])

    value = json.loads(report.read_text(encoding="utf-8"))
    assert result == 0 and value["status"] == "PASS"
    assert "stale" not in value
    assert calls and calls[-1][1] == report


def test_native_record_bundle_mismatch_is_field_specific(monkeypatch, tmp_path):
    # RECORD byte drift must name the wheel record instead of only failing the index.
    matrix, _ = documents()
    row = next(
        item
        for item in matrix["native_distributions"]
        if item["id"] == "curl-cffi-wheel"
    )
    changed = tmp_path / "RECORD"
    changed.write_text("changed", encoding="utf-8")
    original_path = licenses._path

    def redirected(root, name):
        if name == row["record"]["bundle_path"]:
            return changed
        return original_path(root, name)

    monkeypatch.setattr(licenses, "_path", redirected)
    errors = licenses._native_distribution_approval(
        row,
        ROOT,
        "matrix.native_distributions[curl-cffi-wheel]",
        licenses.build_notice_bundle.expected_index(),
    )

    assert any(
        "matrix.native_distributions[curl-cffi-wheel].record: bundled RECORD "
        "byte/hash mismatch" in error
        for error in errors
    )


def test_owned_helper_mapping_does_not_create_tag_claim_files():
    # The GPL mapping belongs in LICENSES, not newly invented helper source files.
    matrix, _ = documents()
    helper = next(
        row for row in matrix["source_components"] if row["id"] == "wechat-helper"
    )

    assert not (ROOT / "src/wechat_keyfinder/LICENSE").exists()
    assert not (ROOT / "src/wechat_keyfinder/NOTICE").exists()
    assert helper["license_files"] == ["LICENSE"]
    assert helper["notice"]["files"] == [
        "LICENSE",
        "src/wechat_keyfinder/CMakeLists.txt",
        "src/wechat_keyfinder/wechat_keyfinder.cpp",
        "src/ohmymeme/integrations/imports/wechat.py",
    ]


@pytest.mark.parametrize("module_name", ["scripts.build", "scripts.nuitka.build"])
def test_frozen_build_rejects_local_helper_payload_before_staging(
    monkeypatch, tmp_path, module_name
):
    # A fetched helper payload in the C++ source tree must stop before staging.
    module = __import__(module_name, fromlist=["_"])
    project = tmp_path / "project"
    helper = project / "src/wechat_keyfinder"
    helper.mkdir(parents=True)
    (helper / "wechat_keyfinder.exe").write_bytes(b"fixture helper")
    monkeypatch.setattr(module, "PROJECT_ROOT", project)
    monkeypatch.setattr(module, "SRC_DIR", project / "src")

    with pytest.raises(SystemExit, match="external wechat helper payload"):
        module._assert_external_helper_not_packaged()


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda matrix: matrix["release_scope"]["components"][0].update(
                scope="shipped_distribution"
            ),
            (
                "matrix.release_scope.components[gmssl-distribution].scope: "
                "external runtime"
            ),
        ),
        (
            lambda matrix: matrix["source_components"][2]["source_revision"].update(
                source_binding="unverified"
            ),
            "matrix.source_components[2].source_revision.source_binding",
        ),
    ],
)
def test_scope_and_helper_source_offer_drift_are_rejected(mutate, expected):
    # Scope and source-offer statements are evidence fields, not release overrides.
    matrix, schema = documents()
    mutate(matrix)

    errors, _, _ = licenses.check_matrix(matrix, schema)

    assert any(expected in error for error in errors), errors


def test_excluded_douyin_runtime_keeps_unavailable_sentinel():
    # A missing optional frozen import quarantines only the selected descriptor.
    descriptor = canonical_descriptor("source.douyin")

    class MissingEntryPoint:
        group = descriptor.group
        name = descriptor.name
        value = descriptor.value

        def load(self):
            raise ModuleNotFoundError("No module named 'gmssl'")

    registry = PluginRegistry(
        (descriptor,), discovered_entry_points=(MissingEntryPoint(),)
    )

    assert registry.get("source.douyin") is None
    with pytest.raises(ValueError, match="source.douyin: provider_unavailable"):
        registry.require("source.douyin")


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (
            ("source_wheel", "sha256"),
            "0" * 64,
            "matrix.native_distributions[curl-cffi-wheel].source_wheel.sha256",
        ),
        (
            ("artifacts", 0, "record_path"),
            "curl_cffi/unknown.pyd",
            "matrix.native_distributions[curl-cffi-wheel].artifacts[0].record_path",
        ),
        (
            ("evidence_expression",),
            "NOASSERTION",
            "matrix.native_distributions[cryptography-wheel].evidence_expression",
        ),
    ],
)
def test_native_wheel_mapping_and_noassertion_drift_are_rejected(path, value, expected):
    # A Python distribution record cannot hide a wheel/source/RECORD mismatch.
    matrix, schema = documents()
    row = next(
        item
        for item in matrix["native_distributions"]
        if item["id"] == ("cryptography-wheel" if len(path) == 1 else "curl-cffi-wheel")
    )
    target = row
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    errors, _, _ = licenses.check_matrix(matrix, schema)

    assert any(expected in error for error in errors), errors


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("spdx", "wrong license 'MIT'"),
        ("notice", ".notice:"),
        ("provenance", ".provenance: missing required field"),
        ("approval", ".dependency_approval: missing required field"),
        ("unknown", "ohmymeme-plugin-rogue"),
        ("stale", ".source_hashes."),
        ("fixture", "unbound evidence"),
        ("upstream", "provenance.references"),
        ("internal-dependency", "official dependency approval/license missing"),
        ("misleading-approval", "missing dependency approval/provenance for boto3"),
    ],
)
def test_adversarial_matrix_rejects_named_field(case, expected):
    # Each mutation must produce its own diagnostic, not merely the baseline blocker.
    matrix, schema = documents()
    row = matrix["distributions"][0]
    if case == "spdx":
        row["spdx"] = "MIT"
    elif case == "notice":
        row["notice"]["files"] = []
    elif case == "provenance":
        del row["provenance"]
    elif case == "approval":
        del row["dependency_approval"]
    elif case == "unknown":
        row.update(id="source.rogue", distribution="ohmymeme-plugin-rogue")
    elif case == "stale":
        row["source_hashes"][row["pyproject"]] = "0" * 64
    elif case == "fixture":
        row["provenance"]["files"] = [
            "fixtures/plugin-parity/license-matrix-invalid.json"
        ]
    elif case == "upstream":
        matrix["source_components"][0]["provenance"]["references"] = []
    else:
        name = "ohmymeme-plugin-sync-s3" if case == "internal-dependency" else "boto3"
        dependency = next(
            item for item in matrix["dependencies"] if item["name"] == name
        )
        dependency["approval"] = (
            "rejected" if case == "internal-dependency" else "approved"
        )
        if case == "misleading-approval":
            dependency["provenance_file"] = None
    errors, _, _ = licenses.check_matrix(matrix, schema)
    assert any(expected in error for error in errors), errors


@pytest.mark.parametrize("case", ["source", "metadata", "unknown", "requirements"])
def test_source_hash_drift_preserves_unrelated_dirty_files_and_cleans_probe(case):
    # Change a real copied source while keeping its frozen counterpart unchanged.
    matrix, schema = documents()
    _, _, hashes = licenses.inspect_sources()
    with tempfile.TemporaryDirectory(prefix="todo14-license-source-") as directory:
        root = Path(directory)
        for name in hashes:
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, destination)
        sentinel = root / "unrelated-dirty.txt"
        sentinel.write_text("retain me", encoding="utf-8")
        if case == "source":
            source = root / "plugins/source.qqnt/src/ohmymeme_plugin_qqnt/__init__.py"
            source.write_bytes(source.read_bytes() + b"\n# source drift probe\n")
            expected = "source hash drift in frozen"
        elif case == "metadata":
            source = (
                root
                / licenses.STAGING
                / "ohmymeme_plugin_qqnt-0.1.0.dist-info/METADATA"
            )
            source.write_bytes(source.read_bytes().replace(b"GPL-3.0-only", b"MIT"))
            expected = "frozen metadata License-Expression mismatch"
        elif case == "unknown":
            source = root / licenses.STAGING / "rogue-1.0.dist-info/METADATA"
            source.parent.mkdir()
            source.write_text("Name: rogue\n", encoding="utf-8")
            expected = "unknown distribution directories"
        else:
            source = root / "requirements.txt"
            source.write_bytes(source.read_bytes() + b"\nrequests>=2.0\n")
            expected = "matrix.dependencies: unknown/missing"
        errors, _, _ = licenses.check_matrix(copy.deepcopy(matrix), schema, root)
        assert any(expected in error for error in errors), errors
        if case == "source":
            assert any(
                "source_hashes" in error and "__init__.py" in error for error in errors
            )
        assert sentinel.read_text(encoding="utf-8") == "retain me"
    assert not root.exists()


@pytest.mark.parametrize(
    "raw", [b"{", b'{"schema_version":1,"schema_version":1}', b'{"schema_version":NaN}']
)
def test_malformed_cli_overwrites_stale_pass_and_cleans_probe(raw):
    # A parse failure must replace old success evidence with a fresh nonzero result.
    with tempfile.TemporaryDirectory(prefix="todo14-license-report-") as directory:
        root = Path(directory)
        matrix = root / "malformed.json"
        report = root / "report.json"
        matrix.write_bytes(raw)
        report.write_text('{"status":"PASS"}', encoding="utf-8")
        result = licenses.main(
            [
                "--check",
                "--schema",
                str(SCHEMA),
                "--matrix",
                str(matrix),
                "--report",
                str(report),
            ]
        )
        value = json.loads(report.read_text(encoding="utf-8"))
        assert result == 1 and value["status"] == "REJECTED"
        assert value["errors"] and value["provider_execution"] is False
        assert value["build_execution"] is False
    assert not root.exists()


def test_valid_cli_atomically_replaces_stale_report(monkeypatch, tmp_path):
    # A plan-path happy run must replace stale evidence with the current PASS facts.
    report = tmp_path / "todo-14-licenses.json"
    report.write_text('{"status":"PASS","stale":true}', encoding="utf-8")
    calls = []
    replace = licenses.os.replace

    def tracked_replace(source, destination):
        calls.append((Path(source), Path(destination)))
        replace(source, destination)

    monkeypatch.setattr(licenses.os, "replace", tracked_replace)
    result = licenses.main(
        [
            "--check",
            "--schema",
            str(SCHEMA),
            "--matrix",
            str(MATRIX),
            "--report",
            str(report),
        ]
    )

    value = json.loads(report.read_text(encoding="utf-8"))
    assert result == 0 and value["status"] == "PASS"
    assert "stale" not in value
    assert any(
        row["id"] == "curl-cffi-wheel"
        and row["component_status"] == "distribution-covered-unmapped"
        for row in value["observed"]["native_distributions"]
    )
    assert calls and calls[-1][1] == report


def test_validator_exception_atomically_replaces_stale_pass(monkeypatch, tmp_path):
    # An unexpected validator failure still publishes a fresh fail-closed report.
    report = tmp_path / "todo-14-licenses.json"
    report.write_text('{"status":"PASS","stale":true}', encoding="utf-8")
    calls = []
    replace = licenses.os.replace

    def tracked_replace(source, destination):
        calls.append((Path(source), Path(destination)))
        replace(source, destination)

    def broken_check(*_args, **_kwargs):
        raise RuntimeError("freshness probe")

    monkeypatch.setattr(licenses.os, "replace", tracked_replace)
    monkeypatch.setattr(licenses, "check_matrix", broken_check)
    result = licenses.main(
        [
            "--check",
            "--schema",
            str(SCHEMA),
            "--matrix",
            str(MATRIX),
            "--report",
            str(report),
        ]
    )

    value = json.loads(report.read_text(encoding="utf-8"))
    assert result == 1 and value["status"] == "REJECTED"
    assert value["errors"] == ["freshness probe"]
    assert "stale" not in value and calls and calls[-1][1] == report
