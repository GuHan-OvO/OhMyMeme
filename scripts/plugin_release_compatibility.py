# pyright: basic

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCHEMA = ROOT / "schemas" / "plugin" / "release-compatibility.schema.json"
SOURCE_MANIFEST = ROOT / "config" / "plugin-manifest.json"
FROZEN_MANIFEST = (
    ROOT
    / "fixtures"
    / "plugin-parity"
    / "frozen-staging"
    / "ohmymeme"
    / "config"
    / "plugin-manifest.json"
)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ohmymeme import __version__  # noqa: E402
from ohmymeme.core.config import _SECRET_KEYS, Config  # noqa: E402
from ohmymeme.core.database import MemeDB  # noqa: E402
from ohmymeme.core.plugins.manifest import (  # noqa: E402
    CANONICAL_PROVIDERS,
    PLUGIN_API_VERSION,
    canonical_document,
    validate_manifest,
)
from ohmymeme.core.plugins.policy import PLUGIN_SETTINGS  # noqa: E402
from ohmymeme.services import updates  # noqa: E402
from scripts.package_smoke import (  # noqa: E402
    FROZEN_DATA_TARGETS,
    RUNTIME_ENTRYPOINT,
    ArtifactContract,
)
from scripts.package_smoke_inputs import TARGETS  # noqa: E402


def _duplicates(pairs):
    # Reject duplicate keys so a later fixture value cannot hide an earlier one.
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(raw):
    # Parse only finite, duplicate-free JSON input.
    def reject(value):
        # Reject non-standard JSON constants such as NaN and Infinity.
        raise ValueError(f"invalid JSON constant: {value}")

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw, object_pairs_hook=_duplicates, parse_constant=reject)


def _schema_errors(value, schema, root, path):
    # Apply the small schema vocabulary used by this offline compatibility input.
    if not isinstance(schema, dict):
        return [f"{path}: invalid schema"]
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#/"):
            return [f"{path}: unsupported schema reference"]
        target = root
        try:
            for item in reference[2:].split("/"):
                target = target[item]
        except (KeyError, TypeError):
            return [f"{path}: unresolved schema reference"]
        return _schema_errors(value, target, root, path)

    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    expected = schema.get("type")
    errors = []
    if expected is not None:
        allowed = expected if isinstance(expected, list) else [expected]
        valid = any(item in types and type(value) is types[item] for item in allowed)
        if not valid:
            return [f"{path}: expected {expected}"]
    if "const" in schema and (
        type(value) is not type(schema["const"]) or value != schema["const"]
    ):
        errors.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and not any(
        type(value) is type(item) and value == item for item in schema["enum"]
    ):
        errors.append(f"{path}: unapproved value {value!r}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing required field")
        additional = schema.get("additionalProperties", {})
        for key, item in value.items():
            child = properties.get(key, additional)
            if child is False:
                errors.append(f"{path}.{key}: unexpected property")
            elif isinstance(child, dict):
                errors.extend(_schema_errors(item, child, root, f"{path}.{key}"))
    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < minimum:
            errors.append(f"{path}: expected at least {minimum} items")
        if maximum is not None and len(value) > maximum:
            errors.append(f"{path}: expected at most {maximum} items")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True) for item in value]
            if len(encoded) != len(set(encoded)):
                errors.append(f"{path}: duplicate item")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                errors.extend(_schema_errors(item, items, root, f"{path}[{index}]"))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: expected non-empty text")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            errors.append(f"{path}: malformed value {value!r}")
    return errors


def _schema():
    # Load the committed fixture contract without a third-party schema framework.
    return _json(SCHEMA.read_bytes())


def _sha256(path):
    # Hash the exact bytes bound into an offline release record.
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _same(errors, path, actual, expected):
    # Report values with strict bool/int separation.
    if type(actual) is not type(expected) or actual != expected:
        errors.append(f"{path}: expected {expected!r}, got {actual!r}")


def _version(value):
    # Parse only final PEP 440-compatible X.Y.Z values used by this updater.
    if type(value) is not str:
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    if match is None:
        return None
    return tuple(int(item) for item in match.groups())


def _canonical_ids():
    # Return the manifest order shared by source, frozen, and editable forms.
    return [row[0] for row in CANONICAL_PROVIDERS]


def _manifest_facts(errors):
    # Confirm that the host and frozen manifests remain byte-identical.
    try:
        source_raw = SOURCE_MANIFEST.read_bytes()
        frozen_raw = FROZEN_MANIFEST.read_bytes()
        source_value = _json(source_raw)
        frozen_value = _json(frozen_raw)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        errors.append(f"host manifest: {error}")
        return b"", []
    if source_raw != canonical_document(source_value):
        errors.append("host manifest: expected canonical UTF-8 JSON bytes")
    if frozen_raw != source_raw:
        errors.append("frozen manifest: bytes differ from host manifest")
    try:
        descriptors = validate_manifest(source_value, "ohmymeme.plugins.v1")
        validate_manifest(frozen_value, "ohmymeme.plugins.v1")
    except ValueError as error:
        errors.extend(getattr(error, "errors", (str(error),)))
        return source_raw, []
    ids = [descriptor.id for descriptor in descriptors]
    if ids != _canonical_ids():
        errors.append("host manifest: expected canonical nine plugin IDs")
    return source_raw, ids


def _check_host(fixture, manifest_raw, errors, facts):
    # Bind fixture claims to the installed runtime and public updater entrypoints.
    host = fixture.get("host") if isinstance(fixture, dict) else None
    if not isinstance(host, dict):
        errors.append("host: expected object")
        return
    _same(
        errors,
        "host.runtime_entrypoint",
        host.get("runtime_entrypoint"),
        RUNTIME_ENTRYPOINT,
    )
    entrypoints = [
        "check_latest_cached",
        "start_download",
        "run_downloaded_installer",
    ]
    _same(
        errors, "host.updater_entrypoints", host.get("updater_entrypoints"), entrypoints
    )
    for name in entrypoints:
        if not callable(getattr(updates, name, None)):
            errors.append(
                f"host.updater_entrypoints[{name}]: missing updater entrypoint"
            )
    _same(
        errors,
        "host.manifest_path",
        host.get("manifest_path"),
        "ohmymeme/config/plugin-manifest.json",
    )
    _same(
        errors,
        "host.manifest_sha256",
        host.get("manifest_sha256"),
        hashlib.sha256(manifest_raw).hexdigest(),
    )
    if "ohmymeme/config/plugin-manifest.json" not in FROZEN_DATA_TARGETS:
        errors.append("host.manifest_path: frozen data target is missing")
    facts["host"] = {
        "runtime_entrypoint": RUNTIME_ENTRYPOINT,
        "updater_entrypoints": entrypoints,
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
    }


def _asset(release, platform):
    # Find one platform asset without accepting duplicates.
    if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
        return None
    matches = [
        row
        for row in release["assets"]
        if isinstance(row, dict) and row.get("platform") == platform
    ]
    return matches[0] if len(matches) == 1 else None


def _updater_accepts(release, target, expected_asset):
    # Exercise existing selection logic with fixture values only, never a network call.
    systems = {
        "windows-x64": ("Windows", "x86_64"),
        "linux-appimage-x64": ("Linux", "x86_64"),
        "macos-arm64": ("Darwin", "arm64"),
        "macos-x86_64": ("Darwin", "x86_64"),
    }
    system = systems.get(target)
    if system is None or not isinstance(release, dict):
        return False
    original_system = updates.platform.system
    original_machine = updates.platform.machine
    original_hashes = dict(updates._ASSET_HASHES)
    try:
        updates.platform.system = lambda: system[0]
        updates.platform.machine = lambda: system[1]
        updates._ASSET_HASHES.clear()
        assets = [
            {
                "name": row.get("asset_name"),
                "browser_download_url": row.get("asset_url"),
                "digest": "sha256:" + row.get("sha256", ""),
            }
            for row in release.get("assets", [])
            if isinstance(row, dict)
        ]
        parsed = updates._parse_release(
            {
                "tag_name": "v" + release.get("version", ""),
                "prerelease": release.get("prerelease"),
                "assets": assets,
            }
        )
        return (
            parsed is not None
            and parsed[2] == expected_asset.get("asset_url")
            and updates._ASSET_HASHES.get(parsed[2]) == expected_asset.get("sha256")
        )
    finally:
        updates.platform.system = original_system
        updates.platform.machine = original_machine
        updates._ASSET_HASHES.clear()
        updates._ASSET_HASHES.update(original_hashes)


def _nightly_rejected(release):
    # Verify the existing stable updater rejects a nightly before asset selection.
    original_system = updates.platform.system
    original_hashes = dict(updates._ASSET_HASHES)
    try:
        updates.platform.system = lambda: "Windows"
        updates._ASSET_HASHES.clear()
        assets = [
            {
                "name": row.get("asset_name"),
                "browser_download_url": row.get("asset_url"),
                "digest": "sha256:" + row.get("sha256", ""),
            }
            for row in release.get("assets", [])
            if isinstance(row, dict)
        ]
        return (
            updates._parse_release(
                {
                    "tag_name": release.get("version", ""),
                    "prerelease": release.get("prerelease"),
                    "assets": assets,
                }
            )
            is None
        )
    finally:
        updates.platform.system = original_system
        updates._ASSET_HASHES.clear()
        updates._ASSET_HASHES.update(original_hashes)


def _check_release_catalog(fixture, errors, facts):
    # Bind candidate and rollback inputs to the package contracts and catalog assets.
    catalog = fixture.get("release_catalog") if isinstance(fixture, dict) else None
    if not isinstance(catalog, dict):
        errors.append("release_catalog: expected object")
        return
    releases = catalog.get("releases")
    if not isinstance(releases, list) or len(releases) < 2:
        errors.append(
            "release_catalog.releases: expected current and rollback releases"
        )
        return
    current, previous = releases[0], releases[1]
    current_version = current.get("version") if isinstance(current, dict) else None
    previous_version = previous.get("version") if isinstance(previous, dict) else None
    current_tuple = _version(current_version)
    previous_tuple = _version(previous_version)
    for index, release in enumerate((current, previous)):
        prefix = f"release_catalog.releases[{index}]"
        if not isinstance(release, dict):
            errors.append(f"{prefix}: expected object")
            continue
        if _version(release.get("version")) is None:
            errors.append(f"{prefix}.version: expected final X.Y.Z")
        _same(errors, f"{prefix}.channel", release.get("channel"), "stable")
        _same(errors, f"{prefix}.prerelease", release.get("prerelease"), False)
        assets = release.get("assets")
        if not isinstance(assets, list) or len(assets) != len(TARGETS):
            errors.append(f"{prefix}.assets: expected six platform assets")
    if current_tuple is not None and current_version != __version__:
        errors.append(
            "release_catalog.releases[0].version: stale current release "
            f"{current_version!r}; expected {__version__!r}"
        )
    if (
        current_tuple is not None
        and previous_tuple is not None
        and current_tuple <= previous_tuple
    ):
        errors.append("release_catalog.releases: rollback release is not older")

    matrix = catalog.get("target_matrix")
    expected_targets = [target for target, _platform in TARGETS]
    if (
        not isinstance(matrix, list)
        or [row.get("target") if isinstance(row, dict) else None for row in matrix]
        != expected_targets
    ):
        errors.append("release_catalog.target_matrix: expected canonical target order")
        matrix = []
    facts["target_matrix"] = []
    for target, platform in TARGETS:
        candidate = _asset(current, platform)
        rollback = _asset(previous, platform)
        contract = ArtifactContract.default(target, current_version or __version__)
        current_contract = ArtifactContract.default(
            target, current_version or __version__
        )
        previous_contract = ArtifactContract.default(
            target, previous_version or __version__
        )
        for name, release, asset, expected_contract in (
            ("candidate", current, candidate, current_contract),
            ("rollback", previous, rollback, previous_contract),
        ):
            release_index = 0 if name == "candidate" else 1
            prefix = f"release_catalog.releases[{release_index}]" f".assets[{platform}]"
            if not isinstance(asset, dict):
                errors.append(f"{prefix}: expected one asset")
                continue
            _same(
                errors,
                f"{prefix}.asset_name",
                asset.get("asset_name"),
                expected_contract.filename,
            )
            _same(
                errors,
                f"{prefix}.architecture",
                asset.get("architecture"),
                expected_contract.architecture,
            )
            if not updates._normalize_sha256(asset.get("sha256")):
                errors.append(f"{prefix}.sha256: expected lowercase SHA-256")
            if not isinstance(asset.get("asset_url"), str) or not asset["asset_url"]:
                errors.append(f"{prefix}.asset_url: expected URL")
        row = next(
            (
                item
                for item in matrix
                if isinstance(item, dict) and item.get("target") == target
            ),
            None,
        )
        for name, release, asset in (
            ("candidate_input", current, candidate),
            ("rollback_input", previous, rollback),
        ):
            prefix = f"release_catalog.target_matrix[{target}].{name}"
            value = row.get(name) if isinstance(row, dict) else None
            if not isinstance(value, dict) or not isinstance(asset, dict):
                errors.append(f"{prefix}: missing authenticated asset input")
                continue
            for field, expected in (
                (
                    "version",
                    release.get("version") if isinstance(release, dict) else None,
                ),
                ("asset_name", asset.get("asset_name")),
                ("asset_url", asset.get("asset_url")),
                ("sha256", asset.get("sha256")),
            ):
                _same(errors, f"{prefix}.{field}", value.get(field), expected)
        facts["target_matrix"].append(
            {
                "target": target,
                "updater_selectable": contract.updater_selectable,
                "candidate_sha256": (
                    candidate.get("sha256", "") if isinstance(candidate, dict) else ""
                ),
                "rollback_sha256": (
                    rollback.get("sha256", "") if isinstance(rollback, dict) else ""
                ),
            }
        )
        if contract.updater_selectable and not _updater_accepts(
            current, target, candidate
        ):
            errors.append(
                f"release_catalog.releases[0].assets[{platform}]: "
                "updater selection drift"
            )

    ignored = catalog.get("ignored_releases")
    nightly_rejected = False
    if not isinstance(ignored, list) or not ignored:
        errors.append("release_catalog.ignored_releases: expected nightly release")
    else:
        nightly = ignored[0]
        if not isinstance(nightly, dict):
            errors.append("release_catalog.ignored_releases[0]: expected object")
        else:
            _same(
                errors,
                "release_catalog.ignored_releases[0].channel",
                nightly.get("channel"),
                "nightly",
            )
            _same(
                errors,
                "release_catalog.ignored_releases[0].prerelease",
                nightly.get("prerelease"),
                True,
            )
            _same(
                errors,
                "release_catalog.ignored_releases[0].updater_selectable",
                nightly.get("updater_selectable"),
                False,
            )
            nightly_rejected = _nightly_rejected(nightly)
            if not nightly_rejected:
                errors.append(
                    "release_catalog.ignored_releases[0]: nightly reached updater"
                )
    facts["stable_filter"] = {"nightly_rejected": nightly_rejected}


def _check_frozen_update(fixture, manifest_raw, errors, facts):
    # Verify all frozen plugins and the host manifest activate as one release unit.
    update = fixture.get("frozen_plugin_update") if isinstance(fixture, dict) else None
    if not isinstance(update, dict):
        errors.append("frozen_plugin_update: expected object")
        return
    _same(errors, "frozen_plugin_update.channel", update.get("channel"), "stable")
    _same(errors, "frozen_plugin_update.version", update.get("version"), __version__)
    _same(errors, "frozen_plugin_update.atomic", update.get("atomic"), True)
    _same(
        errors,
        "frozen_plugin_update.activation_after_verify",
        update.get("activation_after_verify"),
        True,
    )
    host_manifest = update.get("host_manifest")
    if not isinstance(host_manifest, dict):
        errors.append("frozen_plugin_update.host_manifest: expected object")
    else:
        _same(
            errors,
            "frozen_plugin_update.host_manifest.path",
            host_manifest.get("path"),
            "ohmymeme/config/plugin-manifest.json",
        )
        _same(
            errors,
            "frozen_plugin_update.host_manifest.sha256",
            host_manifest.get("sha256"),
            hashlib.sha256(manifest_raw).hexdigest(),
        )
    plugins = update.get("plugins")
    rows = {}
    if not isinstance(plugins, list):
        errors.append("frozen_plugin_update.plugins: expected array")
        plugins = []
    ids = [row.get("id") if isinstance(row, dict) else None for row in plugins]
    if ids != _canonical_ids():
        errors.append(
            "frozen_plugin_update.plugins: expected canonical nine plugin IDs"
        )
    for row in plugins:
        if (
            isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and row["id"] not in rows
        ):
            rows[row["id"]] = row
    observed = []
    for provider_id, package_root, _capabilities in CANONICAL_PROVIDERS:
        prefix = f"frozen_plugin_update.plugins[{provider_id}]"
        row = rows.get(provider_id)
        if row is None:
            errors.append(f"{prefix}: missing official plugin")
            continue
        expected_module = f"plugins/{provider_id}/src/{package_root}/__init__.py"
        expected_frozen = f"{package_root}/__init__.py"
        _same(errors, f"{prefix}.package_root", row.get("package_root"), package_root)
        _same(
            errors, f"{prefix}.api_version", row.get("api_version"), PLUGIN_API_VERSION
        )
        _same(errors, f"{prefix}.module_path", row.get("module_path"), expected_module)
        _same(errors, f"{prefix}.frozen_path", row.get("frozen_path"), expected_frozen)
        source = ROOT / expected_module
        frozen = FROZEN_MANIFEST.parents[2] / expected_frozen
        if not source.is_file() or not frozen.is_file():
            errors.append(f"{prefix}: missing source or frozen module")
            continue
        source_hash = _sha256(source)
        if source.read_bytes() != frozen.read_bytes():
            errors.append(f"{prefix}: frozen module bytes differ from source")
        _same(errors, f"{prefix}.sha256", row.get("sha256"), source_hash)
        observed.append(
            {
                "id": provider_id,
                "api_version": row.get("api_version"),
                "sha256": source_hash,
            }
        )
    facts["frozen_plugin_update"] = {
        "atomic": update.get("atomic") is True,
        "plugins": observed,
    }


def _check_source_editable(fixture, errors, facts):
    # Keep editable source packages out of the independent release updater.
    editable = fixture.get("source_editable") if isinstance(fixture, dict) else None
    if not isinstance(editable, dict):
        errors.append("source_editable: expected object")
        return
    _same(errors, "source_editable.mode", editable.get("mode"), "source-editable")
    _same(
        errors,
        "source_editable.independent_updates",
        editable.get("independent_updates"),
        False,
    )
    _same(
        errors,
        "source_editable.official_ids",
        editable.get("official_ids"),
        _canonical_ids(),
    )
    facts["source_editable"] = {
        "independent_updates": editable.get("independent_updates") is True,
        "official_ids": editable.get("official_ids", []),
    }


def check_release_fixture(fixture):
    # Validate release inputs and frozen activation without execution.
    errors = _schema_errors(fixture, _schema(), _schema(), "fixture")
    manifest_raw, manifest_ids = _manifest_facts(errors)
    facts = {
        "official_ids": manifest_ids,
        "provider_activation": False,
        "network_execution": False,
        "installer_execution": False,
    }
    _check_host(fixture, manifest_raw, errors, facts)
    _check_release_catalog(fixture, errors, facts)
    _check_frozen_update(fixture, manifest_raw, errors, facts)
    _check_source_editable(fixture, errors, facts)
    facts["activation_allowed"] = not bool(errors)
    return errors, facts


def _expected_mappings():
    # Flatten the host-owned legacy map into immutable fixture expectations.
    return {
        (provider_id, key): (legacy_key, secret)
        for provider_id, settings in PLUGIN_SETTINGS.items()
        for key, (legacy_key, secret) in settings.items()
    }


def _check_mapping_fixture(fixture, errors):
    # Reject renamed or omitted legacy keys before creating a configuration file.
    mappings = fixture.get("mappings") if isinstance(fixture, dict) else None
    if not isinstance(mappings, list):
        errors.append("legacy_config.mappings: expected array")
        return {}
    rows = {}
    for row in mappings:
        if not isinstance(row, dict):
            continue
        provider_id, key = row.get("provider"), row.get("key")
        if not isinstance(provider_id, str) or not isinstance(key, str):
            continue
        identity = (provider_id, key)
        if identity in rows:
            errors.append(
                f"legacy_config.mappings[{provider_id}.{key}]: duplicate mapping"
            )
        else:
            rows[identity] = row
    expected = _expected_mappings()
    if set(rows) != set(expected):
        errors.append("legacy_config.mappings: unknown or missing host mapping")
    for (provider_id, key), (legacy_key, secret) in expected.items():
        row = rows.get((provider_id, key))
        if row is None:
            continue
        prefix = f"legacy_config.mappings[{provider_id}.{key}]"
        _same(errors, f"{prefix}.legacy_key", row.get("legacy_key"), legacy_key)
        _same(errors, f"{prefix}.secret", row.get("secret"), secret)
    return expected


def _write_legacy_shape(fixture, root):
    # Create encrypted historical data through Config, removing only the later mode key.
    config_path = root / "config.json"
    config = Config(config_path, root / "data")
    config.set("version", fixture["version"])
    for key, value in fixture["flat"].items():
        config.set(key, value)
    for key, value in fixture["secrets"].items():
        config.set(key, value)
    config.save()
    raw = _json(config_path.read_bytes())
    raw.pop("copy_resize_mode", None)
    config_path.write_text(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return config_path


def _write_legacy_database(path, filename):
    # Materialize the oldest known table before current MemeDB opens it.
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE memes ("
            "id INTEGER PRIMARY KEY, filename TEXT NOT NULL, "
            "file_hash TEXT NOT NULL DEFAULT '', "
            "original_name TEXT NOT NULL DEFAULT '', "
            "width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, "
            "file_size INTEGER DEFAULT 0, "
            "mime_type TEXT DEFAULT 'image/png', created_at TEXT NOT NULL DEFAULT "
            "(datetime('now','localtime')), updated_at TEXT NOT NULL DEFAULT "
            "(datetime('now','localtime')))"
        )
        connection.execute("INSERT INTO memes (filename) VALUES (?)", (filename,))
        connection.commit()
    finally:
        connection.close()


def _legacy_workspace(workspace):
    # Allocate an owned temporary root without touching existing project resources.
    if workspace is None:
        return tempfile.TemporaryDirectory(prefix=".qa-todo15-")
    path = Path(workspace)
    path.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=".qa-todo15-", dir=path)


def check_legacy_config(fixture, workspace=None):
    # Exercise Config and MemeDB migration with source fixture values.
    schema = _schema()
    errors = _schema_errors(
        fixture, schema["$defs"]["legacy_config"], schema, "legacy_config"
    )
    expected_mappings = _check_mapping_fixture(fixture, errors)
    facts = {
        "precedence": {},
        "encrypted_secret_keys": [],
        "database": {},
    }
    if not isinstance(fixture, dict):
        return errors, facts
    flat = fixture.get("flat")
    secrets = fixture.get("secrets")
    plugins = fixture.get("plugins")
    expected = fixture.get("expected")
    database_fixture = fixture.get("database")
    if (
        not isinstance(flat, dict)
        or not isinstance(secrets, dict)
        or not isinstance(plugins, dict)
        or not isinstance(expected, dict)
        or not isinstance(database_fixture, dict)
        or not isinstance(database_fixture.get("filename"), str)
    ):
        errors.append("legacy_config: invalid configuration source values")
        return errors, facts
    if set(secrets) != set(_SECRET_KEYS):
        errors.append("legacy_config.secrets: expected every legacy encrypted key")
    for (provider_id, key), (legacy_key, secret) in expected_mappings.items():
        source = secrets.get(legacy_key) if secret else flat.get(legacy_key)
        if source is None:
            errors.append(f"legacy_config.{legacy_key}: missing source value")
            continue
        default = Config.DEFAULTS.get(legacy_key)
        if source == default:
            errors.append(f"legacy_config.{legacy_key}: source value matches default")
        scoped = plugins.get(provider_id)
        value = scoped.get(key) if isinstance(scoped, dict) else None
        if value is None:
            errors.append(
                f"legacy_config.plugins[{provider_id}.{key}]: missing source value"
            )
        elif value == source:
            errors.append(
                f"legacy_config.plugins[{provider_id}.{key}]: "
                "must differ from legacy value"
            )
    if errors:
        return errors, facts
    try:
        with _legacy_workspace(workspace) as temporary:
            root = Path(temporary)
            config_path = _write_legacy_shape(fixture, root)
            config = Config(config_path, root / "data")
            for (provider_id, key), (legacy_key, secret) in expected_mappings.items():
                config.set_plugin_value(
                    provider_id, key, plugins[provider_id][key], secret=secret
                )
            config.save()
            round_trip = Config(config_path, root / "data")
            _same(
                errors,
                "legacy_config.expected.version",
                round_trip.get("version"),
                expected.get("version"),
            )
            _same(
                errors,
                "legacy_config.expected.copy_resize_mode",
                round_trip.get("copy_resize_mode"),
                expected.get("copy_resize_mode"),
            )
            facts["version"] = round_trip.get("version")
            facts["copy_resize_mode"] = round_trip.get("copy_resize_mode")
            for (provider_id, key), (legacy_key, secret) in expected_mappings.items():
                source = secrets[legacy_key] if secret else flat[legacy_key]
                value = round_trip.get_plugin_value(
                    provider_id, key, legacy_key, secret=secret
                )
                prefix = f"legacy_config.precedence[{provider_id}.{key}]"
                if value != source:
                    errors.append(f"{prefix}: legacy value did not win")
                facts["precedence"][f"{provider_id}.{key}"] = (
                    "configured" if secret else value
                )
            saved = _json(config_path.read_bytes())
            for key, value in secrets.items():
                if saved.get(key) == value or round_trip.get(key) != value:
                    errors.append(
                        f"legacy_config.secrets[{key}]: encrypted round trip failed"
                    )
                else:
                    facts["encrypted_secret_keys"].append(key)

            database_path = root / "legacy.db"
            filename = database_fixture["filename"]
            _write_legacy_database(database_path, filename)
            database = MemeDB(database_path)
            try:
                if database.get_by_filename(filename) is None:
                    errors.append("legacy_config.database: migrated row is unavailable")
                journal = (
                    database._get_conn().execute("PRAGMA journal_mode").fetchone()[0]
                )
                columns = {
                    row[1]
                    for row in database._get_conn()
                    .execute("PRAGMA table_info(memes)")
                    .fetchall()
                }
                migrated = sorted(
                    column
                    for column in ("sort_order", "stego_of_hash", "from_stego")
                    if column in columns
                )
                if migrated != ["from_stego", "sort_order", "stego_of_hash"]:
                    errors.append(
                        "legacy_config.database: migration columns are missing"
                    )
                facts["database"] = {
                    "filename": filename,
                    "journal_mode": journal,
                    "migrated_columns": migrated,
                }
            finally:
                database.close()
    except Exception as error:
        errors.append(f"legacy_config.runtime: {type(error).__name__}: {error}")
    facts["encrypted_secret_keys"].sort()
    return errors, facts


def _write_report(path, report):
    # Atomically replace a stale PASS so every run leaves fresh evidence.
    data = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv=None):
    # Run one fail-closed offline compatibility check and always publish its report.
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--legacy-config", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    report = {
        "schema_version": 1,
        "status": "REJECTED",
        "network_execution": False,
        "installer_execution": False,
        "provider_activation": False,
        "errors": [],
    }
    try:
        fixture_raw = args.fixture.read_bytes()
        legacy_raw = args.legacy_config.read_bytes()
        fixture = _json(fixture_raw)
        legacy = _json(legacy_raw)
        release_errors, release_facts = check_release_fixture(fixture)
        legacy_errors, legacy_facts = check_legacy_config(legacy)
        errors = release_errors + legacy_errors
        report.update(
            {
                "errors": errors,
                "observed": release_facts | {"legacy_config": legacy_facts},
                "input_hashes": {
                    "fixture": hashlib.sha256(fixture_raw).hexdigest(),
                    "legacy_config": hashlib.sha256(legacy_raw).hexdigest(),
                    "schema": _sha256(SCHEMA),
                    "host_manifest": _sha256(SOURCE_MANIFEST),
                },
                "validator_sha256": _sha256(Path(__file__)),
            }
        )
        if not errors:
            report["status"] = "PASS"
    except Exception as error:
        report["errors"] = [str(error) or type(error).__name__]
    _write_report(args.report, report)
    for error in report["errors"]:
        print(error, file=sys.stderr)
    print(
        f"{report['status']}: release compatibility "
        "(network/install/providers not executed)"
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
