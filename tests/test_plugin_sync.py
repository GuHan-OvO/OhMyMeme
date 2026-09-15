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


@pytest.mark.parametrize("kind", ["ftp", "s3", "r2", "webdav"])
def test_real_factory_and_independent_backends(kind):
    # Factories live in physical distributions, never import host implementations.
    module = importlib.import_module(f"ohmymeme_plugin_sync_{kind}")
    assert "/plugins/" in Path(module.__file__).as_posix()
    assert module.create_plugin() is not module.create_plugin()
    first, second = get_backend(config(kind)), get_backend(config(kind))
    try:
        assert first._backend is not second._backend
        assert first._backend.__class__.__module__.startswith("ohmymeme_plugin_sync_")
        assert not hasattr(first._backend, "cfg")
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
    registry = Mock()
    with patch("socket.socket", side_effect=AssertionError("network")) as network:
        with pytest.raises(SyncError, match=field):
            get_backend(dict(config(kind), **{field: value}), registry=registry)
    registry.get.assert_not_called()
    network.assert_not_called()


@pytest.mark.parametrize("kind", ["ftp", "s3", "r2", "webdav"])
def test_missing_and_disabled_provider_never_fallback(kind):
    from ohmymeme.core.plugins.registry import PluginRegistry

    with patch("socket.socket", side_effect=AssertionError("network")):
        with pytest.raises(SyncError, match="provider"):
            get_backend(config(kind), registry=PluginRegistry((), {}, ()))
        with pytest.raises(SyncError, match="provider_disabled"):
            get_backend(config(kind), enabled=())


def test_ftp_error_results_and_close_revoke_secrets(tmp_path):
    # Exercise the real factory, host file projection, and actual FTP operation code.
    module = importlib.import_module("ohmymeme_plugin_sync_ftp")
    wire = Mock()
    wire.storbinary.side_effect = OSError("wire failed")
    with patch.object(module, "FTP", return_value=wire):
        backend = get_backend(config("ftp"))
        secret_getter = backend._backend.secrets
        backend.connect()
        source = tmp_path / "asset.bin"
        source.write_bytes(b"fixture")
        assert backend.upload_file(source, "/memes/a.bin") is False
        wire.retrlines.side_effect = OSError("list failed")
        with pytest.raises(OSError, match="list failed"):
            backend.list_files("/memes")
        backend.close()
        backend.close()
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
    backend = get_backend(cfg)
    try:
        assert _remote_root(cfg) == backend._backend.config.path == "/namespaced"
    finally:
        backend.close()


def test_s3_credentials_do_not_use_ambient_sdk_chain():
    # Even an empty credential snapshot must not query global environment/metadata.
    wire = Mock()
    with patch("boto3.client", return_value=wire) as client:
        backend = get_backend(config("s3"))
        try:
            backend.connect()
            assert client.call_args.kwargs["aws_access_key_id"] == ""
            assert client.call_args.kwargs["aws_secret_access_key"] == ""
        finally:
            backend.close()


def test_r2_reuses_only_s3_code_not_disabled_provider():
    # A disabled S3 provider does not disable the separately selected R2 provider.
    from ohmymeme.core.plugins.manifest import canonical_descriptor
    from ohmymeme.core.plugins.registry import PluginRegistry

    registry = PluginRegistry((canonical_descriptor("sync.r2"),))
    with patch("boto3.client", return_value=Mock()):
        backend = get_backend(config("r2"), registry=registry, enabled=("sync.r2",))
        try:
            backend.connect()
            assert type(backend._backend).__module__ == "ohmymeme_plugin_sync_r2"
        finally:
            backend.close()


def test_ftps_default_protects_data_and_reconnects_independently():
    # Reconnect replaces and closes the old network object without changing peers.
    module = importlib.import_module("ohmymeme_plugin_sync_ftp")
    first, second = Mock(), Mock()
    with patch.object(module, "FTP_TLS", side_effect=[first, second]):
        backend = get_backend(config("ftps"))
        try:
            backend.connect()
            backend.connect()
            first.prot_p.assert_called_once()
            second.prot_p.assert_called_once()
            first.quit.assert_called_once()
        finally:
            backend.close()
        second.quit.assert_called_once()


def test_sync_listing_rejects_untrusted_children():
    # A server cannot trick cleanup into deleting outside the requested directory.
    with patch("boto3.client", return_value=Mock()):
        backend = get_backend(config("s3"))
        try:
            backend._backend.list_files = lambda path: [
                "../outside",
                "/absolute",
                {},
                "good.png",
            ]
            assert backend.list_files("memes") == ["good.png"]
        finally:
            backend.close()


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
        backend = get_backend(config("ftp"))
        backend.connect()
        assert backend.upload_file(source, "/memes/private.png") is True
        backend.close()
    assert paths and not paths[0].is_relative_to(tmp_path)
    assert not paths[0].parent.exists()


def test_sync_probe_error_closes_backend():
    # A legacy error-string result still has to revoke the operation's secrets.
    from ohmymeme.services.sync import service

    backend = Mock()
    backend.test_connection.side_effect = SyncError("probe failed")
    with patch.object(service, "_get_backend", return_value=backend):
        assert service.sync_test() == "probe failed"
    backend.close.assert_called_once()


def test_invalid_backend_result_is_not_success(tmp_path):
    # Truthy malformed plugin output cannot commit a successful upload.
    backend = get_backend(config("ftp"))
    source = tmp_path / "source.bin"
    source.write_bytes(b"fixture")
    backend._backend.upload_file = lambda path, remote: "success"
    try:
        with pytest.raises(SyncError, match="upload_file.*bool"):
            backend.upload_file(source, "memes/a.bin")
        backend._backend.list_files = lambda path: {"file.png": True}
        with pytest.raises(SyncError, match="list_files.*list"):
            backend.list_files("memes")
    finally:
        backend.close()


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
    # The real registry/factory runs each independent review reproduction.
    cfg = config(kind)
    key = {
        "ftp": "password",
        "s3": "secret_key",
        "r2": "secret_access_key",
        "webdav": "password",
    }[kind]
    cfg[kind + "_" + key] = WIRE_SECRET
    cfg[kind + "_user"] = "fixture-user"
    wire = Mock()
    getattr(wire, wire_method).side_effect = OSError("fixture echoed " + WIRE_SECRET)
    target = "ohmymeme_plugin_sync_ftp.FTP" if kind == "ftp" else "boto3.client"
    with patch(target, return_value=wire):
        backend = get_backend(cfg)
        try:
            backend.connect()
            with patch("urllib.request.urlopen", side_effect=wire.urlopen.side_effect):
                with pytest.raises(error_type) as raised:
                    getattr(backend, backend_method)("memes")
            message = str(raised.value)
            formatted = "".join(traceback.format_exception(raised.value))
            logging.getLogger(__name__).error("wire failure: %s", raised.value)
        finally:
            backend.close()
    print(
        json.dumps(
            {
                "kind": kind,
                "method": backend_method,
                "error": message,
                "exception_type": type(raised.value).__name__,
                "scope_closed": backend._closed,
            }
        ),
    )
    assert type(raised.value) is error_type
    assert WIRE_SECRET not in message + formatted + caplog.text
    assert "[REDACTED]" in message


@pytest.mark.parametrize("backend_method", ["list_files", "ensure_remote_dir"])
def test_wire_exception_redaction_hides_http_cause(backend_method, caplog):
    # HTTP code-only outer messages must not expose the credential in their cause.
    cfg = dict(config("webdav"), webdav_password=WIRE_SECRET)
    backend = get_backend(cfg)
    try:
        backend.connect()
        failure = HTTPError("https://fixture.invalid", 403, WIRE_SECRET, {}, None)
        with patch("urllib.request.urlopen", side_effect=failure):
            with pytest.raises(SyncError) as raised:
                try:
                    getattr(backend, backend_method)("memes")
                except SyncError:
                    logging.getLogger(__name__).exception("request failed")
                    raise
        assert "403" in str(raised.value)
        assert WIRE_SECRET not in "".join(traceback.format_exception(raised.value))
        assert WIRE_SECRET not in caplog.text
    finally:
        backend.close()


def test_cleanup_exception_redaction_preserves_empty_fallback(tmp_path, caplog):
    # Observe the production planning logger without changing its legacy fallback.
    from ohmymeme.app.container import Container

    container = Container(tmp_path / "app")
    wire = Mock()
    wire.size.side_effect = error_perm("550 missing")
    wire.retrlines.side_effect = error_perm("530 fixture echoed " + WIRE_SECRET)
    try:
        for key, value in dict(
            config("ftp"), ftp_user="fixture-user", ftp_password=WIRE_SECRET
        ).items():
            container.config.set(key, value)
        with patch("ohmymeme_plugin_sync_ftp.FTP", return_value=wire):
            result = container.sync.cleanup_remote_orphans()
        print(json.dumps({"result": result, "log": caplog.text}))
        assert result == {"ok": True, "orphans": [], "removed": 0}
        assert "list_files /memes failed:" in caplog.text
        assert WIRE_SECRET not in json.dumps(result) + caplog.text
        wire.quit.assert_called_once()
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
    backend = get_backend(dict(config("ftp"), ftp_password=WIRE_SECRET))
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
        with patch.object(backend._backend, method, side_effect=fail):
            with pytest.raises(error_type) as raised:
                try:
                    getattr(backend, method)(*calls[method])
                except Exception:
                    logging.getLogger(__name__).exception("fixed operation failed")
                    raise
        assert type(raised.value) is error_type
        assert "operation echoed [REDACTED]" in str(raised.value)
        assert WIRE_SECRET not in "".join(traceback.format_exception(raised.value))
        assert WIRE_SECRET not in caplog.text
    finally:
        backend.close()
    assert not backend._secret_port._secrets and backend._temporary is None


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
    backend = get_backend(dict(config("ftp"), ftp_password=WIRE_SECRET))
    try:
        with patch.object(backend._backend, "list_files", side_effect=failure):
            with pytest.raises(type(failure)) as raised:
                try:
                    backend.list_files("memes")
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
        backend.close()


@pytest.mark.parametrize("method", ["connect", "list_files"])
def test_secondary_close_exception_preserves_primary_redaction(method, caplog):
    # Cleanup failure must not replace the primary error or revive its secret chain.
    backend = get_backend(dict(config("ftp"), ftp_password=WIRE_SECRET))
    error_type = SyncError if method == "connect" else RuntimeError
    try:
        with patch.object(
            backend._backend, method, side_effect=RuntimeError("primary " + WIRE_SECRET)
        ):
            with patch.object(
                backend._backend,
                "close",
                side_effect=OSError("secondary " + WIRE_SECRET),
            ):
                with pytest.raises(error_type) as raised:
                    try:
                        if method == "connect":
                            backend.connect()
                        else:
                            try:
                                backend.list_files("memes")
                            finally:
                                backend.close()
                    except Exception:
                        logging.getLogger(__name__).exception("primary failure")
                        raise
        assert str(raised.value) == "primary [REDACTED]"
        assert WIRE_SECRET not in caplog.text
        assert WIRE_SECRET not in "".join(traceback.format_exception(raised.value))
        assert backend._closed and not backend._secret_port._secrets
    finally:
        backend.close()


def test_factory_exception_redaction_revokes_scope(caplog):
    # A factory failure must be scrubbed before its just-created scope is cleared.
    module = importlib.import_module("ohmymeme_plugin_sync_ftp")
    scopes = []

    def fail(provider, snapshot, secrets):
        scopes.append(secrets)
        raise RuntimeError("factory echoed " + secrets.get("password"))

    with patch.object(module.Plugin, "create_backend", fail):
        with pytest.raises(SyncError) as raised:
            try:
                get_backend(dict(config("ftp"), ftp_password=WIRE_SECRET))
            except SyncError:
                logging.getLogger(__name__).exception("factory failed")
                raise
    assert str(raised.value) == "factory echoed [REDACTED]"
    assert WIRE_SECRET not in caplog.text
    assert WIRE_SECRET not in "".join(traceback.format_exception(raised.value))
    assert not scopes[0]._secret_port._secrets
    with pytest.raises(RuntimeError, match="closed"):
        scopes[0].get("password")
