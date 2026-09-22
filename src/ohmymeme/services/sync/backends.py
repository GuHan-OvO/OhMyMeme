import shutil
import sys
import tempfile
from functools import wraps
from pathlib import Path

from ohmymeme.core.assets import is_safe_filename
from ohmymeme.core.plugins.network_config import (
    SYNC_CONFIGS,
    SYNC_SECRETS,
    SyncError,
    validate_sync_config,
)
from ohmymeme.core.plugins.policy import PluginSecretPort, redact


def _redact_exception(error, secrets):
    # Keep exception classes/codes, including structured SDK errors and URL reasons.
    seen = set()

    def clean(value):
        if isinstance(value, BaseException):
            if id(value) in seen:
                return value
            seen.add(id(value))
            value.args = clean(value.args)
            value.__dict__.update(clean(value.__dict__))
            for name in ("strerror", "filename", "filename2"):
                text = getattr(value, name, None)
                if isinstance(text, str):
                    setattr(value, name, redact(text, secrets))
            value.__cause__ = None
            value.__context__ = None
            value.__traceback__ = None
            return value
        if isinstance(value, dict):
            return {key: clean(item) for key, item in redact(value, secrets).items()}
        if isinstance(value, (tuple, list)):
            return type(value)(clean(item) for item in value)
        return redact(value, secrets)

    return clean(error)


def _protect_errors(method):
    # Only explicitly decorated host methods cross this boundary; no dynamic action API.
    @wraps(method)
    def protected(self, *args, **kwargs):
        port = vars(self).get("_secret_port")
        secrets = port._values() if port is not None else None
        try:
            return method(self, *args, **kwargs)
        except Exception as error:
            if secrets is None:
                # Construction may have created the scope after entering this guard.
                port = vars(self).get("_secret_port")
                secrets = port._values() if port is not None else ()
            try:
                safe = _redact_exception(error, secrets)
            finally:
                if port is not None and vars(self).get("_closed"):
                    port._secrets.clear()
            raise safe.with_traceback(None) from None

    return protected


def configuration(cfg, provider_id):
    # Project legacy/namespaced values into a non-global, secret-free record.
    kind = provider_id.split(".")[1]

    def read(key, default, secret=False):
        legacy = f"{kind}_{key}"
        getter = getattr(cfg, "get_plugin_value", None)
        if callable(getter):
            return getter(provider_id, key, legacy, default=default, secret=secret)
        return cfg.get(legacy, default)

    record = SYNC_CONFIGS[provider_id]
    values = {k: read(k, v) for k, v in record._field_defaults.items()}
    if provider_id == "sync.ftp":
        if type(values["tls"]) is not bool:
            raise SyncError("ftp_tls: expected bool")
        values["tls"] = values["tls"] or cfg.get("sync_type") == "ftps"
    snapshot = validate_sync_config(provider_id, record(**values))
    secrets = {}
    for key in SYNC_SECRETS[provider_id]:
        value = read(key, "", secret=True)
        if type(value) is not str:
            raise SyncError(f"{kind}_{key}: expected str")
        if provider_id == "sync.r2" and not value:
            raise SyncError(f"{kind}_{key}: not configured")
        secrets[key] = value
    return snapshot, secrets


class _SessionBackend:
    """宿主适配层看到的 backend 端口：固定方法经会话 RPC 转发到 worker。"""

    def __init__(self, session):
        self._session = session

    def connect(self):
        return self._session.call("connect")

    def upload_file(self, local_path, remote_path):
        return self._session.call("upload_file", (str(local_path), remote_path))

    def download_file(self, remote_path, local_path):
        return self._session.call("download_file", (remote_path, str(local_path)))

    def list_files(self, path):
        return self._session.call("list_files", (path,))

    def ensure_remote_dir(self, path):
        return self._session.call("ensure_remote_dir", (path,))

    def file_exists(self, path):
        return self._session.call("file_exists", (path,))

    def delete_file(self, path):
        return self._session.call("delete_file", (path,))

    def test_connection(self):
        return self._session.call("test_connection")

    def clear_directory_cache(self):
        return self._session.call("clear_directory_cache")

    def close(self):
        self._session.close()

    def __getattr__(self, name):
        # 读取转发给会话；写入仍落在适配器实例上
        return getattr(self._session, name)


class _SyncBackend:
    provider_id = None

    @_protect_errors
    def __init__(self, cfg, registry=None, enabled=None, runtime=None):
        # Validate before runtime admission, even when the provider is disabled.
        snapshot, secrets = configuration(cfg, self.provider_id)
        self._closed = False
        self._secret_port = PluginSecretPort(secrets)
        self._temporary = None
        if enabled is not None and self.provider_id not in enabled:
            self._closed = True
            raise SyncError(f"{self.provider_id}: provider_disabled")
        if runtime is None:
            self._closed = True
            raise SyncError(f"{self.provider_id}: provider_unavailable")
        try:
            self._session = runtime.open_backend(
                self.provider_id, snapshot, secrets, enabled=enabled
            )
        except Exception as error:
            self._closed = True
            raise SyncError(str(error)) from error
        self._backend = _SessionBackend(self._session)

    @_protect_errors
    def connect(self):
        # Failed connects cannot retain usable credentials or partial connections.
        if self._closed:
            raise SyncError("operation scope is closed")
        try:
            self._backend.connect()
        except Exception as error:
            self.close()
            raise SyncError(str(error)) from error

    def _stage(self):
        # Persistent host paths never enter a network implementation.
        if self._closed:
            raise SyncError("operation scope is closed")
        if self._temporary is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="ohmm-sync-")
        return Path(self._temporary.name) / "payload"

    @_protect_errors
    def upload_file(self, local_path, remote_path):
        # Copy each host asset into the backend's independent operation workspace.
        try:
            staged = self._stage()
            shutil.copyfile(local_path, staged)
            return self._boolean(
                self._backend.upload_file(staged, remote_path), "upload_file"
            )
        except OSError:
            return False
        finally:
            if self._temporary is not None:
                (Path(self._temporary.name) / "payload").unlink(missing_ok=True)

    @_protect_errors
    def download_file(self, remote_path, local_path):
        # Only successful byte transfers are projected back to the host destination.
        try:
            staged = self._stage()
            if not self._boolean(
                self._backend.download_file(remote_path, staged), "download_file"
            ):
                return False
            local_path = Path(local_path)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staged, local_path)
            return True
        except OSError:
            return False
        finally:
            if self._temporary is not None:
                (Path(self._temporary.name) / "payload").unlink(missing_ok=True)

    @_protect_errors
    def list_files(self, path):
        # Remote names are untrusted; unsafe children must never reach deletion code.
        self._ensure_open()
        names = self._backend.list_files(path)
        if not isinstance(names, list):
            raise SyncError("list_files: expected list")
        return [name for name in names if is_safe_filename(name)]

    def _boolean(self, value, method):
        # Malformed truthy provider results cannot be treated as committed transfers.
        if type(value) is not bool:
            raise SyncError(f"{method}: expected bool")
        return value

    def _ensure_open(self):
        # No closed adapter can issue a new network operation.
        if self._closed:
            raise SyncError("operation scope is closed")

    @_protect_errors
    def ensure_remote_dir(self, path):
        # Directory semantics stay provider-specific.
        self._ensure_open()
        return self._backend.ensure_remote_dir(path)

    @_protect_errors
    def file_exists(self, path):
        # Preserve false versus exception without selecting a fallback provider.
        self._ensure_open()
        return self._boolean(self._backend.file_exists(path), "file_exists")

    @_protect_errors
    def delete_file(self, path):
        # Only the host's validated remote path is supplied to the provider.
        self._ensure_open()
        return self._boolean(self._backend.delete_file(path), "delete_file")

    @_protect_errors
    def test_connection(self):
        # Connection probes retain their existing public diagnostics, without secrets.
        self._ensure_open()
        return self._backend.test_connection()

    def __getattr__(self, name):
        # Preserve published backend operations and the legacy inspection attributes.
        return getattr(self._backend, name)

    @_protect_errors
    def close(self):
        # Closing revokes operation secrets and removes only host-owned staging.
        if self._closed:
            return
        primary = sys.exception()
        failure = None
        self._closed = True
        try:
            self._backend.close()
        except Exception as error:
            failure = error
        finally:
            self._secret_port._secrets.clear()
            if self._temporary is not None:
                try:
                    self._temporary.cleanup()
                except Exception as error:
                    if failure is None:
                        failure = error
                else:
                    self._temporary = None
        if failure is not None:
            if primary is None:
                raise failure
            primary.add_note("Sync backend cleanup failed while handling this error")


class _FtpBackend(_SyncBackend):
    provider_id = "sync.ftp"


class _S3Backend(_SyncBackend):
    provider_id = "sync.s3"


class _R2Backend(_SyncBackend):
    provider_id = "sync.r2"


class _WebDAVBackend(_SyncBackend):
    provider_id = "sync.webdav"


def get_backend(cfg, registry=None, enabled=None, runtime=None):
    # Select exactly one provider; absence never selects a different backend.
    kind = cfg.get("sync_type", "")
    backend = (
        {
            "ftp": _FtpBackend,
            "ftps": _FtpBackend,
            "s3": _S3Backend,
            "r2": _R2Backend,
            "webdav": _WebDAVBackend,
        }.get(kind)
        if isinstance(kind, str)
        else None
    )
    if backend is None:
        raise SyncError("No sync type configured")
    return backend(cfg, registry, enabled, runtime)
