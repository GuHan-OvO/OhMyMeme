import hashlib
import importlib
import inspect
import json
import socket
import struct
import subprocess
from contextlib import closing
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import ohmymeme_plugin_wechat as implementation
import pytest

from ohmymeme.app.container import Container
from ohmymeme.core.adapters.fetch_policy import FetchPolicy
from ohmymeme.core.domain import TaskKind
from ohmymeme.core.plugins.contracts import ImportPluginContext
from ohmymeme.core.plugins.policy import PluginPolicy
from ohmymeme.integrations.imports import wechat
from ohmymeme.presentation.desktop.api.plugin_dispatch import _descriptor
from ohmymeme.presentation.desktop.window_manager import SettingsApi
from plugin_wechat_qa import (
    FixtureHTTP,
    FixtureProcess,
    durable_snapshot,
    image_bytes,
    probe,
    source_database,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    # No test may reach DNS, public sockets, ffmpeg or a physical WeChat process.
    monkeypatch.setattr(
        socket, "getaddrinfo", Mock(side_effect=AssertionError("offline DNS"))
    )
    monkeypatch.setattr(
        socket.socket, "connect", Mock(side_effect=AssertionError("offline socket"))
    )
    monkeypatch.setattr(
        subprocess, "Popen", Mock(side_effect=AssertionError("offline helper"))
    )
    monkeypatch.setattr(implementation.platform, "system", lambda: "Windows")
    monkeypatch.setenv("OHMYMEME_INSECURE_SKIP_HELPER_HASH", "0")


def test_wechat_helper_integrity_requires_matching_sha256(tmp_path, monkeypatch):
    helper = tmp_path / "wechat_keyfinder.exe"
    helper.write_bytes(b"trusted helper")
    expected = hashlib.sha256(b"trusted helper").hexdigest()
    monkeypatch.setattr(wechat.platform, "system", lambda: "Windows")
    monkeypatch.setitem(wechat._WECHAT_KEYFINDER_SHA256, "Windows", expected)

    assert wechat.verify_binary_integrity(str(helper)) is True
    monkeypatch.setitem(wechat._WECHAT_KEYFINDER_SHA256, "Windows", "0" * 64)
    assert wechat.verify_binary_integrity(str(helper)) is False


def test_wechat_wal_merge_rejects_invalid_header_without_mutation(tmp_path):
    output = bytearray(b"keep" * 1024)
    before = bytes(output)
    wal = tmp_path / "emoticon.db-wal"
    wal.write_bytes(b"invalid wal")

    assert implementation._apply_wal(output, b"key", str(wal)) == 0
    assert bytes(output) == before


def test_wechat_factory_is_real_and_instance_scoped():
    # The old integration alone cannot satisfy physical provider extraction.
    plugin = importlib.import_module("ohmymeme_plugin_wechat")
    first, second = plugin.create_plugin(), plugin.create_plugin()
    assert type(first).__module__ == "ohmymeme_plugin_wechat"
    first.stop()
    first._update_wechat(message="first only")
    assert first._check_cancel()
    assert not second._check_cancel()
    assert second.get_progress()["message"] == ""


def test_published_abi_and_exact_entrypoint():
    # Package metadata and old Python call signatures remain independently usable.
    from importlib.metadata import distribution

    entries = distribution("ohmymeme-plugin-wechat").entry_points
    assert [(entry.group, entry.name, entry.value) for entry in entries] == [
        ("ohmymeme.plugins.v1", "source.wechat", "ohmymeme_plugin_wechat:create_plugin")
    ]
    assert entries["source.wechat"].load() is implementation.create_plugin
    assert (
        str(inspect.signature(wechat.start_wechat_import))
        == "(import_callback, user_root=None, download=True, account_path=None)"
    )
    assert (
        str(inspect.signature(wechat.inspect_wechat_environment)) == "(user_root=None)"
    )
    assert (
        str(inspect.signature(wechat.list_wechat_stickers))
        == "(user_root, account_path=None)"
    )
    assert str(inspect.signature(wechat.get_wechat_progress)) == "()"
    assert str(inspect.signature(wechat.cancel_wechat_import)) == "()"
    assert "ResourceLocator" not in inspect.getsource(implementation)
    assert "ohmymeme.presentation" not in inspect.getsource(implementation)
    assert "ohmymeme.app" not in inspect.getsource(implementation)


@pytest.mark.parametrize("hash_value", ["", "PLACEHOLDER_UPDATE_ON_RELEASE", "0" * 64])
def test_missing_or_mismatched_hash_never_launches_helper(
    tmp_path, monkeypatch, hash_value
):
    # Default rejection is executed, rather than a fixture's no_binary expectation.
    path = tmp_path / "wechat_keyfinder.exe"
    path.write_bytes(b"fixture")
    monkeypatch.setitem(wechat._WECHAT_KEYFINDER_SHA256, "Windows", hash_value)
    assert not wechat.verify_binary_integrity(path)
    if not hash_value or hash_value.startswith("PLACEHOLDER"):
        fetch = Mock(side_effect=AssertionError("must not download without hash"))
        monkeypatch.setattr(wechat._FETCH_POLICY, "download_to", fetch)
        assert wechat.ensure_wechat_keyfinder() == ""
        fetch.assert_not_called()


def test_real_offline_bridge_probes(tmp_path):
    # Runtime failures and success check the actual sink, rows, bytes and emissions.
    results = probe(
        tmp_path,
        [
            "happy",
            "missing_hash",
            "helper_failure",
            "unsafe_cdn",
            "unsafe_redirect",
            "missing_account",
            "cancel",
            "provider_absent",
        ],
    )
    assert results["happy"]["sink_count"] == 1
    assert all(value["cleanup"] for value in results.values())


def test_direct_library_surface_and_instance_isolation(tmp_path, monkeypatch):
    # Start two independent factories; only the explicitly cancelled one is stopped.
    source_database(tmp_path / "source", "happy")
    http = FixtureHTTP("happy")
    monkeypatch.setattr(
        implementation, "_FETCH_POLICY", FetchPolicy(resolver=http, connector=http)
    )
    container = Container(tmp_path / "host")
    policies = [PluginPolicy(container.config) for _ in range(2)]
    providers = [implementation.create_plugin() for _ in range(2)]
    operations, events = [], [[], []]
    try:
        before = durable_snapshot(container)
        for index in range(2):
            operation = policies[index].operation(_descriptor("source.wechat"))
            operations.append(operation)
            context = ImportPluginContext(
                _descriptor("source.wechat"),
                container.create_import_sink(),
                events[index].append,
                lambda: False,
                operation,
                {"user_root": str(tmp_path / "source"), "download": True},
            )
            assert providers[index].start(context)
            if index == 0:
                providers[index].stop()
            providers[index].import_media(context)
            if index == 0:
                assert durable_snapshot(container) == before
        assert providers[0].get_progress()["status"] == "cancelled"
        assert providers[1].get_progress()["imported"] == 1
        assert events[0][-1]["status"] == "cancelled"
        assert events[1][-1]["status"] == "done"
    finally:
        for policy, operation in zip(policies, operations):
            policy.close_operation(operation)
        assert not list(
            (container.config.data_dir / "plugin-workspaces").rglob("operation-*")
        )
        container.close()


def _encrypted_database(plain, key):
    # Make a valid small SQLite fixture with the same 80-byte reserve as SQLCipher.
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    reserved = bytearray(plain)
    reserved[20] = 80
    encrypted = bytearray()
    for offset in range(0, len(reserved), 4096):
        header = offset + (100 if offset == 0 else 0)
        assert reserved[header] == 13  # leaf table b-tree, sufficient for this fixture
        count = struct.unpack_from(">H", reserved, header + 3)[0]
        start = struct.unpack_from(">H", reserved, header + 5)[0]
        if start:
            reserved[offset + start - 80 : offset + 4016] = reserved[
                offset + start : offset + 4096
            ]
            struct.pack_into(">H", reserved, header + 5, start - 80)
        for cell in range(count):
            pos = header + 8 + cell * 2
            pointer = struct.unpack_from(">H", reserved, pos)[0]
            struct.pack_into(">H", reserved, pos, pointer - 80)
        reserved[offset + 4016 : offset + 4096] = bytes(80)
        page = reserved[offset : offset + 4096]
        begin = 16 if offset == 0 else 0
        iv = bytes(range(16))
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        encrypted.extend(
            (b"fixture-db-salt!" + b"!")[:begin]
            + cipher.update(page[begin:4016])
            + cipher.finalize()
            + iv
            + bytes(64)
        )
    return bytes(encrypted), bytes(reserved)


def test_real_decryption_query_and_committed_wal_fallback(tmp_path):
    # The fallback must pass bytes, not the helper's hex string, into AES/WAL.
    import sqlite3

    source = source_database(tmp_path / "source", "happy")
    key = bytes(range(32))
    encrypted, reserved = _encrypted_database(source.read_bytes(), key)
    assert len(implementation._query_sticker_metadata(reserved, tmp_path)) == 1
    cipher_path = tmp_path / "encrypted.db"
    cipher_path.write_bytes(encrypted)
    assert (
        bytes(implementation._decrypt_database(str(cipher_path), key.hex())) == reserved
    )
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("DELETE FROM kNonStoreEmoticonTable")
        conn.commit()
    empty, _ = _encrypted_database(source.read_bytes(), key)
    cipher_path.write_bytes(empty)
    header = bytearray(32)
    header[:4] = implementation._WAL_MAGIC_BE
    struct.pack_into(">I", header, 8, 4096)
    header[16:24] = b"salt1234"
    wal = bytes(header)
    for index in range(len(encrypted) // 4096):
        frame = (
            struct.pack(">II", index + 1, 2 if index == 1 else 0)
            + b"salt1234"
            + bytes(8)
        )
        wal += frame + encrypted[index * 4096 : (index + 1) * 4096]
    # A trailing uncommitted page must not replace the committed source metadata.
    wal += struct.pack(">II", 2, 0) + b"salt1234" + bytes(8) + empty[4096:8192]
    Path(str(cipher_path) + "-wal").write_bytes(wal)
    actual = implementation._load_sticker_metadata(
        str(cipher_path), key.hex(), temporary=tmp_path
    )
    assert actual[0]["md5"] == hashlib.md5(image_bytes()).hexdigest()
    assert len(actual) == 1


@pytest.mark.parametrize("key_source", ["masked", "rva"])
def test_helper_protocol_to_real_decryption_and_sink(tmp_path, monkeypatch, key_source):
    # The provider executes the unchanged helper protocol, AES, SQLite and sink path.
    source = source_database(tmp_path / "source", "happy")
    key = bytes(range(32))
    encrypted, _ = _encrypted_database(source.read_bytes(), key)
    source.write_bytes(encrypted)
    helper_root = tmp_path / "host-helper"
    helper_root.mkdir()
    helper = helper_root / "wechat_keyfinder.exe"
    helper.write_bytes(b"fixture helper")
    offsets = helper_root / "offsets.json"
    offsets.write_bytes(b'{"versions":{}}')
    monkeypatch.setattr(wechat, "_get_wechat_dir", lambda: helper_root)
    monkeypatch.setattr(wechat, "_offsets_path", lambda: offsets)
    monkeypatch.setitem(
        wechat._WECHAT_KEYFINDER_SHA256,
        "Windows",
        hashlib.sha256(helper.read_bytes()).hexdigest(),
    )
    proc = FixtureProcess({"ok": True, "key": key.hex(), "method": key_source})
    calls = []

    def popen(cmd, **kwargs):
        assert cmd[1] == "--config"
        assert cmd[3:] == ["--db-path", str(source), "--no-snapshot"]
        assert Path(cmd[0]).read_bytes() == helper.read_bytes()
        assert Path(cmd[2]).read_bytes() == offsets.read_bytes()
        assert "operation-" in cmd[0] and cmd[0] != str(helper)
        calls.append(cmd)
        return proc

    monkeypatch.setattr(implementation.subprocess, "Popen", popen)
    http = FixtureHTTP("happy")
    monkeypatch.setattr(
        implementation, "_FETCH_POLICY", FetchPolicy(resolver=http, connector=http)
    )
    container = Container(tmp_path / "host")
    try:
        api = SettingsApi(container.create_webui(), container.settings)
        assert api.start_wechat_import(str(tmp_path / "source")) == {"ok": True}
        container.operations.wait(TaskKind.IMPORT_WECHAT, 5)
        assert api.get_wechat_import_progress()["imported"] == 1
        assert len(container.db.search()) == 1 and len(calls) == 1
        assert proc.waited and proc.stdout.closed and proc.stderr.closed
        assert source.read_bytes() == encrypted
        assert not list(
            (container.config.data_dir / "plugin-workspaces").rglob("operation-*")
        )
    finally:
        container.close()


@pytest.mark.parametrize("mode", ["timeout", "cancel", "shutdown"])
def test_helper_hung_process_is_registered_drained_and_temp_removed(
    tmp_path, monkeypatch, mode
):
    # Events and a fixture clock make hang/timeout deterministic without sleeping.
    container = Container(tmp_path / "host")
    source = source_database(tmp_path / "source", "helper_failure")
    entered, released = Event(), Event()
    proc = FixtureProcess({})
    original_terminate = proc.terminate

    def communicate(timeout=None):
        entered.set()
        if mode != "timeout":
            assert released.wait(5)
        raise subprocess.TimeoutExpired("fixture-helper", timeout)

    def terminate():
        original_terminate()
        proc.returncode = None
        released.set()

    def wait(timeout=None):
        proc.waited = True
        if proc.returncode is None:
            raise subprocess.TimeoutExpired("fixture-helper", timeout)
        return proc.returncode

    proc.communicate, proc.terminate, proc.wait = communicate, terminate, wait
    monkeypatch.setattr(
        implementation.subprocess, "Popen", lambda *args, **kwargs: proc
    )
    if mode == "timeout":
        clock = iter([0, 0, 91])
        monkeypatch.setattr(
            implementation, "time", SimpleNamespace(monotonic=lambda: next(clock))
        )

    def resources(operation, cancelled):
        helper = operation.temporary.path("helper.exe")
        offsets = operation.temporary.path("offsets.json")
        helper.write_bytes(b"fixture helper")
        offsets.write_bytes(b"{}")
        return str(helper), str(offsets)

    monkeypatch.setattr(wechat, "prepare_wechat_helper", resources)
    try:
        webui = container.create_webui()
        api = SettingsApi(webui, container.settings)
        before = durable_snapshot(container)
        assert api.start_wechat_import(str(source.parents[3])) == {"ok": True}
        assert entered.wait(5)
        worker = webui._import_workers["source.wechat"]
        if mode != "timeout":
            assert container.operations.query(
                TaskKind.IMPORT_WECHAT
            ).inventory.processes
            if mode == "cancel":
                assert api.cancel_wechat_import() is None
                assert api.cancel_wechat_import() is None
                released.set()
            else:
                report = container.operations.shutdown(timeout=5, child_grace=0)
                assert not report.timed_out
        container.operations.wait(TaskKind.IMPORT_WECHAT, 5)
        state = api.get_wechat_import_progress()
        assert (
            state["error_code"] == "binary_timeout"
            if mode == "timeout"
            else state["status"] == "cancelled"
        )
        assert proc.waited and proc.killed and proc.stdout.closed and proc.stderr.closed
        assert worker.operation._closed and not worker._active
        assert durable_snapshot(container) == before
        assert not list(
            (container.config.data_dir / "plugin-workspaces").rglob("operation-*")
        )
    finally:
        released.set()
        container.close()


def test_helper_key_and_error_are_redacted_at_emission(tmp_path, monkeypatch):
    # Inspect actual policy events, not just a final progress dictionary.
    source_database(tmp_path / "source", "helper_failure")
    container = Container(tmp_path / "host")
    events = []
    secret = "ab" * 32
    proc = FixtureProcess(
        {
            "ok": False,
            "key": secret,
            "reason": "key_not_found",
            "detail": "failure " + secret,
        },
        1,
    )
    monkeypatch.setattr(
        implementation.subprocess, "Popen", lambda *args, **kwargs: proc
    )
    monkeypatch.setattr(
        wechat,
        "prepare_wechat_helper",
        lambda operation, cancelled: ("fixture-helper", "fixture-offsets"),
    )
    flush = PluginPolicy.flush_outputs

    def capture(policy, operation, emitter):
        def emit(kind, value):
            events.append((kind, value))
            emitter(kind, value)

        flush(policy, operation, emit)

    monkeypatch.setattr(PluginPolicy, "flush_outputs", capture)
    try:
        api = SettingsApi(container.create_webui(), container.settings)
        assert api.start_wechat_import(str(tmp_path / "source")) == {"ok": True}
        container.operations.wait(TaskKind.IMPORT_WECHAT, 5)
        assert api.get_wechat_import_progress()["error"] == "failure [REDACTED]"
        assert events and secret not in json.dumps(events)
        assert any(kind == "log" and "[REDACTED]" in value for kind, value in events)
    finally:
        container.close()


def test_synchronous_list_and_legacy_callback_cleanup(tmp_path, monkeypatch):
    # Account listing keeps its result without changing import progress.
    source_database(tmp_path / "source", "multiple_accounts")
    monkeypatch.setattr(wechat, "_session", None)
    root = str(tmp_path / "source")
    idle = wechat.get_wechat_progress()
    assert wechat.list_wechat_stickers(root)["status"] == "multiple_accounts"
    listed = wechat.list_wechat_stickers(root, "wxid_fixture")
    assert listed["account"] == "wxid_fixture" and listed["total"] == 1
    assert wechat.get_wechat_progress() == idle
    callback = Mock(return_value={"ids": [1]})
    assert wechat.start_wechat_import(callback, root, False, "wxid_fixture")
    wechat._session.coordinator.wait(TaskKind.IMPORT_WECHAT, 5)
    assert wechat.get_wechat_progress()["status"] == "done"
    callback.assert_not_called()
    assert wechat._session.worker.operation._closed
    assert wechat.cancel_wechat_import() is None
