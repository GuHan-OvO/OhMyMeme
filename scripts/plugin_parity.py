# pyright: basic

import argparse
import copy
import hashlib
import importlib
import json
import runpy
import subprocess
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path

from offline_fixture_runner import (
    ExternalAccessDenied,
    FixturePolicyError,
    FixtureRunner,
    load_json,
    require_offline_flags,
)
from plugin_parity_json import validate_schema

ROOT = Path(__file__).resolve().parents[1]
PARITY_SCHEMA = ROOT / "schemas" / "plugin" / "parity-report.schema.json"
FROZEN_STAGING = ROOT / "fixtures" / "plugin-parity" / "frozen-staging"
VARIANCE = ["ids", "timestamps", "temporary paths", "thread ordering"]
RUN_ID_SCHEMA = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
PROVIDER_IDS = (
    "source.qqnt",
    "source.telegram",
    "source.douyin",
    "source.wechat",
    "sync.ftp",
    "sync.s3",
    "sync.r2",
    "sync.webdav",
    "transport.lan",
)


# Serialize output once so report self-hashes cover every real capture fact.
def canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


# Hash source and fixture trees without editable-install or interpreter residue.
def sha256_path(path):
    path = Path(path)
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
        return digest.hexdigest()
    if not path.is_dir():
        raise ValueError(f"hash input missing: {path}")
    files = sorted(
        item
        for item in path.rglob("*")
        if item.is_file()
        and "__pycache__" not in item.parts
        and not any(part.endswith(".egg-info") for part in item.parts)
        and "build" not in item.parts
    )
    for item in files:
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# Render worktree paths portably in reports while preserving external evidence paths.
def relative_path(path):
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# Record the local integration worktree state before installing offline process guards.
def worktree_facts():
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    entries = [line for line in status.stdout.splitlines() if line]
    return {
        "head": head.stdout.strip() if head.returncode == 0 else "unavailable",
        "dirty": bool(entries),
        "entries": entries,
        "status_exit": status.returncode,
    }


# Read canonical descriptors through the current production manifest validator.
def manifest_descriptors():
    from ohmymeme.core.plugins.manifest import ENTRY_POINT_GROUP, validate_manifest

    return tuple(
        validate_manifest(
            load_json(ROOT / "config" / "plugin-manifest.json"), ENTRY_POINT_GROUP
        )
    )


# Purge provider package modules before switching source and frozen import roots.
def purge_modules(descriptors):
    roots = tuple(descriptor.package_root for descriptor in descriptors)
    for name in tuple(sys.modules):
        if any(name == root or name.startswith(root + ".") for root in roots):
            del sys.modules[name]


# Read real staged entry-point metadata without consulting installed editable metadata.
def frozen_entry_points(staging):
    entries = []
    for distribution in importlib_metadata.distributions(path=[str(staging)]):
        entries.extend(
            entry
            for entry in distribution.entry_points
            if entry.group == "ohmymeme.plugins.v1"
        )
    return tuple(entries)


class FixtureConfig:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)

    # Return only fixture defaults; persistent host configuration never enters a plugin.
    def get_plugin_value(
        self, _provider_id, _key, _legacy_key, default=None, secret=False
    ):
        return "" if secret else default


class FixtureSecrets:
    # Supply no real credential while proving the narrow backend secret accessor shape.
    def get(self, _key):
        return ""


class FixtureSink:
    # A cancelled capture must never reach host media import persistence.
    def import_path(self, _request, cancelled=None):
        raise AssertionError("fixture capture must not import media")

    def import_bytes(self, _request, cancelled=None):
        raise AssertionError("fixture capture must not import media")

    def import_batch(self, _requests, cancelled=None):
        raise AssertionError("fixture capture must not import media")


class FixtureResources:
    # No physical helper is supplied to the cancelled context capture.
    def register_process(self, _process):
        raise AssertionError("fixture capture must not register a physical process")

    def wechat_helper(self):
        return None


# Normalize only runner-owned temporary paths, an explicitly allowed variance class.
def normalize_temporary_paths(value, workspace):
    root = str(Path(workspace).resolve())
    root_posix = Path(root).as_posix()
    if isinstance(value, dict):
        return {
            key: normalize_temporary_paths(item, workspace)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalize_temporary_paths(item, workspace) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_temporary_paths(item, workspace) for item in value)
    if isinstance(value, str):
        return value.replace(root, "<temporary>").replace(root_posix, "<temporary>")
    return value


# Snapshot one provider progress object without retaining a mutable provider reference.
def progress_snapshot(provider, workspace):
    getter = getattr(provider, "get_progress", None)
    if not callable(getter):
        return {}
    value = getter()
    if not isinstance(value, dict):
        value = {"value": value}
    normalized = normalize_temporary_paths(copy.deepcopy(value), workspace)
    return normalized if isinstance(normalized, dict) else {"value": normalized}


# Mutate only a returned progress snapshot to prove the provider keeps private state.
def has_detached_progress(provider):
    getter = getattr(provider, "get_progress", None)
    if not callable(getter):
        return True
    snapshot = getter()
    if not isinstance(snapshot, dict):
        return True
    snapshot["fixture_mutation"] = True
    current = getter()
    return not isinstance(current, dict) or "fixture_mutation" not in current


# Return fixed, cancelled requests that exercise source lifecycle without source I/O.
def source_request(provider_id, workspace):
    root = Path(workspace)
    return {
        "source.qqnt": {
            "qq_number": "fixture",
            "userdata_save_path": str(root / "qqnt-source"),
            "image_only": True,
            "submit": False,
        },
        "source.telegram": {
            "tdata_path": str(root / "telegram-source"),
            "convert_webm": False,
        },
        "source.douyin": {},
        "source.wechat": {
            "user_root": str(root / "wechat-source"),
            "download": False,
            "account_path": "",
        },
    }[provider_id]


# Capture one source provider through the real registry/context cleanup path.
def source_context_facts(registry, provider, descriptor, runner):
    from ohmymeme.core.plugins.contracts import ImportPluginContext
    from ohmymeme.core.plugins.policy import PluginPolicy

    workspace = runner.workspace()
    policy = PluginPolicy(FixtureConfig(workspace / "policy-data"))
    operation = policy.operation(descriptor)
    temporary = operation.temporary.path(".")
    progress_events = []
    context = ImportPluginContext(
        descriptor,
        FixtureSink(),
        progress_events.append,
        lambda: True,
        operation,
        source_request(descriptor.id, workspace),
        FixtureResources(),
    )
    errors = []
    started = False
    imported = False
    before = progress_snapshot(provider, workspace)
    after = {}
    released = False
    try:
        started = registry.start(descriptor.id, context) is True
        imported = True
        provider.import_media(context)
        after = progress_snapshot(provider, workspace)
    except Exception as error:
        errors.append(
            f"provider {descriptor.id} context.import_media: {type(error).__name__}"
        )
    finally:
        registry.stop(descriptor.id)
        finisher = getattr(provider, "finish", None)
        if callable(finisher):
            finisher()
        released = getattr(provider, "_context", None) is None
        policy.close_operation(operation)
    return (
        {
            "type": f"{type(context).__module__}.{type(context).__qualname__}",
            "fields": list(context._fields),
            "registry_started": started,
            "import_called": imported,
            "cancellation_requested": True,
            "context_released": released,
            "temporary_created": temporary.is_relative_to(Path(workspace)),
            "temporary_removed": not temporary.exists(),
            "progress_events": len(progress_events),
            "before": before,
            "after": after,
        },
        errors,
    )


# Construct one valid, disconnected sync backend through the real registry lifecycle.
def sync_context_facts(registry, provider, descriptor):
    from ohmymeme.core.plugins.contracts import SyncPluginContext
    from ohmymeme.core.plugins.network_config import (
        FtpConfig,
        R2Config,
        S3Config,
        WebDAVConfig,
    )

    configs = {
        "sync.ftp": FtpConfig(host="fixture.invalid"),
        "sync.s3": S3Config(endpoint="https://fixture.invalid", bucket="fixture"),
        "sync.r2": R2Config(account_id="fixture", bucket="fixture"),
        "sync.webdav": WebDAVConfig(url="https://fixture.invalid"),
    }
    context = SyncPluginContext(descriptor, lambda _value: None, lambda: True)
    started = registry.start(descriptor.id, context) is True
    try:
        backend = provider.create_backend(configs[descriptor.id], FixtureSecrets())
    finally:
        registry.stop(descriptor.id)
    return {
        "type": f"{type(context).__module__}.{type(context).__qualname__}",
        "fields": list(context._fields),
        "registry_started": started,
        "backend_type": f"{type(backend).__module__}.{type(backend).__qualname__}",
        "backend_unconnected": all(
            getattr(backend, field, None) is None for field in ("ftp", "client")
        ),
    }


# Construct one unopened LAN transport through the real registry lifecycle.
def lan_context_facts(registry, provider, descriptor):
    from ohmymeme.core.plugins.contracts import LanPluginContext
    from ohmymeme.core.plugins.network_config import LanTransportConfig

    context = LanPluginContext(descriptor, object(), lambda _value: None, lambda: True)
    started = registry.start(descriptor.id, context) is True
    try:
        transport = provider.create_transport(LanTransportConfig("127.0.0.1", 0))
    finally:
        registry.stop(descriptor.id)
    return {
        "type": f"{type(context).__module__}.{type(context).__qualname__}",
        "fields": list(context._fields),
        "registry_started": started,
        "transport_type": (
            f"{type(transport).__module__}.{type(transport).__qualname__}"
        ),
        "listeners_unopened": transport.tcp is None and transport.udp is None,
    }


# Capture one provider factory and context while retaining origin only as provenance.
def provider_facts(registry, descriptor, variant, runner):
    provider = registry.require(descriptor.id)
    module = importlib.import_module(descriptor.package_root)
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise ValueError(f"provider {descriptor.id}: module origin missing")
    origin = Path(module_file).resolve()
    expected_origin = (
        ROOT
        / "plugins"
        / descriptor.id
        / "src"
        / descriptor.package_root
        / "__init__.py"
        if variant == "baseline"
        else FROZEN_STAGING / descriptor.package_root / "__init__.py"
    ).resolve()
    second = module.create_plugin()
    detached = has_detached_progress(provider)
    context_errors = []
    if descriptor.id.startswith("source."):
        context, context_errors = source_context_facts(
            registry, provider, descriptor, runner
        )
    elif descriptor.id.startswith("sync."):
        context = sync_context_facts(registry, provider, descriptor)
    else:
        context = lan_context_facts(registry, provider, descriptor)
    return (
        {
            "id": descriptor.id,
            "descriptor": {
                "api_version": descriptor.api_version,
                "package_root": descriptor.package_root,
                "entry_point": list(descriptor.entry_point),
                "capabilities": list(descriptor.capabilities.names),
            },
            "factory": {
                "module": descriptor.package_root,
                "instance_type": (
                    f"{type(provider).__module__}.{type(provider).__qualname__}"
                ),
                "provider_id": getattr(provider, "provider_id", None),
                "api_version": getattr(provider, "api_version", None),
                "independent_instances": provider is not second,
                "detached_state": detached,
            },
            "context": context,
            "provenance": {
                "module_origin": relative_path(origin),
                "origin_matches_variant": origin == expected_origin,
            },
        },
        context_errors,
    )


# Build a source/frozen registry from canonical descriptors and real entry points.
def registry_facts(variant, descriptors, runner):
    from ohmymeme.core.plugins.registry import PluginRegistry

    original_path = list(sys.path)
    errors = []
    try:
        purge_modules(descriptors)
        if variant == "baseline":
            registry = PluginRegistry(descriptors)
        else:
            sys.path.insert(0, str(FROZEN_STAGING))
            registry = PluginRegistry(
                descriptors, discovered_entry_points=frozen_entry_points(FROZEN_STAGING)
            )
        providers = []
        for descriptor in descriptors:
            row, row_errors = provider_facts(registry, descriptor, variant, runner)
            providers.append(row)
            errors.extend(row_errors)
        return {
            "construction": (
                "PluginRegistry(canonical manifest descriptors, real entry points)"
            ),
            "status": list(registry.status()),
            "providers": providers,
        }, errors
    finally:
        sys.path[:] = original_path
        purge_modules(descriptors)


# Fingerprint every executable input so a stale capture cannot be compared as current.
def capture_inputs(fixture_root, fixture_schema, policy_path):
    values = {
        "config/plugin-manifest.json": sha256_path(
            ROOT / "config" / "plugin-manifest.json"
        ),
        "fixtures/plugin-parity/frozen-staging": sha256_path(FROZEN_STAGING),
        "schemas/plugin/parity-report.schema.json": sha256_path(PARITY_SCHEMA),
        relative_path(fixture_schema): sha256_path(fixture_schema),
        relative_path(policy_path): sha256_path(policy_path),
        "scripts/offline_fixture_runner.py": sha256_path(
            ROOT / "scripts" / "offline_fixture_runner.py"
        ),
        "scripts/plugin_parity.py": sha256_path(ROOT / "scripts" / "plugin_parity.py"),
        "src/ohmymeme/core/plugins/contracts.py": sha256_path(
            ROOT / "src" / "ohmymeme" / "core" / "plugins" / "contracts.py"
        ),
        "src/ohmymeme/core/plugins/import_runtime.py": sha256_path(
            ROOT / "src" / "ohmymeme" / "core" / "plugins" / "import_runtime.py"
        ),
        "src/ohmymeme/core/plugins/manifest.py": sha256_path(
            ROOT / "src" / "ohmymeme" / "core" / "plugins" / "manifest.py"
        ),
        "src/ohmymeme/core/plugins/network_config.py": sha256_path(
            ROOT / "src" / "ohmymeme" / "core" / "plugins" / "network_config.py"
        ),
        "src/ohmymeme/core/plugins/policy.py": sha256_path(
            ROOT / "src" / "ohmymeme" / "core" / "plugins" / "policy.py"
        ),
        "src/ohmymeme/core/plugins/registry.py": sha256_path(
            ROOT / "src" / "ohmymeme" / "core" / "plugins" / "registry.py"
        ),
    }
    for provider_id in PROVIDER_IDS:
        values[f"provider-fixture/{provider_id}"] = sha256_path(
            Path(fixture_root) / f"{provider_id.replace('.', '-')}.json"
        )
        values[f"plugin-source/{provider_id}"] = sha256_path(
            ROOT / "plugins" / provider_id / "src"
        )
    return dict(sorted(values.items()))


# Bind each capture or comparison to its complete, exact input-hash set.
def report_run_id(kind, variant, inputs):
    return hashlib.sha256(
        canonical_bytes({"kind": kind, "variant": variant, "input_hashes": inputs})
    ).hexdigest()


# Validate the closed schema document before accepting a report that cites it.
def validate_parity_schema(schema=None):
    schema = schema or load_json(PARITY_SCHEMA)
    errors = []
    if (
        schema.get("$id")
        != "https://ohmymeme.local/schemas/plugin/parity-report.schema.json"
    ):
        errors.append("parity schema.$id: unexpected value")
    if schema.get("properties", {}).get("schema_version", {}).get("const") != 1:
        errors.append("parity schema.schema_version: expected 1")
    if "run_id" not in schema.get("required", []):
        errors.append("parity schema.run_id: missing required field")
    if schema.get("properties", {}).get("run_id") != RUN_ID_SCHEMA:
        errors.append("parity schema.run_id: expected sha256 string schema")
    return errors


# Validate a report against the closed Todo17 schema before semantic checks.
def validate_report_schema(value):
    schema = load_json(PARITY_SCHEMA)
    return validate_parity_schema(schema) + validate_schema(value, schema, schema, "")


# Validate common report identity and detect an old success hash before comparison.
def validate_provider_capture(value, expected_variant=None, expected_inputs=None):
    errors = validate_report_schema(value)
    if not isinstance(value, dict):
        return errors + ["capture: expected object"]
    required = {
        "schema_version",
        "kind",
        "variant",
        "verdict",
        "allowed_variance",
        "errors",
        "input_hashes",
        "capture_inputs",
        "interception",
        "providers",
        "run_id",
        "capture_sha256",
    }
    for field in sorted(required - set(value)):
        errors.append(f"capture.{field}: missing")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        errors.append("capture.schema_version: expected 1")
    if value.get("kind") != "provider_capture":
        errors.append("capture.kind: expected provider_capture")
    if value.get("variant") not in ("baseline", "recomposed"):
        errors.append("capture.variant: expected baseline or recomposed")
    if expected_variant and value.get("variant") != expected_variant:
        errors.append(f"capture.variant: expected {expected_variant}")
    if value.get("verdict") not in ("pass", "changes_requested"):
        errors.append("capture.verdict: unexpected")
    if value.get("allowed_variance") != VARIANCE:
        errors.append("capture.allowed_variance: unexpected fields")
    if not isinstance(value.get("errors"), list) or any(
        not isinstance(item, str) for item in value.get("errors", [])
    ):
        errors.append("capture.errors: expected string array")
    for field in ("input_hashes", "capture_inputs", "interception"):
        if not isinstance(value.get(field), dict):
            errors.append(f"capture.{field}: expected object")
    if value.get("input_hashes") != value.get("capture_inputs"):
        errors.append("capture.input_hashes: differs from capture_inputs")
    if value.get("run_id") != report_run_id(
        "provider_capture", value.get("variant"), value.get("capture_inputs")
    ):
        errors.append("capture.run_id: stale or tampered input identity")
    providers = value.get("providers")
    if not isinstance(providers, list):
        errors.append("capture.providers: expected array")
    elif [item.get("id") for item in providers if isinstance(item, dict)] != list(
        PROVIDER_IDS
    ):
        errors.append("capture.providers: canonical ID order mismatch")
    copied = dict(value)
    reported = copied.pop("capture_sha256", None)
    if reported != hashlib.sha256(canonical_bytes(copied)).hexdigest():
        errors.append("capture.capture_sha256: stale or tampered output")
    if expected_inputs is not None and value.get("capture_inputs") != expected_inputs:
        errors.append("capture.capture_inputs: stale output/hash mismatch")
    return sorted(set(errors))


# Load the existing static characterization only to reject malformed provider fixtures.
def fixture_contract_errors(fixture_root):
    module = runpy.run_path(str(ROOT / "scripts" / "plugin_parity_schema.py"))
    fixtures, errors = module["load_provider_fixtures"](ROOT, Path(fixture_root))
    if [item.get("id") for item in fixtures] != list(PROVIDER_IDS):
        errors.append("provider fixtures: canonical ID order mismatch")
    return errors


# Capture one real source or frozen implementation without reusing a prior result.
def capture_provider(args):
    fixture_root = Path(args.fixture_root)
    fixture_schema = Path(args.fixture_schema)
    policy_path = Path(args.policy or fixture_root / "offline-policy.json")
    runner = FixtureRunner(
        ROOT,
        fixture_root,
        "provider",
        output_paths=(args.output,),
        workspace_parent=ROOT / ".omo" / "evidence" / "pluginized-recomposition-parity",
        policy_path=policy_path,
    )
    runner.workspace()
    worktree = worktree_facts()
    errors = validate_parity_schema()
    try:
        inputs = capture_inputs(fixture_root, fixture_schema, policy_path)
    except (OSError, ValueError) as error:
        inputs = {}
        errors.append(str(error))
    registry = {"construction": "failed", "status": [], "providers": []}
    try:
        errors.extend(fixture_contract_errors(fixture_root))
        with runner:
            runner.probe_denials()
            runner.probe_fixture_external_request()
            registry, capture_errors = registry_facts(
                args.variant, manifest_descriptors(), runner
            )
            errors.extend(capture_errors)
    except (FixturePolicyError, ExternalAccessDenied, OSError, ValueError) as error:
        errors.append(str(error))
    finally:
        cleaned = runner.cleanup()
    interception = runner.summary()
    errors.extend(interception["policy_errors"])
    report = {
        "schema_version": 1,
        "kind": "provider_capture",
        "variant": args.variant,
        "verdict": "pass" if not errors else "changes_requested",
        "allowed_variance": VARIANCE,
        "errors": sorted(set(errors)),
        "input_hashes": inputs,
        "capture_inputs": inputs,
        "run_id": report_run_id("provider_capture", args.variant, inputs),
        "interception": interception,
        "worktree": worktree,
        "providers": registry["providers"],
        "registry": {
            "construction": registry["construction"],
            "status": registry["status"],
        },
        "durable_state": {"providers": registry["providers"]},
        "cleanup": {"workspace_removed": cleaned},
    }
    report["capture_sha256"] = hashlib.sha256(canonical_bytes(report)).hexdigest()
    return report


# Compare fixed descriptor, factory and context facts without a generic diff language.
def provider_durable_errors(baseline, recomposed):
    errors = []
    baseline_rows = {
        row["id"]: row for row in baseline if isinstance(row, dict) and "id" in row
    }
    recomposed_rows = {
        row["id"]: row for row in recomposed if isinstance(row, dict) and "id" in row
    }
    for provider_id in PROVIDER_IDS:
        expected = baseline_rows.get(provider_id)
        actual = recomposed_rows.get(provider_id)
        if expected is None or actual is None:
            errors.append(f"provider {provider_id} durable_state: missing")
            continue
        for field in ("descriptor", "factory", "context"):
            if expected.get(field) != actual.get(field):
                errors.append(f"provider {provider_id} durable_state.{field}: mismatch")
        for row, variant in ((expected, "baseline"), (actual, "recomposed")):
            provenance = row.get("provenance", {})
            if not provenance.get("origin_matches_variant"):
                errors.append(
                    f"provider {provider_id} provenance.{variant}: wrong origin"
                )
        expected_origin = expected.get("provenance", {}).get("module_origin")
        actual_origin = actual.get("provenance", {}).get("module_origin")
        if expected_origin == actual_origin:
            errors.append(
                f"provider {provider_id} provenance.module_origin: not recomposed"
            )
    return errors


# Validate the one concrete F3 provider selector input; it is not a diff DSL.
def validate_provider_failure_input(value):
    required = {
        "schema_version",
        "kind",
        "provider",
        "field",
        "selector",
        "expected",
        "policy",
    }
    if not isinstance(value, dict):
        return ["provider failure input: expected object"]
    errors = []
    for field in sorted(required - set(value)):
        errors.append(f"provider failure input.{field}: missing")
    for field in sorted(set(value) - required):
        errors.append(f"provider failure input.{field}: unexpected")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        errors.append("provider failure input.schema_version: expected 1")
    if value.get("kind") != "provider_failure_input":
        errors.append("provider failure input.kind: expected provider_failure_input")
    if value.get("provider") not in PROVIDER_IDS:
        errors.append("provider failure input.provider: unknown")
    if value.get("field") != "factory.provider_id":
        errors.append("provider failure input.field: expected factory.provider_id")
    if value.get("selector") != "factory.provider_id":
        errors.append("provider failure input.selector: expected factory.provider_id")
    if not isinstance(value.get("expected"), str):
        errors.append("provider failure input.expected: expected string")
    if value.get("policy") != "offline.dns":
        errors.append("provider failure input.policy: expected offline.dns")
    return errors


# Compare the concrete F3 provider selector against an actual recomposed capture.
def provider_failure_errors(capture, failure):
    errors = validate_provider_failure_input(failure)
    if errors:
        return sorted(errors)
    row = next(
        (
            item
            for item in capture.get("providers", [])
            if isinstance(item, dict) and item.get("id") == failure["provider"]
        ),
        None,
    )
    actual = row.get("factory", {}).get("provider_id") if row else None
    if actual != failure["expected"]:
        return [
            f"provider {failure['provider']} field {failure['field']} selector "
            f"{failure['selector']} policy {failure['policy']}: mismatch"
        ]
    return []


# Compare independently captured baseline and recomposed provider reports exactly.
def compare_provider_captures(
    baseline, recomposed, fixture_root, fixture_schema, policy_path
):
    expected_inputs = capture_inputs(fixture_root, fixture_schema, policy_path)
    errors = validate_provider_capture(baseline, "baseline", expected_inputs)
    errors.extend(validate_provider_capture(recomposed, "recomposed", expected_inputs))
    if baseline.get("verdict") != "pass":
        errors.extend(
            baseline.get("errors", ["baseline.verdict: capture did not pass"])
        )
    if recomposed.get("verdict") != "pass":
        errors.extend(
            recomposed.get("errors", ["recomposed.verdict: capture did not pass"])
        )
    errors.extend(
        provider_durable_errors(
            baseline.get("providers", []), recomposed.get("providers", [])
        )
    )
    return sorted(set(errors))


# Write deterministic JSON only to a caller-selected existing parent directory.
def write_report(path, report):
    path = Path(path)
    if not path.parent.is_dir():
        raise ValueError(f"report parent missing: {path.parent}")
    path.write_bytes(canonical_bytes(report) + b"\n")


# Parse the two capture variants and one explicit baseline/recomposed compare form.
def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Capture bounded plugin parity offline"
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--capture", action="store_true")
    modes.add_argument("--compare", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--intercept-external", action="store_true")
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument(
        "--fixture-schema",
        type=Path,
        default=ROOT / "schemas" / "plugin-parity" / "provider-fixture.schema.json",
    )
    parser.add_argument("--policy", type=Path)
    parser.add_argument(
        "--variant", choices=("baseline", "recomposed"), default="baseline"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--recomposed", type=Path)
    parser.add_argument("--ui-baseline", type=Path)
    parser.add_argument("--ui-recomposed", type=Path)
    parser.add_argument("--provider-failure", type=Path)
    parser.add_argument("--ui-failure", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    require_offline_flags(args.offline, args.no_network, args.intercept_external)
    if args.capture and args.output is None:
        parser.error("--capture requires --output")
    if args.compare and any(
        value is None
        for value in (
            args.baseline,
            args.recomposed,
            args.ui_baseline,
            args.ui_recomposed,
            args.report,
        )
    ):
        parser.error(
            "--compare requires provider/UI baseline, recomposed, and report paths"
        )
    return args


# Run capture/compare and replace any stale report content deterministically.
def main(argv=None):
    try:
        args = parse_args(argv)
        fixture_root = Path(args.fixture_root)
        fixture_schema = Path(args.fixture_schema)
        policy_path = Path(args.policy or fixture_root / "offline-policy.json")
        if args.capture:
            report = capture_provider(args)
            write_report(args.output, report)
        else:
            import plugin_ui_parity

            baseline = load_json(args.baseline)
            recomposed = load_json(args.recomposed)
            ui_baseline = load_json(args.ui_baseline)
            ui_recomposed = load_json(args.ui_recomposed)
            provider_errors = compare_provider_captures(
                baseline, recomposed, fixture_root, fixture_schema, policy_path
            )
            ui_result = plugin_ui_parity.compare_ui(
                ui_baseline, ui_recomposed, fixture_root, policy_path
            )
            failure_errors = []
            failure_inputs = {}
            if args.provider_failure:
                provider_failure = load_json(args.provider_failure)
                failure_inputs["provider"] = sha256_path(args.provider_failure)
                failure_errors.extend(
                    provider_failure_errors(recomposed, provider_failure)
                )
            if args.ui_failure:
                ui_failure = load_json(args.ui_failure)
                failure_inputs["ui"] = sha256_path(args.ui_failure)
                failure_errors.extend(
                    plugin_ui_parity.ui_failure_errors(ui_recomposed, ui_failure)
                )
            errors = sorted(set(provider_errors + ui_result["errors"] + failure_errors))
            input_hashes = {
                "provider_baseline": sha256_path(args.baseline),
                "provider_recomposed": sha256_path(args.recomposed),
                "ui_baseline": sha256_path(args.ui_baseline),
                "ui_recomposed": sha256_path(args.ui_recomposed),
                **failure_inputs,
            }
            report = {
                "schema_version": 1,
                "kind": "parity_comparison",
                "verdict": "pass" if not errors else "changes_requested",
                "allowed_variance": VARIANCE,
                "errors": errors,
                "input_hashes": input_hashes,
                "interception": {
                    "provider_baseline": baseline.get("interception", {}),
                    "provider_recomposed": recomposed.get("interception", {}),
                    "ui_baseline": ui_baseline.get("interception", {}),
                    "ui_recomposed": ui_recomposed.get("interception", {}),
                },
                "durable_comparison": {"errors": provider_errors},
                "ui_comparison": ui_result,
                "failure_comparison": {"errors": sorted(set(failure_errors))},
            }
            report["run_id"] = report_run_id(
                report["kind"], report["verdict"], input_hashes
            )
            report["report_sha256"] = hashlib.sha256(
                canonical_bytes(report)
            ).hexdigest()
            write_report(args.report, report)
    except (FixturePolicyError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    for error in report["errors"]:
        print(f"FAIL: {error}", file=sys.stderr)
    if report["verdict"] != "pass":
        return 1
    print("PASS: deterministic plugin parity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
