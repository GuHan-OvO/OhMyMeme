import copy
import json
from pathlib import Path

import pytest

from ohmymeme import __version__
from ohmymeme.core.plugins.manifest import CANONICAL_PROVIDERS
from scripts import plugin_release_compatibility as compatibility

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "plugin-parity" / "release-compatibility.json"
INVALID_FIXTURE = (
    ROOT / "fixtures" / "plugin-parity" / "release-compatibility-invalid.json"
)
LEGACY_CONFIG = ROOT / "fixtures" / "plugin-parity" / "legacy-config.json"
INVALID_LEGACY_CONFIG = (
    ROOT / "fixtures" / "plugin-parity" / "legacy-config-invalid.json"
)


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _run(fixture, legacy_config, report):
    return compatibility.main(
        [
            "--check",
            "--fixture",
            str(fixture),
            "--legacy-config",
            str(legacy_config),
            "--report",
            str(report),
        ]
    )


def test_legacy_config_round_trips_real_config_and_keeps_old_key_precedence(tmp_path):
    errors, observed = compatibility.check_legacy_config(_load(LEGACY_CONFIG), tmp_path)

    assert errors == []
    assert observed["version"] == __version__
    assert observed["copy_resize_mode"] == 0
    assert observed["precedence"]["sync.ftp.host"] == "legacy-todo15-ftp.example"
    assert observed["precedence"]["sync.ftp.password"] == "configured"
    assert observed["encrypted_secret_keys"] == sorted(_load(LEGACY_CONFIG)["secrets"])
    assert observed["database"] == {
        "filename": "legacy-todo15.png",
        "journal_mode": "wal",
        "migrated_columns": ["from_stego", "sort_order", "stego_of_hash"],
    }


def test_release_catalog_keeps_nine_ids_order_and_atomic_activation_without_network(
    monkeypatch,
):
    fixture = _load(FIXTURE)
    monkeypatch.setattr(
        compatibility.updates,
        "_fetch_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network")),
    )

    errors, observed = compatibility.check_release_fixture(fixture)

    assert errors == []
    assert observed["official_ids"] == [row[0] for row in CANONICAL_PROVIDERS]
    assert observed["activation_allowed"] is True
    assert observed["stable_filter"] == {"nightly_rejected": True}
    assert [row["target"] for row in observed["target_matrix"]] == [
        "windows-x64",
        "linux-appimage-x64",
        "linux-deb-amd64",
        "linux-rpm-x64",
        "macos-arm64",
        "macos-x86_64",
    ]


def test_invalid_fixture_rejects_changed_hash_api_nightly_and_rollback_hash(tmp_path):
    report = tmp_path / "failure.json"

    assert _run(INVALID_FIXTURE, LEGACY_CONFIG, report) == 1
    result = _load(report)

    assert result["status"] == "REJECTED"
    assert result["observed"]["activation_allowed"] is False
    assert any(
        "frozen_plugin_update.plugins[source.qqnt].sha256" in error
        for error in result["errors"]
    )
    assert any(
        "frozen_plugin_update.plugins[source.telegram].api_version" in error
        for error in result["errors"]
    )
    assert any("frozen_plugin_update.channel" in error for error in result["errors"])
    assert any(
        "release_catalog.target_matrix[windows-x64].candidate_input.sha256" in error
        for error in result["errors"]
    )


def test_invalid_legacy_mapping_is_field_specific_and_replaces_stale_report(tmp_path):
    report = tmp_path / "failure.json"
    report.write_text('{"status":"PASS","stale":true}', encoding="utf-8")

    assert _run(FIXTURE, INVALID_LEGACY_CONFIG, report) == 1
    result = _load(report)

    assert result["status"] == "REJECTED"
    assert "stale" not in result
    assert any(
        "legacy_config.mappings[sync.ftp.host].legacy_key" in error
        for error in result["errors"]
    )


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda value: value["release_catalog"]["releases"][0].update(version="0.6"),
            "release_catalog.releases[0].version",
        ),
        (
            lambda value: value["release_catalog"]["releases"][0].update(
                version="0.6.4"
            ),
            "release_catalog.releases[0].version: stale current release",
        ),
        (
            lambda value: value["frozen_plugin_update"].update(atomic=1),
            "frozen_plugin_update.atomic",
        ),
    ],
)
def test_malformed_or_stale_release_inputs_block_activation(change, expected):
    fixture = copy.deepcopy(_load(FIXTURE))
    change(fixture)

    errors, observed = compatibility.check_release_fixture(fixture)

    assert observed["activation_allowed"] is False
    assert any(expected in error for error in errors)
