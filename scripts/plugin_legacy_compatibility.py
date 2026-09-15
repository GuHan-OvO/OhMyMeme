import json
import sys
import tempfile
from argparse import ArgumentParser
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_SOURCE_PROVIDERS = (
    "source.qqnt",
    "source.telegram",
    "source.douyin",
    "source.wechat",
)
_FAILURE_CASES = (
    "missing_provider",
    "disabled_provider",
    "incompatible_provider",
    "no_fallback_provider",
    "malformed_wrapper",
    "wrong_arguments",
)
_UNAVAILABLE_SENTINELS = {
    "start_tg_import": {"ok": False},
    "get_tg_import_progress": {},
    "cancel_tg_import": None,
}
_PRODUCTION_FLAGS = (
    "requires_all_provider_descriptors",
    "requires_host_action_registry",
    "requires_entry_point_factory",
)

from ohmymeme.app.container import Container  # noqa: E402
from ohmymeme.core.plugins.manifest import (  # noqa: E402
    ENTRY_POINT_GROUP,
    validate_manifest,
)
from ohmymeme.core.plugins.registry import PluginRegistry  # noqa: E402
from ohmymeme.presentation.desktop import import_workers  # noqa: E402
from ohmymeme.presentation.desktop.api.facades import SettingsApi  # noqa: E402
from ohmymeme.presentation.desktop.api.plugin_dispatch import (  # noqa: E402
    HostActionAdapter,
    HostDispatcher,
)


def _load_json(path):
    # Read only the declared compatibility expectations.
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates
    )


def _descriptors():
    # Load canonical identities without importing provider implementations.
    manifest = _load_json(ROOT / "config/plugin-manifest.json")
    return validate_manifest(manifest, ENTRY_POINT_GROUP)


def _descriptor_record(descriptor):
    # Serialize the fixed manifest identity, including its entry-point triplet.
    return {
        "id": descriptor.id,
        "api_version": descriptor.api_version,
        "package_root": descriptor.package_root,
        "entry_point": [descriptor.group, descriptor.name, descriptor.value],
        "capabilities": list(descriptor.capabilities.names),
    }


def _validate_fixture(fixture):
    # Reject compatibility metadata before any Container or provider is created.
    if not isinstance(fixture, dict):
        raise ValueError("fixture: expected object")
    if set(fixture) != {
        "providers",
        "unavailable_sentinels",
        "failure_cases",
        "production",
    }:
        raise ValueError("fixture: exact top-level fields required")
    if fixture["providers"] != list(_SOURCE_PROVIDERS):
        raise ValueError("fixture.providers: expected canonical source provider order")
    sentinels = fixture["unavailable_sentinels"]
    if not isinstance(sentinels, dict) or set(sentinels) != set(_UNAVAILABLE_SENTINELS):
        raise ValueError("fixture.unavailable_sentinels: drifted legacy sentinels")
    if (
        not isinstance(sentinels["start_tg_import"], dict)
        or set(sentinels["start_tg_import"]) != {"ok"}
        or type(sentinels["start_tg_import"]["ok"]) is not bool
        or sentinels["start_tg_import"]["ok"]
        or sentinels["get_tg_import_progress"] != {}
        or sentinels["cancel_tg_import"] is not None
    ):
        raise ValueError("fixture.unavailable_sentinels: drifted legacy sentinels")
    if fixture["failure_cases"] != list(_FAILURE_CASES):
        raise ValueError("fixture.failure_cases: expected canonical failure cases")
    production = fixture["production"]
    if not isinstance(production, dict) or set(production) != {
        *_PRODUCTION_FLAGS,
        "descriptors",
    }:
        raise ValueError("fixture.production: exact fields required")
    if any(
        type(production[field]) is not bool or not production[field]
        for field in _PRODUCTION_FLAGS
    ):
        raise ValueError("fixture.production: all required probes must be enabled")
    expected = [_descriptor_record(descriptor) for descriptor in _descriptors()]
    descriptors = production["descriptors"]
    if not isinstance(descriptors, list) or len(descriptors) != len(expected):
        raise ValueError(
            "fixture.production.descriptors: canonical count/order required"
        )
    fields = {"id", "api_version", "package_root", "entry_point", "capabilities"}
    for index, descriptor in enumerate(descriptors):
        if not isinstance(descriptor, dict) or set(descriptor) != fields:
            raise ValueError(f"fixture.production.descriptors[{index}]: malformed")
        if (
            type(descriptor["api_version"]) is not int
            or not isinstance(descriptor["id"], str)
            or not isinstance(descriptor["package_root"], str)
            or not isinstance(descriptor["entry_point"], list)
            or len(descriptor["entry_point"]) != 3
            or not all(isinstance(value, str) for value in descriptor["entry_point"])
            or not isinstance(descriptor["capabilities"], list)
            or not all(isinstance(value, str) for value in descriptor["capabilities"])
        ):
            raise ValueError(f"fixture.production.descriptors[{index}]: malformed")
        if descriptor != expected[index]:
            raise ValueError(
                f"fixture.production.descriptors[{index}]: canonical identity drifted"
            )


def _record(errors, observations, name, callback):
    # Keep every probe result so one missing seam does not hide later failures.
    try:
        callback()
    except Exception as error:
        errors.append(f"{name}: {type(error).__name__}: {error}")


def _production_probe(observations):
    # Exercise the real Container -> WebUI -> facade construction seam offline.
    with tempfile.TemporaryDirectory(prefix="ohmm-todo12-") as directory:
        container = Container(Path(directory) / "host")
        try:
            webui = container.create_webui()
            descriptors = tuple(container.plugins.descriptors())
            provider_ids = tuple(descriptor.id for descriptor in descriptors)
            observations["production"] = {
                "provider_ids": provider_ids,
                "descriptors": [
                    _descriptor_record(descriptor) for descriptor in descriptors
                ],
                "has_host_action_registry": isinstance(
                    getattr(webui, "_plugin_action_registry", None), PluginRegistry
                ),
                "main_facade": type(webui._api).__name__,
                "settings_facade": type(webui._settings_api).__name__,
            }
            expected = _descriptors()
            if descriptors != expected:
                raise AssertionError("Container registry descriptors drifted")
            if not isinstance(
                getattr(webui, "_plugin_action_registry", None), PluginRegistry
            ):
                raise AssertionError("WebUI did not construct a host action registry")
        finally:
            container.close()


def _entry_point_factory_probe(observations):
    # Prove worker construction asks the supplied registry for a factory.
    with tempfile.TemporaryDirectory(prefix="ohmm-todo12-factory-") as directory:
        container = Container(Path(directory) / "host")
        try:
            webui = container.create_webui()
            calls = []

            class Provider:
                def get_progress(self):
                    return {"status": "idle", "progress": 0}

            provider = Provider()

            class Registry:
                def require(self, provider_id, enabled=None):
                    calls.append((provider_id, enabled))
                    return provider

            container.plugins = Registry()
            had_import = "import_module" in vars(import_workers)
            original_import = getattr(import_workers, "import_module", None)
            import_workers.import_module = Mock(
                side_effect=AssertionError("provider construction bypassed registry")
            )
            try:
                worker = import_workers.get_import_worker(
                    webui._settings_api._legacy, "source.telegram"
                )
            finally:
                if had_import:
                    import_workers.import_module = original_import
                else:
                    del import_workers.import_module
            if worker.provider is not provider:
                raise AssertionError(
                    "worker did not retain the registry-created provider"
                )
            if calls != [("source.telegram", None)]:
                raise AssertionError(f"unexpected registry calls: {calls!r}")
            observations["entry_point_factory"] = {"provider_calls": calls}
        finally:
            container.close()


def _facade_for_registry(container, webui, registry, enabled=None):
    # Bind a real settings facade to a deliberately controlled host registry.
    webui._plugin_action_registry = registry
    webui._enabled_import_plugins = enabled
    return SettingsApi(webui, container.settings)


def _unavailable_probe(fixture, observations):
    # Verify each missing-provider mode preserves the old wrapper sentinels.
    observed = {}
    with tempfile.TemporaryDirectory(prefix="ohmm-todo12-failure-") as directory:
        container = Container(Path(directory) / "host")
        try:
            webui = container.create_webui()
            descriptors = _descriptors()
            for case in fixture["failure_cases"]:
                if case == "malformed_wrapper":
                    try:
                        HostActionAdapter("source.telegram", {"execute": Mock()})
                    except ValueError:
                        observed[case] = "rejected"
                    else:
                        raise AssertionError("malformed legacy wrapper was accepted")
                    continue
                if case == "wrong_arguments":
                    registry = Mock()
                    dispatcher = HostDispatcher("settings", registry)
                    try:
                        dispatcher.dispatch("start_tg_import", [None])
                    except ValueError:
                        observed[case] = "rejected"
                    else:
                        raise AssertionError("wrong fixed arguments crossed dispatcher")
                    registry.get.assert_not_called()
                    continue

                current = descriptors
                builtins = {}
                enabled = None
                alternate = Mock(return_value=object())
                if case == "missing_provider":
                    current = tuple(
                        descriptor
                        for descriptor in descriptors
                        if descriptor.id != "source.telegram"
                    )
                elif case == "disabled_provider":
                    enabled = ()
                    builtins["source.telegram"] = alternate
                elif case == "incompatible_provider":
                    current = tuple(
                        (
                            descriptor._replace(api_version=99)
                            if descriptor.id == "source.telegram"
                            else descriptor
                        )
                        for descriptor in descriptors
                    )
                    builtins["source.telegram"] = alternate
                elif case == "no_fallback_provider":
                    builtins["source.douyin"] = alternate
                else:
                    raise AssertionError(f"unknown fixture case: {case}")
                registry = PluginRegistry(current, builtins, ())
                api = _facade_for_registry(container, webui, registry, enabled)
                actual = {
                    "start_tg_import": api.start_tg_import(),
                    "get_tg_import_progress": api.get_tg_import_progress(),
                    "cancel_tg_import": api.cancel_tg_import(),
                }
                if actual != fixture["unavailable_sentinels"]:
                    raise AssertionError(f"{case} changed legacy sentinels: {actual!r}")
                alternate.assert_not_called()
                observed[case] = actual
        finally:
            container.close()
    observations["unavailable"] = observed


def _adapter_probe(observations):
    # Prove the facade reaches one fixed host adapter with exact positional args.
    with tempfile.TemporaryDirectory(prefix="ohmm-todo12-adapter-") as directory:
        container = Container(Path(directory) / "host")
        try:
            webui = container.create_webui()
            handler = Mock(return_value={"ok": True})
            registry = PluginRegistry(
                _descriptors(),
                {
                    "source.telegram": lambda: HostActionAdapter(
                        "source.telegram", {"start_tg_import": handler}
                    )
                },
                (),
            )
            api = _facade_for_registry(container, webui, registry)
            actual = api.start_tg_import("fixture-tdata", "fixture-passcode", False)
            if actual != {"ok": True}:
                raise AssertionError(f"adapter result changed: {actual!r}")
            handler.assert_called_once_with("fixture-tdata", "fixture-passcode", False)
            observations["host_adapter"] = {
                "result": actual,
                "args": list(handler.call_args.args),
            }
        finally:
            container.close()


def main():
    # Emit a deterministic report and fail closed on any compatibility drift.
    parser = ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    errors = []
    observations = {}
    try:
        fixture = _load_json(args.fixture)
        _validate_fixture(fixture)
        _record(
            errors,
            observations,
            "production",
            lambda: _production_probe(observations),
        )
        _record(
            errors,
            observations,
            "entry_point_factory",
            lambda: _entry_point_factory_probe(observations),
        )
        _record(
            errors,
            observations,
            "unavailable",
            lambda: _unavailable_probe(fixture, observations),
        )
        _record(
            errors,
            observations,
            "host_adapter",
            lambda: _adapter_probe(observations),
        )
    except Exception as error:
        errors.append(f"fixture: {type(error).__name__}: {error}")
    report = {
        "status": "PASS" if not errors else "REJECTED",
        "verdict": "confirmed" if not errors else "changes_requested",
        "errors": errors,
        "observations": observations,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
