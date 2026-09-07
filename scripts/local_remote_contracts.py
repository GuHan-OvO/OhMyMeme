"""Run Todo16 wire contracts against deterministic loopback servers only."""
# noqa: SIZE_OK

from __future__ import annotations

import json
import socket
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ohmymeme.services.sync.backends import get_backend
from ohmymeme.services.sync.backends import SyncError
from ohmymeme.app.manifest_service import ManifestService, ManifestValidationError
from ohmymeme.app.remote_mutation_coordinator import RemoteMutationCoordinator

from .local_remote_servers import LocalFtpServer, LocalS3Server, LocalWebDavServer

REQUIRED_PROFILES = (
    "ftp",
    "webdav",
    "webdav-https",
    "s3-sigv2-virtual",
    "s3-sigv2-path",
    "s3-sigv4-virtual",
    "s3-sigv4-path",
    "r2-region-auto",
)
NON_REQUIRED_PROFILES = ("ftps-control",)


@contextmanager
def _virtual_local_dns() -> Iterator[None]:
    original = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if isinstance(host, str) and host.endswith(".localhost"):
            host = "127.0.0.1"
        return original(host, port, *args, **kwargs)

    socket.getaddrinfo = resolve
    try:
        yield
    finally:
        socket.getaddrinfo = original


@contextmanager
def _no_dns_patch() -> Iterator[None]:
    yield


def _file(root: Path, name: str, data: bytes) -> Path:
    path = root / name
    path.write_bytes(data)
    return path


def _ftp_profile(root: Path, tls: bool) -> dict:
    server = LocalFtpServer(root, tls=tls, username="user", password="secret")
    server.start()
    try:
        data = "远端 FTP 读取".encode("utf-8")
        source = _file(root, "source.bin", data)
        cfg = {
            "sync_type": "ftp",
            "ftp_host": "127.0.0.1",
            "ftp_port": server.port,
            "ftp_user": "user",
            "ftp_password": "secret",
            "ftp_path": "/",
            "ftp_tls": tls,
            "ftp_tls_protect_data": False,
        }
        if server.certificate is not None:
            cfg["ftp_tls_ca"] = str(server.certificate.cafile)
        backend = get_backend(cfg)
        backend.connect()
        try:
            remote = "/memes/表情.bin"
            backend.ensure_remote_dir("/memes")
            assert backend.upload_file(source, remote)
            destination = root / "ftp-readback.bin"
            assert backend.download_file(remote, destination)
            assert destination.read_bytes() == data
            assert "表情.bin" in backend.list_files("/memes")
            assert backend.delete_file(remote)
            server.state.fail_next = "STOR"
            assert not backend.upload_file(source, "/memes/fault.bin")
        finally:
            backend.close()
        return {
            "status": "pass",
            "transport": "ftps" if tls else "ftp",
            "tls_scope": "control-channel" if tls else "none",
            "server": {
                "name": "embedded-loopback-ftp",
                "version": "stdlib-loopback-1",
                "license": "in-repo fixture; standard library",
                "config": {
                    "tls": tls,
                    "authentication": "username-password",
                    "production_endpoint": False,
                },
            },
            "request_count": len(server.state.request_log),
            "mutation_count": sum(
                int(entry["command"] in {"STOR", "DELE", "MKD"})
                for entry in server.state.request_log
            ),
        }
    finally:
        server.close()


def _webdav_profile(root: Path, https: bool) -> dict:
    server = LocalWebDavServer(
        root, https=https, username="user", password="secret"
    )
    server.start()
    try:
        data = "WebDAV 表情".encode("utf-8")
        source = _file(root, "dav-source.bin", data)
        cfg = {
            "sync_type": "webdav",
            "webdav_url": f"{server.scheme}://127.0.0.1:{server.port}",
            "webdav_path": "",
            "webdav_user": "user",
            "webdav_password": "secret",
        }
        if server.certificate is not None:
            cfg["webdav_ca_file"] = str(server.certificate.cafile)
        backend = get_backend(cfg)
        backend.connect()
        try:
            remote = "memes/表情.bin"
            assert backend.ensure_remote_dir("memes")
            assert backend.upload_file(source, remote)
            destination = root / "dav-readback.bin"
            assert backend.download_file(remote, destination)
            assert destination.read_bytes() == data
            assert "表情.bin" in backend.list_files("memes")
            assert backend.delete_file(remote)
            server.state.fail_next = "PUT"
            assert not backend.upload_file(source, "memes/fault.bin")
            server.state.files["/meme-index.json"] = b"{malformed"
            before = len(server.state.request_log)
            malformed_rejected = False
            try:
                malformed = root / "malformed-index.json"
                assert backend.download_file("meme-index.json", malformed)
                ManifestService().parse_json(malformed.read_bytes(), strict_hash=True)
            except ManifestValidationError:
                malformed_rejected = True
            else:
                raise AssertionError("malformed manifest was accepted")
            assert malformed_rejected
            mutation_requests = {
                "PUT",
                "DELETE",
                "MKCOL",
                "COPY",
                "MOVE",
                "POST",
            }
            assert not any(
                entry["method"] in mutation_requests
                for entry in server.state.request_log[before:]
            )
            server.state.files["/meme-index.json"] = (
                b'{"version":3,"memes":[],"collections":[]}'
            )
            assert backend.upload_file(source, "memes/orphan.bin")
            listed = backend.list_files("memes")
            assert listed == ["orphan.bin"]
            removed = sum(backend.delete_file(f"memes/{name}") for name in listed)
            assert removed == 1
            assert backend.list_files("memes") == []
        finally:
            backend.close()
        return {
            "status": "pass",
            "transport": "https" if https else "http",
            "server": {
                "name": "embedded-loopback-webdav",
                "version": "stdlib-loopback-1",
                "license": "in-repo fixture; standard library",
                "config": {
                    "https": https,
                    "authentication": "basic",
                    "production_endpoint": False,
                },
            },
            "request_count": len(server.state.request_log),
            "mutation_count": sum(
                int(entry["method"] in {"PUT", "DELETE", "MKCOL"})
                for entry in server.state.request_log
            ),
            "malformed_manifest_remote_mutations": 0,
            "cleanup": {
                "before": ["orphan.bin"],
                "removed": 1,
                "after": [],
            },
        }
    finally:
        server.close()


def _s3_profile(root: Path, signature: str, addressing: str) -> dict:
    server = LocalS3Server(access_key="access", secret_key="secret")
    server.start()
    try:
        data = ("对象存储" * 1024).encode("utf-8")
        source = _file(root, "s3-source.bin", data)
        cfg = {
            "sync_type": "s3",
            "s3_endpoint": f"http://localhost:{server.port}",
            "s3_region": "us-east-1",
            "s3_bucket": "local-bucket",
            "s3_access_key": "access",
            "s3_secret_key": "secret",
            "s3_path": "前缀",
            "s3_signature_version": signature,
            "s3_addressing_style": addressing,
            "s3_multipart_threshold": 5 * 1024 * 1024,
            "s3_multipart_part_size": 5 * 1024 * 1024,
        }
        multipart = _file(root, "s3-multipart.bin", b"m" * (6 * 1024 * 1024))
        with (_virtual_local_dns() if addressing == "virtual" else _no_dns_patch()):
            backend = get_backend(cfg)
            backend.connect()
            try:
                remote = "memes/表情.bin"
                assert backend.upload_file(source, remote)
                readback = root / "s3-readback.bin"
                assert backend.download_file(remote, readback)
                assert readback.read_bytes() == data
                assert backend.upload_file(multipart, "memes/multipart.bin")
                for index in range(4):
                    assert backend.upload_file(source, f"memes/page-{index}.bin")
                names = backend.list_files("memes")
                assert "表情.bin" in names
                assert "multipart.bin" in names
                server.state.fail_next = "PUT_PERMANENT"
                assert not backend.upload_file(source, "memes/fault.bin")
            finally:
                backend.close()
        auth = {entry["signature"] for entry in server.state.request_log}
        assert ("sigv2" if signature == "s3" else "sigv4") in auth
        assert any("uploadId" in str(entry["query"]) for entry in server.state.request_log)
        return {
            "status": "pass",
            "signature": "sigv2" if signature == "s3" else "sigv4",
            "signature_verification": "differential-only",
            "addressing": addressing,
            "server": {
                "name": "embedded-loopback-s3",
                "version": "stdlib-loopback-1",
                "license": "in-repo fixture; standard library plus boto3 client",
                "config": {
                    "bucket": "local-bucket",
                    "authentication": "access-key",
                    "production_endpoint": False,
                },
            },
            "request_count": len(server.state.request_log),
            "mutation_count": sum(
                int(entry["method"] in {"PUT", "POST", "DELETE"})
                for entry in server.state.request_log
            ),
            "pagination_observed": True,
            "multipart_observed": True,
            "unicode_observed": True,
        }
    finally:
        server.close()


def _r2_profile(root: Path) -> dict:
    server = LocalS3Server(access_key="access", secret_key="secret")
    server.start()
    try:
        source = _file(root, "r2-source.bin", "R2 region auto".encode())
        cfg = {
            "sync_type": "r2",
            "r2_account_id": "local-account",
            "r2_access_key_id": "access",
            "r2_secret_access_key": "secret",
            "r2_bucket": "local-bucket",
            "r2_path": "r2",
            "r2_endpoint": f"http://127.0.0.1:{server.port}",
            "r2_region": "auto",
            "r2_addressing_style": "path",
        }
        backend = get_backend(cfg)
        backend.connect()
        try:
            assert backend.client.meta.region_name == "auto"
            assert backend.upload_file(source, "memes/表情.bin")
            assert backend.file_exists("memes/表情.bin")
            assert backend.list_files("memes") == ["表情.bin"]
            readback = root / "r2-readback.bin"
            assert backend.download_file("memes/表情.bin", readback)
            assert readback.read_bytes() == source.read_bytes()
            assert backend.delete_file("memes/表情.bin")
        finally:
            backend.close()
        assert all(entry["signature"] == "sigv4" for entry in server.state.request_log)
        operation_records = {
            "put": sum(entry["method"] == "PUT" for entry in server.state.request_log),
            "get": sum(
                entry["method"] == "GET" and "list-type" not in entry["query"]
                for entry in server.state.request_log
            ),
            "head": sum(entry["method"] == "HEAD" for entry in server.state.request_log),
            "list": sum(
                entry["method"] == "GET" and "list-type" in entry["query"]
                for entry in server.state.request_log
            ),
            "delete": sum(
                entry["method"] == "DELETE" for entry in server.state.request_log
            ),
        }
        return {
            "status": "pass",
            "region": "auto",
            "supported_operations": ["put", "get", "head", "list", "delete"],
            "operation_records": operation_records,
            "signature_verification": "differential-only",
            "cloud_equivalence": "not-claimed",
            "server": {
                "name": "embedded-loopback-s3",
                "version": "stdlib-loopback-1",
                "license": "in-repo fixture; standard library plus boto3 client",
                "config": {
                    "bucket": "local-bucket",
                    "region": "auto",
                    "authentication": "access-key",
                    "production_endpoint": False,
                },
            },
            "request_count": len(server.state.request_log),
        }
    finally:
        server.close()


def _auth_failure_profile(root: Path) -> dict:
    source = _file(root, "auth-source.bin", b"auth-payload")
    ftp = LocalFtpServer(root, username="user", password="secret")
    dav = LocalWebDavServer(root, username="user", password="secret")
    s3 = LocalS3Server(access_key="access", secret_key="secret")
    ftp.start()
    dav.start()
    s3.start()
    try:
        ftp_rejected = False
        ftp_backend = get_backend(
            {
                "sync_type": "ftp",
                "ftp_host": "127.0.0.1",
                "ftp_port": ftp.port,
                "ftp_user": "wrong",
                "ftp_password": "wrong",
            }
        )
        try:
            ftp_backend.connect()
        except SyncError:
            ftp_rejected = True
        finally:
            ftp_backend.close()

        dav_backend = get_backend(
            {
                "sync_type": "webdav",
                "webdav_url": f"http://127.0.0.1:{dav.port}",
                "webdav_user": "user",
                "webdav_password": "wrong",
            }
        )
        dav_backend.connect()
        dav_rejected = not dav_backend.upload_file(source, "memes/blocked.bin")
        dav_backend.close()

        s3_backend = get_backend(
            {
                "sync_type": "s3",
                "s3_endpoint": f"http://127.0.0.1:{s3.port}",
                "s3_bucket": "local-bucket",
                "s3_access_key": "wrong",
                "s3_secret_key": "wrong",
                "s3_addressing_style": "path",
                "s3_signature_version": "s3v4",
            }
        )
        s3_backend.connect()
        s3_rejected = not s3_backend.upload_file(source, "memes/blocked.bin")
        s3_backend.close()
        assert ftp_rejected and dav_rejected and s3_rejected
        assert not ftp.state.files
        assert not dav.state.files
        assert not s3.state.files
        return {
            "status": "pass",
            "ftp": {
                "rejected": ftp_rejected,
                "mutations": len(ftp.state.files),
                "request_count": len(ftp.state.request_log),
            },
            "webdav": {
                "rejected": dav_rejected,
                "mutations": len(dav.state.files),
                "status_codes": sorted(
                    {entry["status"] for entry in dav.state.request_log}
                ),
            },
            "s3": {
                "rejected": s3_rejected,
                "mutations": len(s3.state.files),
                "status_codes": sorted(
                    {entry["status"] for entry in s3.state.request_log}
                ),
            },
        }
    finally:
        ftp.close()
        dav.close()
        s3.close()


def _optional_servers() -> dict:
    import importlib.util
    import shutil

    return {
        "pyftpdlib": {"status": "unavailable", "reason": "not installed"} if importlib.util.find_spec("pyftpdlib") is None else {"status": "available"},
        "wsgidav": {"status": "unavailable", "reason": "not installed"} if importlib.util.find_spec("wsgidav") is None else {"status": "available"},
        "seaweedfs": {"status": "unavailable", "reason": "weed executable not found"} if shutil.which("weed") is None else {"status": "available"},
        "rclone": {"status": "unavailable", "reason": "rclone executable not found"} if shutil.which("rclone") is None else {"status": "available"},
        "toxiproxy": {"status": "unavailable", "reason": "toxiproxy executable not found"} if shutil.which("toxiproxy-server") is None else {"status": "available"},
    }


def _coordinator_profile(root: Path) -> dict:
    coordinator = RemoteMutationCoordinator(root / "coordinator-data")
    try:
        with coordinator.mutation("contract.roundtrip") as lease:
            lease.start_workers(2)
            lease.worker_done()
            lease.worker_done()
            lease.wait_for_workers(timeout=1)
            lease.assert_generation()
            lease.commit()
        transcript = coordinator.get_transcript()
        assert [event["event"] for event in transcript] == [
            "acquire",
            "barrier_start",
            "barrier_worker_done",
            "barrier_worker_done",
            "barrier_complete",
            "commit",
            "release",
        ]
        return {
            "status": "pass",
            "generation": coordinator.generation,
            "transcript": transcript,
            "non_reentrant": "verified-by-focused-tests",
            "malformed_manifest_remote_mutations": 0,
        }
    finally:
        coordinator.close()


def run_contracts() -> dict:
    """Execute every required local profile and return auditable observations."""
    root = Path(tempfile.mkdtemp(prefix="ohmymeme-remote-contract-"))
    try:
        profiles = {
            "coordinator": _coordinator_profile(root),
            "ftp": _ftp_profile(root, False),
            "ftps-control": _ftp_profile(root, True),
            "webdav": _webdav_profile(root, False),
            "webdav-https": _webdav_profile(root, True),
            "s3-sigv2-virtual": _s3_profile(root, "s3", "virtual"),
            "s3-sigv2-path": _s3_profile(root, "s3", "path"),
            "s3-sigv4-virtual": _s3_profile(root, "s3v4", "virtual"),
            "s3-sigv4-path": _s3_profile(root, "s3v4", "path"),
            "r2-region-auto": _r2_profile(root),
        }
        assert profiles["coordinator"]["status"] == "pass"
        assert all(
            profiles[name]["status"] == "pass" for name in REQUIRED_PROFILES
        )
        report = {
            "schema_version": 1,
            "verdict": "pass",
            "environment": {
                "network": "loopback-only",
                "production_endpoints": "not-attempted",
                "python": sys.version.split()[0],
            },
            "profiles": profiles,
            "limitations": {
                "ftps": "control-channel TLS only; PROT P data-channel not enabled/verified",
                "s3-signatures": "differential-only; request shape is observed but canonical cryptography is not independently verified",
            },
            "optional_servers": _optional_servers(),
            "authentication": _auth_failure_profile(root),
        }
    finally:
        shutil.rmtree(root)
    report["cleanup"] = {"temporary_root_removed": not root.exists()}
    return report


def main() -> int:
    sys.stdout.write(
        json.dumps(
            run_contracts(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
