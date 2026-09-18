# pyright: basic

import hashlib
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

offline = importlib.import_module("offline_fixture_runner")
plugin_parity = importlib.import_module("plugin_parity")
plugin_ui_parity = importlib.import_module("plugin_ui_parity")


def _policy(kind, allow_subprocess):
    # Return the one fixed policy shape accepted by Todo17 fixtures.
    return {
        "schema_version": 1,
        "kind": kind,
        "allowed_path_scopes": list(offline.PATH_SCOPES),
        "allow_subprocess": allow_subprocess,
        "denied_external": list(offline.DENIED_EXTERNAL),
        "expected_denials": list(offline.EXPECTED_DENIALS),
    }


# Build the smallest closed-schema report used by run-id rejection probes.
def _schema_report(run_id="a" * 64):
    return {
        "schema_version": 1,
        "kind": "parity_comparison",
        "verdict": "pass",
        "allowed_variance": list(plugin_parity.VARIANCE),
        "errors": [],
        "input_hashes": {},
        "interception": {},
        "run_id": run_id,
    }


@pytest.mark.parametrize(
    ("run_id", "expected"),
    [
        (None, "run_id: missing required field"),
        (True, "run_id: expected string"),
        ("not-a-sha256", "run_id: value does not match pattern"),
        ("A" * 64, "run_id: value does not match pattern"),
    ],
)
def test_report_schema_rejects_missing_or_malformed_run_id(run_id, expected):
    # Schema rejection precedes every provider/UI semantic comparison.
    report = _schema_report()
    if run_id is None:
        del report["run_id"]
    else:
        report["run_id"] = run_id
    assert expected in plugin_parity.validate_report_schema(report)
    assert expected in plugin_ui_parity.validate_report_schema(report)


def test_final_lineage_reports_validate_closed_schema():
    # Every active evidence report must declare a schema-valid run identity.
    evidence = ROOT / ".omo/evidence/pluginized-recomposition-parity"
    manifest = json.loads(
        (evidence / "todo-17-evidence-lineage-fix.json").read_text(encoding="utf-8")
    )
    for row in manifest["active_artifacts"]:
        report = json.loads((evidence / row["path"]).read_text(encoding="utf-8"))
        assert plugin_parity.validate_report_schema(report) == [], row["path"]


def test_missing_local_capture_script_is_a_blocker(tmp_path):
    # Browser parity cannot silently fall back to a static fixture or global tool.
    with pytest.raises(ValueError, match="missing local Chromium capture script"):
        plugin_ui_parity.require_local_capture_script(tmp_path / "missing-capture.mjs")


def test_wrong_provider_selector_names_provider_field_selector_and_policy():
    # F3 is a dedicated input, not a mutated copy of a final green capture.
    failure = json.loads(
        (
            ROOT / "fixtures/plugin-parity/providers-failure/f3-provider-selector.json"
        ).read_text(encoding="utf-8")
    )
    capture = {
        "providers": [
            {
                "id": "source.telegram",
                "factory": {"provider_id": "source.telegram"},
            }
        ]
    }
    assert plugin_parity.provider_failure_errors(capture, failure) == [
        "provider source.telegram field factory.provider_id selector "
        "factory.provider_id policy offline.dns: mismatch"
    ]


def test_wrong_ui_selector_names_provider_field_selector_and_policy():
    # UI failure input compares a real captured selector instead of providing DOM.
    failure = json.loads(
        (ROOT / "fixtures/plugin-parity/ui-failure/f3-selector.json").read_text(
            encoding="utf-8"
        )
    )
    capture = {
        "ui": {
            "settings": {
                "regions": [{"selector": "#settings-content", "dom_sha256": "1" * 64}]
            }
        }
    }
    assert plugin_ui_parity.ui_failure_errors(capture, failure) == [
        "provider source.telegram field dom_sha256 selector #settings-content "
        "policy offline.browser-request: mismatch"
    ]


def test_external_guards_reject_every_class_and_leave_a_trace(tmp_path):
    # DNS, HTTP, processes, helpers, media tools and ADB all fail before execution.
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "offline-policy.json").write_text(
        json.dumps(_policy("provider", [])), encoding="utf-8"
    )
    runner = offline.FixtureRunner(ROOT, fixture_root, "provider")
    with runner:
        runner.probe_denials()
    summary = runner.summary()
    assert summary["policy_errors"] == []
    assert set(offline.EXPECTED_DENIALS).issubset(summary["denials"])
    assert any(
        item["kind"] == "helper" and item["verdict"] == "blocked"
        for item in summary["trace"]
    )
    assert any(
        item["kind"] == "adb" and item["verdict"] == "blocked"
        for item in summary["trace"]
    )


def test_boolean_policy_version_is_not_an_integer(tmp_path):
    # True must not satisfy the strict integer schema version check.
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    policy = _policy("provider", [])
    policy["schema_version"] = True
    (fixture_root / "offline-policy.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )
    with pytest.raises(offline.FixturePolicyError, match="schema_version"):
        offline.FixtureRunner(ROOT, fixture_root, "provider")


def test_malformed_policy_is_not_a_pass_fixture(tmp_path):
    # A truncated JSON fixture cannot be mistaken for an empty successful policy.
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "offline-policy.json").write_text(
        '{"schema_version":', encoding="utf-8"
    )
    with pytest.raises(offline.FixturePolicyError, match="cannot read"):
        offline.FixtureRunner(ROOT, fixture_root, "provider")


def test_dirty_worktree_is_recorded_before_offline_guards(monkeypatch):
    # A real dirty tree is evidence, not an implicit clean/pass assumption.
    responses = [
        SimpleNamespace(
            returncode=0, stdout="9c64dc91c84c5a8f4d2895b6fa29e6d45277e554\n"
        ),
        SimpleNamespace(returncode=0, stdout="?? scripts/plugin_parity.py\n"),
    ]
    monkeypatch.setattr(
        plugin_parity.subprocess, "run", lambda *args, **kwargs: responses.pop(0)
    )
    assert plugin_parity.worktree_facts() == {
        "head": "9c64dc91c84c5a8f4d2895b6fa29e6d45277e554",
        "dirty": True,
        "entries": ["?? scripts/plugin_parity.py"],
        "status_exit": 0,
    }


def test_stale_provider_hash_is_rejected():
    # A stale success bit cannot make a changed capture compare cleanly.
    capture = {
        "schema_version": 1,
        "kind": "provider_capture",
        "variant": "baseline",
        "verdict": "pass",
        "allowed_variance": list(plugin_parity.VARIANCE),
        "errors": [],
        "input_hashes": {},
        "capture_inputs": {},
        "interception": {},
        "providers": [],
        "capture_sha256": "0" * 64,
    }
    assert (
        "capture.capture_sha256: stale or tampered output"
        in plugin_parity.validate_provider_capture(capture)
    )


def test_stale_capture_inputs_cannot_be_referenced_as_current():
    # Rehashing a stale report cannot replace the current C3 input identity.
    stale_inputs = {
        "fixtures/plugin-parity/frozen-staging": (
            "e671255f6270ac85a2801968a3b8fbb5911a7d9ed79d937dd148f93da3708a76"
        )
    }
    capture = {
        "schema_version": 1,
        "kind": "provider_capture",
        "variant": "recomposed",
        "verdict": "pass",
        "allowed_variance": list(plugin_parity.VARIANCE),
        "errors": [],
        "input_hashes": stale_inputs,
        "capture_inputs": stale_inputs,
        "interception": {},
        "providers": [],
        "run_id": plugin_parity.report_run_id(
            "provider_capture", "recomposed", stale_inputs
        ),
    }
    capture["capture_sha256"] = hashlib.sha256(
        plugin_parity.canonical_bytes(capture)
    ).hexdigest()
    errors = plugin_parity.validate_provider_capture(
        capture,
        "recomposed",
        {
            "fixtures/plugin-parity/frozen-staging": (
                "d584d20d7cc24a0f37ab09e7ab6a64a255e161d026789de2df912a347b046530"
            )
        },
    )
    assert "capture.capture_sha256: stale or tampered output" not in errors
    assert "capture.capture_inputs: stale output/hash mismatch" in errors


def test_flaky_ui_region_is_rejected():
    # Repeated screenshots are a measured stability condition, not a PASS label.
    capture = {
        "schema_version": 1,
        "kind": "ui_capture",
        "variant": "recomposed",
        "verdict": "pass",
        "allowed_variance": list(plugin_ui_parity.VARIANCE),
        "errors": [],
        "input_hashes": {},
        "capture_inputs": {},
        "interception": {},
        "viewport": "960x640",
        "theme": "dark",
        "browser": {"name": "chromium"},
        "ui": {
            "settings": {
                "regions": [
                    {
                        "selector": selector,
                        "dom_sha256": "1" * 64,
                        "rect": {"x": 0, "y": 0, "width": 1, "height": 1},
                        "screenshot": {
                            "path": "<evidence>",
                            "sha256": "1" * 64,
                            "repeat_sha256": "0" * 64,
                            "stable": False,
                        },
                    }
                    for selector in plugin_ui_parity.SETTINGS_SELECTORS
                ]
            }
        },
        "capture_sha256": "0" * 64,
    }
    assert any(
        error.endswith("screenshot: flaky")
        for error in plugin_ui_parity.validate_ui_capture(capture)
    )


def test_failure_output_replaces_a_misleading_pass(tmp_path):
    # A fresh changes-requested report replaces a pre-existing PASS payload exactly.
    path = tmp_path / "report.json"
    path.write_text('{"verdict":"pass"}', encoding="utf-8")
    plugin_parity.write_report(
        path,
        {
            "verdict": "changes_requested",
            "errors": ["provider source.telegram: mismatch"],
        },
    )
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "errors": ["provider source.telegram: mismatch"],
        "verdict": "changes_requested",
    }
