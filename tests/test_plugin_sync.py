import importlib
import json
import logging
import traceback
from ftplib import error_perm
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import pytest

from ohmymeme.services.sync.backends import SyncError, get_backend
from sync_backend_fixtures import FailingRuntime, LocalSyncRuntime

WIRE_SECRET = "TODO10-SYNTHETIC-SECRET-ONLY"


def config(kind):
    # Supply only declared fake-wire endpoints and credentials.
    return {
        "sync_type": kind,
        "ftp_host": "fixture.invalid",
        "s3_endpoint": "https://fixture.invalid",
        "s3_bucket": "bucket",
        "r2_account_id": "account",
        "r2_bucket": "bucket",
        "r2_access_key_id": "access",
        "r2_secret_access_key": "secret",
        "webdav_url": "https://fixture.invalid",
    }


def backend(kind, *, enabled=None, runtime=None, **overrides):
    # 分层夹具：宿主适配层不变，会话调用本地插件 backend
    return get_backend(
        dict(config(kind), **overrides),
        enabled=enabled,
        runtime=LocalSyncRuntime() if runtime is None else runtime,
    )


def _plugin_backend(adapter):
    # 取宿主机适配层背后的真实插件 backend
    return adapter._backend._session.backend


@pytest.mark.parametrize("kind", ["ftp", "s3", "r2", "webdav"])
def test_real_factory_and_independent_backends(kind):
    # Factories live in physical distributions, never import host implementations.
    module = importlib.import_module(f"ohmymeme_plugin_sync_{kind}")
    assert "/plugins/" in Path(module.__file__).as_posix()
    assert module.create_plugin() is not module.create_plugin()
    first, second = backend(kind), backend(kind)
    try:
        assert _plugin_backend(first) is not _plugin_backend(second)
        assert type(_plugin_backend(first)).__module__.startswith(
            "ohmymeme_plugin_sync_"
        )
        assert not hasattr(_plugin_backend(first), "cfg")
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("ftp", "ftp_port", True),
        ("ftp", "ftp_tls", 1),
        ("s3", "s3_addressing_style", "other"),
        ("s3", "s3_multipart_threshold", True),
        ("r2", "r2_bucket", []),
        ("webdav", "webdav_timeout", "abc"),
    ],
)
def test_invalid_config_rejected_before_provider_or_network(kind, field, value):
    # Invalid scalar types cannot be coerced into valid network configuration.
    runtime = LocalSyncRuntime()
    with patch("socket.socket", side_effect=AssertionError("network")) as network:
        with pytest.raises(SyncError, match=field):
            get_backend(dict(config(kind), **{field: value}), runtime=runtime)
    assert not runtime.sessions
    network.assert_not_called()


@pytest.mark.parametrize("kind", ["ftp", "s3", "r2", "webdav"])
def test_missing_and_disabled_provider_never_fallback(kind):
    with patch("socket.socket", side_effect=AssertionError("network")):
        with pytest.raises(SyncError, match="plugin_missing"):
            backend(kind, runtime=FailingRuntime("plugin_missing"))
        with pytest.raises(SyncError, match="provider_disabled"):
            backend(kind, enabled=())


def test_ftp_error_results_and_close_revoke_secrets(tmp_path):
    # Exercise the real factory, host file projection, and actual FTP operation code.
    module = importlib.import_module("ohmymeme_plugin_sync_ftp")
    wire = Mock()
    wire.storbinary.side_effect = OSError("wire failed")
    with patch.object(module, "FTP", return_value=wire):
        bk = backend("ftp")
        secret_getter = bk._backend.secrets
        bk.connect()
        source = tmp_path / "asset.bin"
        source.write_bytes(b"fixture")
        assert bk.upload_file(source, "/memes/a.bin") is False
        wire.retrlines.side_effect = OSError("list failed")
        with pytest.raises(OSError, match="list failed"):
            bk.list_files("/memes")
        bk.close()
        bk.close()
        with pytest.raises(RuntimeError, match="closed"):
            secret_getter.get("password")
    wire.quit.assert_called_once()


def test_namespaced_ftp_path_matches_wire_snapshot(tmp_path):
    # Host planning and the provider must read the same old-key-precedence mapping.
    from ohmymeme.core.config import Config
    from ohmymeme.services.sync.planning import _remote_root

    cfg = Config(tmp_path / "config.json", tmp_path / "data")
    cfg.set("sync_type", "ftp")
    cfg.set_plugin_value("sync.ftp", "host", "fixture.invalid")
    cfg.set_plugin_value("sync.ftp", "path", "/namespaced")
    bk = get_backend(cfg, runtime=LocalSyncRuntime())
    try:
        assert _remote_root(cfg) == bk._backend.config.path == "/namespaced"
    finally:
        bk.close()


def test_s3_credentials_do_not_use_ambient_sdk_chain():
    # Even an empty credential snapshot must not query global environment/metadata.
    wire = Mock()
    with patch("boto3.client", return_value=wire) as client:
        bk = backend("s3")
        try:
            bk.connect()
            assert client.call_args.kwargs["aws_access_key_id"] == ""
            assert client.call_args.kwargs["aws_secret_access_key"] == ""
        finally:
            bk.close()


def test_r2_reuses_only_s3_code_not_disabled_provider():
    # A disabled S3 provider does not disable the separately selected R2 provider.
    with patch("boto3.client", return_value=Mock()):
        bk = get_backend(
            config("r2"),
            enabled=("sync.r2",),
            runtime=LocalSyncRuntime(),
        )
        try:
            bk.connect()
            assert type(_plugin_backend(bk)).__module__ == "ohmymeme_plugin_sync_r2"
        finally:
            bk.close()


def test_ftps_default_protects_data_and_reconnects_independently():
    # Reconnect replaces and closes the old network object without changing peers.
    module = importlib.import_module("ohmymeme_plugin_sync_ftp")
    first, second = Mock(), Mock()
    with patch.object(module, "FTP_TLS", side_effect=[first, second]):
        bk = backend("ftps")
        try:
            bk.connect()
            bk.connect()
            first.prot_p.assert_called_once()
            second.prot_p.assert_called_once()
            first.quit.assert_called_once()
        finally:
            bk.close()
        second.quit.assert_called_once()


def test_sync_listing_rejects_untrusted_children():
    # A server cannot trick cleanup into deleting outside the requested directory.
    with patch("boto3.client", return_value=Mock()):
        bk = backend("s3")
        try:
            bk._backend.list_files = lambda path: [
                "../outside",
                "/absolute",
                {},
                "good.png",
            ]
            assert bk.list_files("memes") == ["good.png"]
        finally:
            bk.close()


def test_upload_staging_never_exposes_durable_path(tmp_path):
    # Observe the actual stream filename passed to the FTP implementation.
    module = importlib.import_module("ohmymeme_plugin_sync_ftp")
    source = tmp_path / "cache" / "private.png"
    source.parent.mkdir()
    source.write_bytes(b"payload")
    paths = []
    wire = Mock()

    def store(command, stream):
        paths.append(Path(stream.name))
        assert stream.read() == b"payload"

    wire.storbinary.side_effect = store
    with patch.object(module, "FTP", return_value=wire):
        bk = backend("ftp")
        bk.connect()
        assert bk.upload_file(source, "/memes/private.png") is True
        bk.close()
    assert paths and not paths[0].is_relative_to(tmp_path)
    assert not paths[0].parent.exists()


def test_sync_probe_error_closes_backend():
    # A legacy error-string result still has to revoke the operation's secrets.
    from ohmymeme.services.sync import service

    probe = Mock()
    probe.test_connection.side_effect = SyncError("probe failed")
    with patch.object(service, "_get_backend", return_value=probe):
        assert service.sync_test() == "probe failed"
    probe.close.assert_called_once()


def test_invalid_backend_result_is_not_success(tmp_path):
    # Truthy malformed plugin output cannot commit a successful upload.
    bk = backend("ftp")
    source = tmp_path / "source.bin"
    source.write_bytes(b"fixture")
    bk._backend.upload_file = lambda path, remote: "success"
    try:
        with pytest.raises(SyncError, match="upload_file.*bool"):
            bk.upload_file(source, "memes/a.bin")
        bk._backend.list_files = lambda path: {"file.png": True}
        with pytest.raises(SyncError, match="list_files.*list"):
            bk.list_files("memes")
    finally:
        bk.close()


@pytest.mark.parametrize(
    "kind,wire_method,backend_method,error_type",
    [
        ("ftp", "retrlines", "list_files", OSError),
        ("ftp", "cwd", "ensure_remote_dir", OSError),
        ("s3", "list_objects_v2", "list_files", OSError),
        ("r2", "list_objects_v2", "list_files", OSError),
        ("webdav", "urlopen", "list_files", SyncError),
        ("webdav", "urlopen", "ensure_remote_dir", SyncError),
    ],
)
def test_wire_exception_redaction(
    kind, wire_method, backend_method, error_type, caplog
):
    # The real factory runs each independent review reproduction through the adapter.
    key = {
        "ftp": "password",
        "s3": "secret_key",
        "r2": "secret_access_key",
        "webdav": "password",
    }[kind]
    wire = Mock()
    getattr(wire, wire_method).side_effect = OSError("fixture echoed " + WIRE_SECRET)
    target = "ohmymeme_plugin_sync_ftp.FTP" if kind == "ftp" else "boto3.client"
    with patch(target, return_value=wire):
        bk = backend(kind, **{kind + "_" + key: WIRE_SECRET, kind + "_user": "u"})
        try:
            bk.connect()
            with patch("urllib.request.urlopen", side_effect=wire.urlopen.side_effect):
                with pytest.raises(error_type) as raised:
                    getattr(bk, backend_method)("memes")
            message = str(raised.value)
            formatted = "".join(traceback.format_exception(raised.value))
            logging.getLogger(__name__).error("wire failure: %s", raised.value)
        finally:
            bk.close()
    print(
        json.dumps(
            {
                "kind": kind,
                "method": backend_method,
                "error": message,
                "exception_type": type(raised.value).__name__,
                "scope_closed": bk._closed,
            }
        ),
    )
    assert type(raised.value) is error_type
    assert WIRE_SECRET not in message + formatted + caplog.text
    assert "[REDACTED]" in message


@pytest.mark.parametrize("backend_method", ["list_files", "ensure_remote_dir"])
def test_wire_exception_redaction_hides_http_cause(backend_method, caplog):
    # HTTP code-only outer messages must not expose the credential in their cause.
    bk = backend("webdav", webdav_password=WIRE_SECRET)
    try:
        bk.connect()
        failure = HTTPError("https://fixture.invalid", 403, WIRE_SECRET, {}, None)
        with patch("urllib.request.urlopen", side_effect=failure):
            with pytest.raises(SyncError) as raised:
                try:
                    getattr(bk, backend_method)("memes")
                except SyncError:
                    logging.getLogger(__name__).exception("request failed")
                    raise
        assert "403" in str(raised.value)
        assert WIRE_SECRET not in "".join(traceback.format_exception(raised.value))
        assert WIRE_SECRET not in caplog.text
    finally:
        bk.close()


class _FailingSession:
    # 宿主清理回退用例：连接成功，列表调用携带密钥失败
    def __init__(self, error):
        self._error = error

    def call(self, method, args=(), timeout=None):
        if method == "connect":
            return None
        raise self._error

    def close(self):
        pass


class _StaticRuntime:
    def __init__(self, session):
        self._session = session

    def open_backend(self, provider_id, config, secrets, enabled=None):
        return self._session


def test_cleanup_exception_redaction_preserves_empty_fallback(tmp_path, caplog):
    # Observe the production planning logger without changing its legacy fallback.
    from ohmymeme.app.container import Container

    container = Container(tmp_path / "app")
    container.sync._plugin_runtime = _StaticRuntime(
        _FailingSession(error_perm("530 fixture echoed " + WIRE_SECRET))
    )
    try:
        for key, value in dict(
            config("ftp"), ftp_user="fixture-user", ftp_password=WIRE_SECRET
        ).items():
            container.config.set(key, value)
        result = container.sync.cleanup_remote_orphans()
        print(json.dumps({"result": result, "log": caplog.text}))
        assert result == {"ok": True, "orphans": [], "removed": 0}
        assert "list_files /memes failed:" in caplog.text
        assert WIRE_SECRET not in json.dumps(result) + caplog.text
    finally:
        container.close()
        container.db.close()


@pytest.mark.parametrize(
    "method,error_type",
    [
        ("connect", SyncError),
        ("ensure_remote_dir", RuntimeError),
        ("upload_file", RuntimeError),
        ("download_file", RuntimeError),
        ("file_exists", RuntimeError),
        ("delete_file", RuntimeError),
        ("list_files", RuntimeError),
        ("test_connection", RuntimeError),
        ("close", RuntimeError),
    ],
)
def test_fixed_method_exception_redaction(method, error_type, tmp_path, caplog):
    # All fixed port methods protect escaping errors, including close after revocation.
    bk = backend("ftp", ftp_password=WIRE_SECRET)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"fixture")
    calls = {
        "connect": (),
        "ensure_remote_dir": ("memes",),
        "upload_file": (source, "memes/a.bin"),
        "download_file": ("memes/a.bin", tmp_path / "output.bin"),
        "file_exists": ("memes/a.bin",),
        "delete_file": ("memes/a.bin",),
        "list_files": ("memes",),
        "test_connection": (),
        "close": (),
    }

    def fail(*args):
        try:
            raise ValueError("wire cause " + WIRE_SECRET)
        except ValueError as cause:
            error = RuntimeError("operation echoed " + WIRE_SECRET)
            error.add_note("wire note " + WIRE_SECRET)
            raise error from cause

    try:
        with patch.object(bk._backend, method, side_effect=fail):
            with pytest.raises(error_type) as raised:
                try:
                    getattr(bk, method)(*calls[method])
                except Exception:
                    logging.getLogger(__name__).exception("fixed operation failed")
                    raise
        assert type(raised.value) is error_type
        assert "operation echoed [REDACTED]" in str(raised.value)
        assert WIRE_SECRET not in "".join(traceback.format_exception(raised.value))
        assert WIRE_SECRET not in caplog.text
    finally:
        bk.close()
    assert not bk._secret_port._secrets and bk._temporary is None


@pytest.mark.parametrize("error_kind", ["oserror", "client", "url", "unsupported"])
def test_structured_exception_redaction_preserves_classification(error_kind, caplog):
    # Retain errno, SDK codes and NotImplementedError while scrubbing diagnostics.
    from botocore.exceptions import ClientError

    failure = {
        "oserror": OSError(13, "denied " + WIRE_SECRET, "/remote/" + WIRE_SECRET),
        "client": ClientError(
            {"Error": {"Code": "AccessDenied", "Message": WIRE_SECRET}}, "ListObjectsV2"
        ),
        "url": URLError(OSError("nested reason " + WIRE_SECRET)),
        "unsupported": NotImplementedError("unsupported " + WIRE_SECRET),
    }[error_kind]
    bk = backend("ftp", ftp_password=WIRE_SECRET)
    try:
        with patch.object(bk._backend, "list_files", side_effect=failure):
            with pytest.raises(type(failure)) as raised:
                try:
                    bk.list_files("memes")
                except Exception:
                    logging.getLogger(__name__).exception("structured error")
                    raise
        assert type(raised.value) is type(failure)
        assert WIRE_SECRET not in str(raised.value) + caplog.text
        assert WIRE_SECRET not in "".join(traceback.format_exception(raised.value))
        if error_kind == "oserror":
            assert raised.value.errno == 13
        elif error_kind == "client":
            assert raised.value.response["Error"]["Code"] == "AccessDenied"
            assert raised.value.operation_name == "ListObjectsV2"
    finally:
        bk.close()


@pytest.mark.parametrize("method", ["connect", "list_files"])
def test_secondary_close_exception_preserves_primary_redaction(method, caplog):
    # Cleanup failure must not replace the primary error or revive its secret chain.
    bk = backend("ftp", ftp_password=WIRE_SECRET)
    error_type = SyncError if method == "connect" else RuntimeError
    try:
        with patch.object(
            bk._backend, method, side_effect=RuntimeError("primary " + WIRE_SECRET)
        ):
            with patch.object(
                bk._backend,
                "close",
                side_effect=OSError("secondary " + WIRE_SECRET),
            ):
                with pytest.raises(error_type) as raised:
                    try:
                        if method == "connect":
                            bk.connect()
                        else:
                            try:
                                bk.list_files("memes")
                            finally:
                                bk.close()
                    except Exception:
                        logging.getLogger(__name__).exception("primary failure")
                        raise
        assert str(raised.value) == "primary [REDACTED]"
        assert WIRE_SECRET not in caplog.text
        assert WIRE_SECRET not in "".join(traceback.format_exception(raised.value))
        assert bk._closed and not bk._secret_port._secrets
    finally:
        bk.close()


def test_factory_exception_redaction(caplog):
    # A factory failure must be scrubbed before it is projected as a SyncError.
    module = importlib.import_module("ohmymeme_plugin_sync_ftp")

    def fail(provider, snapshot, secrets):
        raise RuntimeError("factory echoed " + secrets.get("password"))

    with patch.object(module.Plugin, "create_backend", fail):
        with pytest.raises(SyncError) as raised:
            try:
                backend("ftp", ftp_password=WIRE_SECRET)
            except SyncError:
                logging.getLogger(__name__).exception("factory failed")
                raise
    assert str(raised.value) == "factory echoed [REDACTED]"
    assert WIRE_SECRET not in caplog.text
    assert WIRE_SECRET not in "".join(traceback.format_exception(raised.value))
