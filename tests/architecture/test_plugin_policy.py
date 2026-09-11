# pyright: basic

import json
import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest

from ohmymeme.core.config import Config
from ohmymeme.core.plugins.contracts import PluginCapabilities
from ohmymeme.core.plugins.manifest import ENTRY_POINT_GROUP, validate_manifest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "plugin_policy.py"
MATRIX = ROOT / "docs" / "plugin-secret-matrix.json"


def _run_policy(fixture, report):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--check",
            "--matrix",
            str(MATRIX),
            "--fixture",
            str(fixture),
            "--report",
            str(report),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _descriptor(provider_id):
    manifest = json.loads(
        (ROOT / "config" / "plugin-manifest.json").read_text(encoding="utf-8")
    )
    return next(
        descriptor
        for descriptor in validate_manifest(manifest, ENTRY_POINT_GROUP)
        if descriptor.id == provider_id
    )


def test_plugin_policy_cli_accepts_scoped_fixture_and_rejects_each_escape(tmp_path):
    valid_report = tmp_path / "policy.json"
    valid = _run_policy(
        ROOT / "fixtures" / "plugin-parity" / "policy-valid.json", valid_report
    )
    invalid_report = tmp_path / "policy-failure.json"
    invalid = _run_policy(
        ROOT / "fixtures" / "plugin-parity" / "policy-invalid.json", invalid_report
    )

    assert valid.returncode == 0, valid.stderr
    assert b'"status":"PASS"' in valid_report.read_bytes()
    assert invalid.returncode != 0
    assert "cross-provider config access" in invalid.stderr
    assert "temporary path traversal" in invalid.stderr
    assert "undeclared capability 'network.https'" in invalid.stderr
    assert "undeclared capability 'process.helper'" in invalid.stderr
    assert "secret output must be redacted" in invalid.stderr
    assert "LAN export includes secret key 'lan_secret'" in invalid.stderr
    assert b'"status":"REJECTED"' in invalid_report.read_bytes()


def test_scoped_operation_keeps_legacy_precedence_and_host_temp_ownership(tmp_path):
    policy = import_module("ohmymeme.core.plugins.policy")

    # Given: legacy and namespaced values disagree, and the host owns a secret.
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "ftp_host": "legacy.example",
                "plugins": {"sync.ftp": {"host": "plugin.example"}},
            }
        ),
        encoding="utf-8",
    )
    config = Config(config_path, tmp_path / "data")
    config.set("ftp_password", "fixture-secret")
    plugin_policy = policy.PluginPolicy(config)

    # When: the FTP plugin uses its host-created operation scope.
    with plugin_policy.operation(_descriptor("sync.ftp")) as operation:
        child = operation.temporary.path("staging/image.bin")

        # Then: legacy wins, only scoped services are exposed, and traversal fails.
        assert operation.settings.get("host") == "legacy.example"
        operation.settings.set("path", "/scoped")
        assert operation.settings.get("path") == "/scoped"
        assert operation.secrets.get("password") == "fixture-secret"
        assert not hasattr(operation.settings, "to_dict")
        assert not hasattr(operation.settings, "data_dir")
        assert child.parent.is_dir()
        with pytest.raises(policy.PluginPolicyError, match="temporary path traversal"):
            operation.temporary.path("../durable-data")
        with pytest.raises(policy.PluginPolicyError, match="unknown config key"):
            operation.settings.get("bucket")
        with pytest.raises(policy.PluginPolicyError, match="undeclared capability"):
            operation.require("process.helper")
        assert policy.redact({"password": "fixture-secret"}) == {
            "password": "[REDACTED]"
        }

    assert not child.exists()
    assert "lan_secret" not in config.export_for_lan()


def test_scoped_operation_rejects_forged_descriptor_and_redacts_nested_secrets(
    tmp_path,
):
    policy = import_module("ohmymeme.core.plugins.policy")

    # Given: a host config and a canonical FTP descriptor.
    config = Config(tmp_path / "config.json", tmp_path / "data")
    config.set("ftp_password", "legacy-secret")
    descriptor = _descriptor("sync.ftp")
    plugin_policy = policy.PluginPolicy(config)
    forged_capabilities = descriptor._replace(
        capabilities=PluginCapabilities(("network.remote", "process.helper"))
    )
    forged_identity = descriptor._replace(id="source.qqnt")

    # When: untrusted descriptors attempt to acquire an operation.
    with pytest.raises(policy.PluginPolicyError, match="canonical"):
        plugin_policy.operation(forged_capabilities)
    with pytest.raises(policy.PluginPolicyError, match="canonical"):
        plugin_policy.operation(forged_identity)

    # Then: scoped runtime output recursively redacts legacy and scoped secret names.
    with plugin_policy.operation(descriptor) as operation:
        assert all(
            not isinstance(value, Config)
            for scope in (operation.settings, operation.secrets)
            for value in vars(scope).values()
        )
        output = operation.redact(
            {
                "nested": {
                    "ftp_password": "legacy-secret",
                    "password": "scoped-secret",
                },
                "items": [
                    {"lan_secret": "lan-secret"},
                    ("safe", {"secret_access_key": "scoped-access-secret"}),
                ],
            }
        )

    assert "legacy-secret" not in repr(output)
    assert "scoped-secret" not in repr(output)
    assert "lan-secret" not in repr(output)
    assert "scoped-access-secret" not in repr(output)


def test_scoped_operation_detaches_config_and_freezes_capabilities(tmp_path):
    policy = import_module("ohmymeme.core.plugins.policy")

    # Given: an operation created from the canonical FTP descriptor.
    config = Config(tmp_path / "config.json", tmp_path / "data")
    plugin_policy = policy.PluginPolicy(config)
    descriptor = _descriptor("sync.ftp")

    # When: a plugin inspects its config port and tampers with its descriptor.
    with plugin_policy.operation(descriptor) as operation:
        config_port = operation.settings._config_port
        operation._descriptor = descriptor._replace(
            capabilities=PluginCapabilities(("network.remote", "process.helper"))
        )

        # Then: no bound Config method is reachable and the canonical capability
        # set still rejects the injected privilege.
        assert all(
            getattr(value, "__self__", None) is not config
            for value in vars(config_port).values()
            if callable(value)
        )
        assert all(
            not isinstance(value, Config) for value in vars(config_port).values()
        )
        with pytest.raises(policy.PluginPolicyError, match="undeclared capability"):
            operation.require("process.helper")


def test_capabilities_copy_input_and_operation_serializers_redact(tmp_path):
    policy = import_module("ohmymeme.core.plugins.policy")

    # Given: a mutable capability input and secret-bearing operation output.
    names = ["network.remote"]
    capabilities = PluginCapabilities(names)
    names.append("process.helper")
    replacement = capabilities._replace(names=names)
    names.append("filesystem.write")
    config = Config(tmp_path / "config.json", tmp_path / "data")
    plugin_policy = policy.PluginPolicy(config)
    payload = {
        "nested": [
            {"password": "descriptor-secret"},
            {"lan_secret": "progress-secret"},
            ("safe", {"secret_key": "error-secret"}),
            {"ftp_password": "log-secret"},
        ]
    }

    # When: the host serializes each plugin-facing output category.
    with plugin_policy.operation(_descriptor("sync.ftp")) as operation:
        serialized = (
            operation.serialize_descriptor(payload),
            operation.serialize_progress(payload),
            operation.serialize_error(payload),
            operation.serialize_log(payload),
        )

    # Then: mutation cannot add a capability and every serialization path redacts.
    assert not capabilities.allows("process.helper")
    assert not replacement.allows("filesystem.write")
    for value in serialized:
        assert "descriptor-secret" not in repr(value)
        assert "progress-secret" not in repr(value)
        assert "error-secret" not in repr(value)
        assert "log-secret" not in repr(value)


def test_plugin_ports_queue_host_writes_and_redact_real_emissions(tmp_path):
    policy = import_module("ohmymeme.core.plugins.policy")

    # Given: a scoped plugin has a secret that may occur in plain output text.
    config = Config(tmp_path / "config.json", tmp_path / "data")
    config.set("ftp_password", "fixture-secret")
    plugin_policy = policy.PluginPolicy(config)

    # When: the plugin queues a setting and emits all runtime output categories.
    with plugin_policy.operation(_descriptor("sync.ftp")) as operation:
        reachable = [operation]
        seen = set()
        while reachable:
            value = reachable.pop()
            if id(value) in seen:
                continue
            seen.add(id(value))
            if isinstance(value, Config):
                pytest.fail("plugin-held object graph reaches Config")
            if isinstance(value, dict):
                reachable.extend(value.values())
            elif isinstance(value, (list, tuple, set, frozenset)):
                reachable.extend(value)
            elif hasattr(value, "__dict__"):
                reachable.extend(vars(value).values())
            if callable(value):
                bound_self = getattr(value, "__self__", None)
                if bound_self is not None:
                    reachable.append(bound_self)
                closure = getattr(value, "__closure__", None)
                if closure is not None:
                    reachable.extend(cell.cell_contents for cell in closure)

        operation.settings.set("path", "/queued")
        assert config.get_plugin_value("sync.ftp", "path", "ftp_path") is None
        operation.emit_descriptor({"message": "descriptor fixture-secret"})
        operation.emit_progress({"nested": [{"password": "fixture-secret"}]})
        operation.emit_error({"message": "error fixture-secret"})
        operation.emit_log({"message": "log fixture-secret"})
        emitted = []
        plugin_policy.flush_outputs(
            operation, lambda kind, payload: emitted.append((kind, payload))
        )
        plugin_policy.commit_settings(operation)

    # Then: only the host commits the queued change and receives redacted output.
    assert config.get_plugin_value("sync.ftp", "path", "ftp_path") == "/queued"
    assert [kind for kind, _payload in emitted] == [
        "descriptor",
        "progress",
        "error",
        "log",
    ]
    assert emitted == [
        ("descriptor", {"message": "descriptor [REDACTED]"}),
        ("progress", {"nested": [{"password": "[REDACTED]"}]}),
        ("error", {"message": "error [REDACTED]"}),
        ("log", {"message": "log [REDACTED]"}),
    ]
