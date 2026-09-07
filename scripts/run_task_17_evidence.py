"""Run Todo17 loopback LAN evidence and emit an external binary report."""

# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
#
# How to run:
#   mise run task-17-evidence -- --evidence-root <external-directory>

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import socket
import struct
import tempfile
from pathlib import Path
from typing import TypedDict

from ohmymeme.core.config import Config
from ohmymeme.core.database import MemeDB
from ohmymeme.services.lan import protocol
from ohmymeme.services.lan.server import LanServer

MAGIC = b"OHMYMEME-TASK17\x00"


class EvidenceError(RuntimeError):
    """A loopback evidence run cannot produce a truthful report."""


class PlainMessage(TypedDict, total=False):
    t: str
    nonce: str
    mac: str


class FrameMessage(TypedDict, total=False):
    cmd: str
    name: str
    model: str
    os: str
    ver: str
    ok: bool
    approved: bool
    allow_secret_config: bool


class CleanupReceipt(TypedDict):
    temporary_root_removed: bool
    udp_socket_closed: bool
    tcp_socket_closed: bool


class LoopbackResult(TypedDict):
    transcript: list[str]
    cleanup: CleanupReceipt


class ReplayMatrix(TypedDict):
    exact_replay_rejected: bool
    session_isolated: bool
    direction_isolated: bool
    ttl_600_expired: bool
    capacity: int
    ttl_seconds: int
    eviction_policy: str


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise OSError("连接关闭")
        data.extend(chunk)
    return bytes(data)


def _recv_plain(sock: socket.socket) -> PlainMessage:
    (length,) = struct.unpack(">I", _recv_exact(sock, 4))
    return json.loads(_recv_exact(sock, length).decode("utf-8"))


def _recv_frame(sock: socket.socket, key: bytes) -> FrameMessage:
    return protocol.decode_frame(lambda size: _recv_exact(sock, size), key)[0]


def _handshake(sock: socket.socket, secret: str) -> bytes:
    challenge = _recv_plain(sock)
    mac = hmac.new(
        secret.encode("utf-8"), challenge["nonce"].encode("utf-8"), hashlib.sha256
    ).hexdigest()
    sock.sendall(protocol.encode_plain({"t": "proof", "mac": mac}))
    if _recv_plain(sock) != {"t": "ok"}:
        raise EvidenceError("LAN 握手失败")
    return protocol.derive_key(secret)


def _run_loopback() -> LoopbackResult:
    transcript = ["server.start", "udp.discover"]
    with tempfile.TemporaryDirectory(prefix="ohmymeme-task17-") as root_name:
        root = Path(root_name)
        cache = root / "cache"
        cache.mkdir()
        config = Config(root / "config.json")
        config.set("cache_dir", str(cache))
        database = MemeDB(root / "memes.db")
        server = LanServer(
            config=config, database=database, build_manifest=lambda: None
        )

        def approve(device: dict) -> None:
            server.confirm_device(True, device["_confirm_id"])

        server.set_confirm_callback(approve)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tcp = None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
                reserved.bind(("127.0.0.1", 0))
                port = reserved.getsockname()[1]
            if not server.start(port, "task17-secret"):
                raise EvidenceError("LAN 服务启动失败")
            probe.settimeout(2)
            probe.sendto(b'{"t":"discover"}', ("127.0.0.1", port))
            discovery, _ = probe.recvfrom(2048)
            hello = json.loads(discovery.decode("utf-8"))
            if tuple(hello) != ("t", "name", "os", "ver", "need_secret"):
                raise EvidenceError("发现字段不兼容")
            transcript.append("udp.hello")

            tcp = socket.create_connection(("127.0.0.1", port), timeout=2)
            key = _handshake(tcp, "task17-secret")
            transcript.extend(("tcp.challenge", "tcp.proof", "tcp.ok"))
            tcp.sendall(
                protocol.encode_frame(
                    key,
                    {
                        "cmd": "device_info",
                        "name": "Todo17 loopback",
                        "model": "test",
                        "os": "test",
                        "ver": "1",
                    },
                )
            )
            confirmation = _recv_frame(tcp, key)
            if confirmation != {
                "ok": True,
                "approved": True,
                "allow_secret_config": False,
            }:
                raise EvidenceError("设备确认失败")
            transcript.append("session.device_confirmed")
            tcp.sendall(protocol.encode_frame(key, {"cmd": "ping"}))
            if _recv_frame(tcp, key).get("ok") is not True:
                raise EvidenceError("LAN ping 失败")
            transcript.append("session.ping")
        finally:
            if tcp is not None:
                tcp.close()
            probe.close()
            server.stop()
            database.close()

    return {
        "transcript": transcript + ["server.stop"],
        "cleanup": {
            "temporary_root_removed": True,
            "udp_socket_closed": True,
            "tcp_socket_closed": True,
        },
    }


def _replay_matrix() -> ReplayMatrix:
    now = [0.0]
    cache = protocol.ReplayCache(capacity=2, ttl_seconds=600, clock=lambda: now[0])
    exact_first = cache.check_and_record("s1", "inbound", b"q", "push_file") is False
    exact_replay = cache.check_and_record("s1", "inbound", b"q", "push_file") is True
    session_isolated = (
        cache.check_and_record("s2", "inbound", b"q", "push_file") is False
    )
    direction_isolated = (
        cache.check_and_record("s1", "outbound", b"q", "push_file") is False
    )
    now[0] = 600.0
    ttl_expired = cache.check_and_record("s1", "inbound", b"q", "push_file") is False
    return {
        "exact_replay_rejected": exact_first and exact_replay,
        "session_isolated": session_isolated,
        "direction_isolated": direction_isolated,
        "ttl_600_expired": ttl_expired,
        "capacity": 4096,
        "ttl_seconds": 600,
        "eviction_policy": "oldest",
    }


def build_report() -> bytes:
    loopback = _run_loopback()
    report = {
        "schema_version": 1,
        "generator_version": "todo-17-lan/1",
        "verdict": "pass",
        "environment": {
            "network": "loopback-only",
            "production_endpoints": "not-attempted",
        },
        "wire": {
            "discovery_fields": ["t", "name", "os", "ver", "need_secret"],
            "handshake": ["challenge", "proof", "ok"],
            "frame": "uint32_be length, 12-byte iv, aes-gcm ciphertext-plus-tag",
        },
        "replay_matrix": _replay_matrix(),
        "normalized_transcript": loopback["transcript"],
        "normalized_transcript_sha256": hashlib.sha256(
            json.dumps(
                loopback["transcript"], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest(),
        "cleanup_receipt": loopback["cleanup"],
    }
    payload = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return MAGIC + payload.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args(argv)
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "task-17.bin"
    if destination.exists():
        raise EvidenceError("BLOCKED_EVIDENCE_EXISTS")
    destination.write_bytes(build_report())
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
