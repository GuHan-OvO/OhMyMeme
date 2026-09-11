# pyright: basic

import json

from .contracts import PluginCapabilities, PluginDescriptor

PLUGIN_API_VERSION = 1
ENTRY_POINT_GROUP = "ohmymeme.plugins.v1"
CANONICAL_PROVIDERS = (
    (
        "source.qqnt",
        "ohmymeme_plugin_qqnt",
        ("filesystem.read", "import.submit"),
    ),
    (
        "source.telegram",
        "ohmymeme_plugin_telegram",
        ("filesystem.read", "import.submit", "process.ffmpeg"),
    ),
    (
        "source.douyin",
        "ohmymeme_plugin_douyin",
        ("import.submit", "network.https"),
    ),
    (
        "source.wechat",
        "ohmymeme_plugin_wechat",
        ("filesystem.read", "import.submit", "network.https", "process.helper"),
    ),
    ("sync.ftp", "ohmymeme_plugin_sync_ftp", ("network.remote", "sync.remote")),
    ("sync.s3", "ohmymeme_plugin_sync_s3", ("network.remote", "sync.remote")),
    ("sync.r2", "ohmymeme_plugin_sync_r2", ("network.remote", "sync.remote")),
    (
        "sync.webdav",
        "ohmymeme_plugin_sync_webdav",
        ("network.remote", "sync.remote"),
    ),
    (
        "transport.lan",
        "ohmymeme_plugin_lan",
        (
            "device.approval",
            "lan.byte-transport",
            "lan.commands",
            "lan.replay-policy",
            "lan.secure-session",
            "lan.security",
        ),
    ),
)
CANONICAL_BY_ID = {item[0]: item for item in CANONICAL_PROVIDERS}


class PluginManifestError(ValueError):
    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


def canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_document(value):
    return canonical_bytes(value) + b"\n"


def _required_errors(value, fields, prefix):
    if not isinstance(value, dict):
        return [f"{prefix}: expected object"]
    return [
        f"{prefix}.{field}: missing required field"
        for field in fields
        if field not in value
    ]


def _validate_entry_point(value, prefix, provider_id, package_root):
    errors = _required_errors(value, ("group", "name", "value"), prefix)
    if not isinstance(value, dict):
        return errors
    expected = (ENTRY_POINT_GROUP, provider_id, f"{package_root}:create_plugin")
    actual = tuple(value.get(field) for field in ("group", "name", "value"))
    if actual != expected:
        errors.append(f"{prefix}: expected {expected!r}")
    return errors


def _validate_capabilities(value, prefix, provider_id, expected):
    if not isinstance(value, list):
        return [f"{prefix}: expected array"]
    errors = []
    for index, capability in enumerate(value):
        if not isinstance(capability, str):
            errors.append(f"{prefix}[{index}]: expected string")
        elif capability not in expected:
            errors.append(
                f"{prefix}[{index}]: undeclared capability {capability!r} "
                f"for {provider_id}"
            )
    if any(capability in value[:index] for index, capability in enumerate(value)):
        errors.append(f"{prefix}: duplicate capability")
    if tuple(value) != expected:
        errors.append(f"{prefix}: expected canonical capabilities")
    return errors


def _validate_plugin(value, index):
    prefix = f"plugins[{index}]"
    errors = _required_errors(
        value,
        ("api_version", "capabilities", "entry_point", "id", "package_root"),
        prefix,
    )
    if not isinstance(value, dict):
        return errors, None
    provider_id = value.get("id")
    expected = (
        CANONICAL_BY_ID.get(provider_id) if isinstance(provider_id, str) else None
    )
    if expected is None:
        errors.append(f"{prefix}.id: unknown plugin ID {provider_id!r}")
        return errors, None
    _, package_root, capabilities = expected
    if (
        type(value.get("api_version")) is not int
        or value.get("api_version") != PLUGIN_API_VERSION
    ):
        errors.append(f"{prefix}.api_version: expected {PLUGIN_API_VERSION}")
    if value.get("package_root") != package_root:
        errors.append(f"{prefix}.package_root: expected {package_root}")
    errors.extend(
        _validate_entry_point(
            value.get("entry_point"),
            f"{prefix}.entry_point",
            provider_id,
            package_root,
        )
    )
    errors.extend(
        _validate_capabilities(
            value.get("capabilities"),
            f"{prefix}.capabilities",
            provider_id,
            capabilities,
        )
    )
    descriptor = PluginDescriptor(
        provider_id,
        PLUGIN_API_VERSION,
        package_root,
        ENTRY_POINT_GROUP,
        provider_id,
        f"{package_root}:create_plugin",
        PluginCapabilities(capabilities),
    )
    return errors, descriptor


def validate_manifest(value, entry_point_group):
    errors = _required_errors(
        value, ("api_version", "entry_point_group", "plugins"), "manifest"
    )
    if not isinstance(value, dict):
        raise PluginManifestError(errors)
    if entry_point_group != ENTRY_POINT_GROUP:
        errors.append(f"entry_point_group: expected {ENTRY_POINT_GROUP}")
    if value.get("entry_point_group") != entry_point_group:
        errors.append(f"manifest.entry_point_group: expected {entry_point_group}")
    if (
        type(value.get("api_version")) is not int
        or value.get("api_version") != PLUGIN_API_VERSION
    ):
        errors.append(f"manifest.api_version: expected {PLUGIN_API_VERSION}")
    plugins = value.get("plugins")
    if not isinstance(plugins, list):
        errors.append("manifest.plugins: expected array")
        raise PluginManifestError(errors)
    descriptors = []
    ids = []
    for index, plugin in enumerate(plugins):
        plugin_errors, descriptor = _validate_plugin(plugin, index)
        errors.extend(plugin_errors)
        if descriptor is not None:
            descriptors.append(descriptor)
        if isinstance(plugin, dict):
            ids.append(plugin.get("id"))
    duplicates = []
    for index, provider_id in enumerate(ids):
        if isinstance(provider_id, str) and provider_id in ids[:index]:
            if provider_id not in duplicates:
                duplicates.append(provider_id)
    for provider_id in duplicates:
        errors.append(f"plugins: duplicate plugin ID {provider_id!r}")
    expected_ids = tuple(item[0] for item in CANONICAL_PROVIDERS)
    if tuple(ids) != expected_ids:
        errors.append("plugins: expected canonical ID order")
    if errors:
        raise PluginManifestError(errors)
    return tuple(descriptors)
