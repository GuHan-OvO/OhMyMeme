import base64
import hashlib
import io
import json
import socket
import sys
import tempfile
import threading
import traceback
from argparse import ArgumentParser
from contextlib import ExitStack
from importlib.metadata import EntryPoint
from pathlib import Path
from unittest.mock import patch

from ohmymeme.app.container import Container
from ohmymeme.core.domain import TaskKind
from ohmymeme.core.plugins.manifest import canonical_descriptor
from ohmymeme.core.plugins.network_config import LanTransportConfig
from ohmymeme.core.plugins.registry import PluginRegistry
from ohmymeme.services.lan import protocol
from ohmymeme.services.lan import server as host

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.plugin_sync import FAILURES, offline_network

CASES = [
    "wire-and-library",
    "approval-denied",
    "approval-wait-close",
    "blocked-read-close",
    "hung-session-deadline",
]


def expected_matrix():
    # Values are tied to the actual host protocol and the byte-only config record.
    return {
        "schema_version": 1,
        "provider": "transport.lan",
        "record": "LanTransportConfig",
        "transport_fields": list(LanTransportConfig._fields),
        "legacy_keys": {"port": "lan_port", "secret": "lan_secret"},
        "wire_version": host.PROTOCOL_VERSION,
        "frame_limit": protocol.MAX_FRAME,
        "security": "host:HMAC-SHA256/PBKDF2-SHA256/AES-GCM",
        "approval": "host:callback-or-headless",
        "replay": "host:session-direction-frame-command",
        "commands": "host:fixed-allowlist",
        "lifecycle": "host:OperationCoordinator",
        "share_secrets_default": False,
        "activation": "temporary",
    }


def receive(sock, size):
    # The fixture client consumes exact bytes, with a bounded socket deadline.
    output = bytearray()
    while len(output) < size:
        chunk = sock.recv(size - len(output))
        if not chunk:
            raise OSError("closed")
        output.extend(chunk)
    return bytes(output)


def connect(server, secret):
    # Run the released v1 challenge/proof handshake over an actual TCP socket.
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=2)
    try:
        greeting = protocol.decode_plain(lambda n: receive(sock, n))
        if secret:
            assert greeting["t"] == "challenge"
            sock.sendall(
                protocol.encode_plain(
                    {"t": "proof", "mac": protocol.proof(secret, greeting["nonce"])}
                )
            )
            greeting = protocol.decode_plain(lambda n: receive(sock, n))
        assert greeting == {"t": "ok"}
        return sock
    except BaseException:
        sock.close()
        raise


def command(sock, key, message=None, frame=None):
    # Return the decoded real host response, not the fixture's expected JSON.
    sock.sendall(frame if frame is not None else protocol.encode_frame(key, message))
    return protocol.decode_frame(lambda n: receive(sock, n), key)[0]


def provider_rejections():
    # All production provider selection failures retain the false Bridge sentinel.
    descriptor = canonical_descriptor("transport.lan")
    observations = []
    for case in FAILURES:
        active = (
            descriptor._replace(api_version=99)
            if case == "incompatible"
            else descriptor
        )
        entries = ()
        if case == "stale-entry-point":
            entries = (
                EntryPoint(
                    name=descriptor.id,
                    value="wrong:create_plugin",
                    group=descriptor.group,
                ),
            )
        server = host.LanServer(
            registry=PluginRegistry((active,), {}, entries),
            enabled=() if case == "disabled" else None,
        )
        with patch(
            "socket.socket",
            side_effect=AssertionError("provider fallback bound socket"),
        ) as network:
            result = server.start(0, "")
            assert result is False
            assert network.call_count == 0
        observations.append(
            {
                "case": case,
                "result": result,
                "error": server.get_status()["last_error"],
                "network_calls": network.call_count,
            }
        )
    return observations


def wire_and_library(container):
    # Registry factory bytes feed host approval, replay policy and durable commands.
    from PIL import Image

    server = container.lan
    host.set_allow_secret_config(False)
    server.set_confirm_callback(
        lambda device: server.confirm_device(True, device["_confirm_id"])
    )
    assert server.start(0, "fixture-secret")
    assert type(server._transport).__module__ == "ohmymeme_plugin_lan"
    status = server.get_status()
    assert status["allow_secret_config"] is False
    inventory = container.operations.query(TaskKind.LAN_SERVICE).inventory
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.settimeout(2)
        udp.sendto(b'{"t":"discover"}', ("127.0.0.1", server.port))
        discovery = json.loads(udp.recvfrom(2048)[0])
        assert set(discovery) == {"t", "name", "os", "ver", "need_secret"}
        assert discovery["need_secret"] is True
    with connect(server, "fixture-secret") as sock:
        key = protocol.derive_key("fixture-secret")
        approval = command(sock, key, {"cmd": "device_info", "name": "fixture"})
        assert approval == {"ok": True, "approved": True, "allow_secret_config": False}
        ping = command(sock, key, {"cmd": "ping"})
        unknown = command(sock, key, {"cmd": "execute"})
        assert ping["ok"] is True and unknown["ok"] is False
        stream = io.BytesIO()
        Image.new("RGB", (2, 2), (30, 60, 90)).save(stream, "PNG")
        payload = stream.getvalue()
        upload = command(
            sock,
            key,
            {
                "cmd": "push_file",
                "filename": "fixture.png",
                "data": base64.b64encode(payload).decode(),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        )
        assert upload["ok"] is True
        download = command(
            sock, key, {"cmd": "pull_file", "filename": upload["filename"]}
        )
        assert base64.b64decode(download["data"]) == payload
        bad_file = command(
            sock,
            key,
            {"cmd": "push_file", "filename": "../outside.png", "data": "eA=="},
        )
        assert bad_file["ok"] is False
        frame = protocol.encode_frame(
            key, {"cmd": "send_config", "config": {"copy_resize_max": 123}}
        )
        applied = command(sock, key, frame=frame)
        replay = command(sock, key, frame=frame)
        assert applied == {"ok": True} and replay == {"ok": False, "error": "重复请求"}
        assert container.config.get("copy_resize_max") == 123
        exported = command(sock, key, {"cmd": "get_config"})
        assert "lan_secret" not in exported["config"]
        manifest = command(sock, key, {"cmd": "pull_manifest"})
        assert manifest["ok"] is True and len(manifest["manifest"]["memes"]) == 1
    server.stop()
    report = container.operations.shutdown(timeout=1)
    assert not report.shutdown_blocked and not report.inventory.sockets
    assert not server._threads and not server._session_sockets
    return {
        "implementation": type(server._transport).__module__,
        "discovery": discovery,
        "approval": approval,
        "ping": ping,
        "unknown": unknown,
        "upload": upload,
        "bad_file": bad_file,
        "applied": applied,
        "replay": replay,
        "rows": container.db.count(),
        "registered_socket_operations": len(inventory.sockets),
        "cleanup": {
            "blocked": report.shutdown_blocked,
            "sockets": len(report.inventory.sockets),
            "threads": len(server._threads),
            "sessions": len(server._session_sockets),
        },
    }


def lifecycle_case(container, case):
    # Event barriers expose real pending approval/read/hung-session states.
    import ohmymeme_plugin_lan as implementation

    server = container.lan
    entered, release = threading.Event(), threading.Event()
    host.set_allow_secret_config(False)
    if case == "approval-denied":
        server.set_confirm_callback(
            lambda d: server.confirm_device(False, d["_confirm_id"])
        )
    elif case == "approval-wait-close":
        server.set_confirm_callback(lambda d: entered.set())
    else:
        server.set_confirm_callback(None)
    sock = None
    try:
        with ExitStack() as stack:
            if case == "hung-session-deadline":

                def hang(*args):
                    entered.set()
                    release.wait()

                stack.enter_context(patch.object(server, "_session_loop", hang))
            if case == "blocked-read-close":
                original = implementation.ByteConnection.receive_bytes

                def read(connection, size):
                    entered.set()
                    return original(connection, size)

                stack.enter_context(
                    patch.object(implementation.ByteConnection, "receive_bytes", read)
                )
            assert server.start(0, "")
            sock = connect(server, "")
            if case == "approval-denied":
                denied = command(sock, bytes(32), {"cmd": "device_info"})
                result = command(sock, bytes(32), {"cmd": "ping"})
                assert denied["approved"] is False and result["ok"] is False
            elif case == "approval-wait-close":
                sock.sendall(protocol.encode_frame(bytes(32), {"cmd": "device_info"}))
                assert entered.wait(2)
            else:
                assert entered.wait(2)
            before = container.operations.query(TaskKind.LAN_SERVICE).inventory
            report = container.operations.shutdown(
                timeout=0.05 if case == "hung-session-deadline" else 2
            )
            if case == "hung-session-deadline":
                assert report.shutdown_blocked and report.timed_out
                release.set()
                container.operations.wait(TaskKind.LAN_SERVICE, 2)
                drained = container.operations.shutdown(timeout=2)
            else:
                drained = report
            assert not drained.shutdown_blocked and not drained.inventory.sockets
            assert not server._pending_confirms and not server._threads
            return {
                "socket_owner_tasks": len(before.sockets),
                "deadline_blocked": report.shutdown_blocked,
                "drained": not drained.shutdown_blocked,
                "remaining_sockets": len(drained.inventory.sockets),
                "remaining_threads": len(server._threads),
            }
    finally:
        release.set()
        if sock is not None:
            sock.close()
        server.stop()


def happy(root):
    # Each case has independent Container state, ephemeral port and explicit drain.
    observations = {}
    for case in CASES:
        container = Container(root / case)
        try:
            observations[case] = (
                wire_and_library(container)
                if case == "wire-and-library"
                else lifecycle_case(container, case)
            )
        finally:
            container.close()
            container.db.close()
    return {"runtime": observations, "provider_rejections": provider_rejections()}


def invalid(fixture):
    # Reject actual host configuration/security/approval before provider/network calls.
    observations = []
    for case in fixture["invalid"]:
        registry = PluginRegistry((canonical_descriptor("transport.lan"),))
        server = host.LanServer(registry=registry)
        server._confirm_cb = case.get("approval")
        with patch.object(registry, "get", wraps=registry.get) as load:
            with patch.object(host, "HAS_AESGCM", case.get("security", True)):
                with patch(
                    "socket.socket",
                    side_effect=AssertionError("network before validation"),
                ) as network:
                    result = server.start(case.get("port", 0), case.get("secret", ""))
        message = server.get_status()["last_error"]
        assert result is False and case["field"] in message
        assert load.call_count == network.call_count == 0
        observations.append(
            {
                "result": result,
                "error": message,
                "provider_calls": load.call_count,
                "network_calls": network.call_count,
            }
        )
    return observations


def main():
    # Report only observations from this process, with nonzero invalid-input exit.
    parser = ArgumentParser()
    parser.add_argument("--check", action="store_true", required=True)
    for name in ("matrix", "fixture", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = {"status": "failed", "observations": {}, "errors": []}
    exit_code = 1
    try:
        matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
        expected = expected_matrix()
        for field in expected:
            if (
                type(matrix.get(field)) is not type(expected[field])
                or matrix.get(field) != expected[field]
            ):
                raise ValueError(f"matrix.{field}: host mapping mismatch")
        if matrix.keys() != expected.keys():
            raise ValueError("matrix: unknown fields")
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        if (
            type(fixture.get("schema_version")) is not int
            or fixture["schema_version"] != 1
        ):
            raise ValueError("schema_version: expected integer 1")
        failing = "invalid" in fixture
        if failing:
            if fixture.get("network") != "deny-all" or not fixture["invalid"]:
                raise ValueError("invalid: expected nonempty deny-all cases")
        elif fixture != {
            "schema_version": 1,
            "network": "declared-loopback-only",
            "cases": CASES,
            "provider_failures": FAILURES,
        }:
            raise ValueError("fixture: undeclared runtime cases")
        with offline_network(not failing) as violations:
            with tempfile.TemporaryDirectory(prefix="ohmm-plugin-lan-qa-") as temporary:
                report["observations"] = (
                    invalid(fixture) if failing else happy(Path(temporary))
                )
        report["external_violations"] = violations
        assert not violations, violations
        report["status"] = "rejected" if failing else "passed"
        exit_code = 1 if failing else 0
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
        report["traceback"] = traceback.format_exc()
    report["exit_code"] = exit_code
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
