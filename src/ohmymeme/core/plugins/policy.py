# pyright: basic

"""插件配置、能力、密钥和临时工作区的宿主边界。"""

import shutil
import tempfile
from pathlib import Path

from ohmymeme.core.config import _SECRET_KEYS

from .contracts import PluginCapabilities, PluginDescriptor
from .manifest import CANONICAL_BY_ID, ENTRY_POINT_GROUP, PLUGIN_API_VERSION


class PluginPolicyError(RuntimeError):
    pass


PLUGIN_SETTINGS = {
    "source.qqnt": {
        "ini_path": ("qqnt_ini_path", False),
        "userdata_path": ("qqnt_userdata_path", False),
    },
    "source.telegram": {"tdata_path": ("tg_tdata_path", False)},
    "source.douyin": {},
    "source.wechat": {},
    "sync.ftp": {
        "host": ("ftp_host", False),
        "port": ("ftp_port", False),
        "user": ("ftp_user", False),
        "password": ("ftp_password", True),
        "path": ("ftp_path", False),
    },
    "sync.s3": {
        "endpoint": ("s3_endpoint", False),
        "region": ("s3_region", False),
        "bucket": ("s3_bucket", False),
        "access_key": ("s3_access_key", True),
        "secret_key": ("s3_secret_key", True),
        "path": ("s3_path", False),
    },
    "sync.r2": {
        "account_id": ("r2_account_id", False),
        "access_key_id": ("r2_access_key_id", True),
        "secret_access_key": ("r2_secret_access_key", True),
        "bucket": ("r2_bucket", False),
        "path": ("r2_path", False),
    },
    "sync.webdav": {
        "url": ("webdav_url", False),
        "user": ("webdav_user", False),
        "password": ("webdav_password", True),
        "path": ("webdav_path", False),
    },
    "transport.lan": {
        "port": ("lan_port", False),
        "secret": ("lan_secret", True),
    },
}

_REDACTED = "[REDACTED]"
_SCOPED_SECRET_LABELS = {
    "passcode",
    "cookie",
    "access_key",
    "access_key_id",
    "password",
    "secret",
    "secret_access_key",
    "secret_key",
}
_SECRET_LABELS = frozenset(_SECRET_KEYS) | _SCOPED_SECRET_LABELS


def redact(value, secret_values=()):
    """删除可序列化输出中的已知密钥字段。"""
    if isinstance(value, dict):
        return {
            key: (
                _REDACTED
                if isinstance(key, str) and key.casefold() in _SECRET_LABELS
                else redact(item, secret_values)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secret_values) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, secret_values) for item in value)
    if isinstance(value, str):
        for secret in secret_values:
            if secret:
                value = value.replace(secret, _REDACTED)
    return value


class PluginConfigPort:
    def __init__(self, settings):
        self._settings = dict(settings)
        self._changes = []

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)

    def set_setting(self, key, value):
        self._settings[key] = value
        self._changes.append((key, value))

    def snapshot(self):
        # 供 worker 操作下发只读快照
        return dict(self._settings)

    def _drain_changes(self):
        changes = tuple(self._changes)
        self._changes.clear()
        return changes


class PluginSecretPort:
    def __init__(self, secrets):
        self._secrets = dict(secrets)

    def get_secret(self, key):
        return self._secrets.get(key)

    def snapshot(self):
        # 供 worker 操作下发按操作注入的密钥
        return dict(self._secrets)

    def _values(self):
        # Match the provider's Cookie splitting without persisting derived values.
        values = [value for value in self._secrets.values() if isinstance(value, str)]
        cookie = self._secrets.get("cookie")
        if isinstance(cookie, str):
            for item in cookie.split(";"):
                if "=" in item:
                    values.append(item.split("=", 1)[1].strip())
        return tuple(sorted(filter(None, values), key=len, reverse=True))


class PluginOutputPort:
    def __init__(self):
        self._events = []

    def emit(self, kind, value):
        self._events.append((kind, value))

    def _drain_events(self):
        events = tuple(self._events)
        self._events.clear()
        return events


class ScopedSettings:
    def __init__(self, config_port, provider_id, closed):
        self._config_port = config_port
        self._provider_id = provider_id
        self._closed = closed

    def get(self, key, default=None):
        _legacy_key, secret = self._setting(key)
        if secret:
            raise PluginPolicyError("secret config requires operation secrets")
        return self._config_port.get_setting(key, default)

    def set(self, key, value):
        _legacy_key, secret = self._setting(key)
        if secret:
            raise PluginPolicyError("secret config requires operation secrets")
        self._config_port.set_setting(key, value)

    def _setting(self, key):
        if self._closed():
            raise PluginPolicyError("operation scope is closed")
        setting = PLUGIN_SETTINGS[self._provider_id].get(key)
        if setting is None:
            raise PluginPolicyError(f"unknown config key {key!r}")
        return setting


class ScopedSecrets:
    def __init__(self, secret_port, provider_id, closed):
        self._secret_port = secret_port
        self._provider_id = provider_id
        self._closed = closed

    def get(self, key):
        if self._closed():
            raise PluginPolicyError("operation scope is closed")
        setting = PLUGIN_SETTINGS[self._provider_id].get(key)
        transient = {"source.telegram": {"passcode"}, "source.douyin": {"cookie"}}
        if (setting is None or not setting[1]) and key not in transient.get(
            self._provider_id, set()
        ):
            raise PluginPolicyError(f"unknown secret key {key!r}")
        return self._secret_port.get_secret(key)


class TemporaryWorkspace:
    def __init__(self, root):
        self._root = Path(root).resolve()
        self._closed = False

    @property
    def root(self):
        # worker 侧与宿主侧共用同一个操作临时根
        return self._root

    def path(self, relative):
        if self._closed:
            raise PluginPolicyError("operation scope is closed")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise PluginPolicyError("temporary path traversal")
        destination = (self._root / candidate).resolve()
        if self._root != destination and self._root not in destination.parents:
            raise PluginPolicyError("temporary path traversal")
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination

    def close(self):
        if not self._closed:
            shutil.rmtree(self._root, ignore_errors=True)
            self._closed = True


class PluginOperation:
    def __init__(self, config_port, secret_port, descriptor, workspace_root):
        self._descriptor = descriptor
        self._canonical_capabilities = frozenset(descriptor.capabilities.names)
        self._closed = False
        root = workspace_root / descriptor.id
        root.mkdir(parents=True, exist_ok=True)
        self.temporary = TemporaryWorkspace(
            tempfile.mkdtemp(prefix="operation-", dir=root)
        )
        self.settings = ScopedSettings(config_port, descriptor.id, self._is_closed)
        self.secrets = ScopedSecrets(secret_port, descriptor.id, self._is_closed)
        self._outputs = PluginOutputPort()
        self._redaction_values = set()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.temporary.close()
        self._closed = True

    def require(self, capability):
        if self._closed:
            raise PluginPolicyError("operation scope is closed")
        if capability not in self._canonical_capabilities:
            raise PluginPolicyError(f"undeclared capability {capability!r}")

    # 下发 worker 的非密钥配置快照
    def export_config(self):
        return self.settings._config_port.snapshot()

    # 下发 worker 的按操作密钥快照
    def export_secrets(self):
        return self.secrets._secret_port.snapshot()

    def redact(self, value):
        return redact(
            value, (*self.secrets._secret_port._values(), *self._redaction_values)
        )

    def protect_secret(self, value):
        # Helper/source-derived keys are transient output redaction, never config.
        if self._closed:
            raise PluginPolicyError("operation scope is closed")
        if isinstance(value, str) and value:
            self._redaction_values.add(value)

    def serialize_descriptor(self, value):
        return self.redact(value)

    serialize_progress = serialize_descriptor
    serialize_error = serialize_descriptor
    serialize_log = serialize_descriptor

    def emit_descriptor(self, value):
        self._outputs.emit("descriptor", value)

    def emit_progress(self, value):
        self._outputs.emit("progress", value)

    def emit_error(self, value):
        self._outputs.emit("error", value)

    def emit_log(self, value):
        self._outputs.emit("log", value)

    def serialize_output(self, kind, value):
        match kind:
            case "descriptor":
                return self.serialize_descriptor(value)
            case "progress":
                return self.serialize_progress(value)
            case "error":
                return self.serialize_error(value)
            case "log":
                return self.serialize_log(value)
        raise PluginPolicyError(f"unknown output kind {kind!r}")

    def _is_closed(self):
        return self._closed


class PluginPolicy:
    def __init__(self, config):
        self._config = config
        self._workspace_root = config.data_dir / "plugin-workspaces"
        self._operations = {}

    def operation(self, descriptor, transient_secrets=None):
        canonical = self._canonical_descriptor(descriptor)
        config_port, secret_port = self._ports(canonical.id)
        allowed = {"source.telegram": {"passcode"}, "source.douyin": {"cookie"}}
        transient = dict(transient_secrets or {})
        if transient.keys() - allowed.get(canonical.id, set()):
            raise PluginPolicyError("unknown transient secret")
        secret_port._secrets.update(transient)
        operation = PluginOperation(
            config_port, secret_port, canonical, self._workspace_root
        )
        self._operations[operation] = (canonical.id, config_port)
        return operation

    def close_operation(self, operation):
        # Close only after the host has drained the worker and its children.
        operation.__exit__(None, None, None)
        operation.secrets._secret_port._secrets.clear()
        operation._redaction_values.clear()
        self._operations.pop(operation, None)

    def commit_settings(self, operation):
        provider_id, config_port = self._operation_record(operation)
        for key, value in config_port._drain_changes():
            self._config.set_plugin_value(provider_id, key, value)

    def flush_outputs(self, operation, emitter):
        self._operation_record(operation)
        for kind, value in operation._outputs._drain_events():
            emitter(kind, operation.serialize_output(kind, value))

    def _ports(self, provider_id):
        settings = {}
        secrets = {}
        for key, (legacy_key, secret) in PLUGIN_SETTINGS[provider_id].items():
            value = self._config.get_plugin_value(
                provider_id, key, legacy_key, secret=secret
            )
            if secret:
                secrets[key] = value
            else:
                settings[key] = value

        return PluginConfigPort(settings), PluginSecretPort(secrets)

    def _operation_record(self, operation):
        record = self._operations.get(operation)
        if record is None:
            raise PluginPolicyError("operation does not belong to this policy")
        return record

    def _canonical_descriptor(self, descriptor):
        if not isinstance(descriptor, PluginDescriptor):
            raise PluginPolicyError("descriptor does not match canonical descriptor")
        expected = CANONICAL_BY_ID.get(descriptor.id)
        if expected is None or descriptor.id not in PLUGIN_SETTINGS:
            raise PluginPolicyError(f"unknown plugin ID {descriptor.id!r}")
        provider_id, package_root, capabilities = expected
        canonical = PluginDescriptor(
            provider_id,
            PLUGIN_API_VERSION,
            package_root,
            ENTRY_POINT_GROUP,
            provider_id,
            f"{package_root}:create_plugin",
            PluginCapabilities(capabilities),
        )
        if descriptor != canonical:
            raise PluginPolicyError("descriptor does not match canonical descriptor")
        return canonical
