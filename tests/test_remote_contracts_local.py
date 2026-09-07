from pathlib import Path

import pytest


def test_local_remote_contract_runner_exposes_required_profiles():
    # Given: the local-only contract runner module
    from scripts.local_remote_contracts import REQUIRED_PROFILES

    # When: its profile registry is inspected
    # Then: every protocol and both S3 addressing/signature dimensions are explicit
    assert REQUIRED_PROFILES == (
        "ftp",
        "webdav",
        "webdav-https",
        "s3-sigv2-virtual",
        "s3-sigv2-path",
        "s3-sigv4-virtual",
        "s3-sigv4-path",
        "r2-region-auto",
    )


def test_local_remote_contract_runner_is_a_wire_harness_not_sdk_fixture():
    # Given: the source of the local contract runner
    source = Path("scripts/local_remote_contracts.py").read_text(encoding="utf-8")

    # When: its implementation is inspected for the transport boundary
    # Then: it must expose real listener/client operations and request accounting
    assert "LocalWebDavServer" in source
    assert "LocalFtpServer" in source
    assert "LocalS3Server" in source
    assert "request_log" in Path("scripts/local_remote_servers.py").read_text(encoding="utf-8")
    assert "get_backend" in source


def test_local_webdav_cleanup_removes_only_true_orphan_children(tmp_path):
    # Given: a WebDAV tree with one orphan child and the collection directory itself
    from scripts.local_remote_servers import LocalWebDavServer
    from ohmymeme.services.sync import planning
    from ohmymeme.services.sync.backends import get_backend

    server = LocalWebDavServer(tmp_path / "dav")
    server.start()

    class Config:
        data_dir = tmp_path

        def get(self, key, default=None):
            return {"webdav_path": ""}.get(key, default)

    try:
        source = tmp_path / "orphan.bin"
        source.write_bytes(b"orphan")
        config = {
            "sync_type": "webdav",
            "webdav_url": f"http://127.0.0.1:{server.port}",
            "webdav_path": "",
        }
        backend = get_backend(config)
        backend.connect()
        try:
            backend.ensure_remote_dir("memes")
            assert backend.upload_file(source, "memes/orphan.bin")
            index = tmp_path / "index.json"
            index.write_text(
                '{"version":3,"memes":[],"collections":[]}', encoding="utf-8"
            )
            assert backend.upload_file(index, "meme-index.json")
            # When: the real WebDAV listing is compared to the remote manifest
            orphans = planning.list_remote_orphans(backend, "", Config())
            assert orphans == ["orphan.bin"]
            removed = sum(
                backend.delete_file(f"memes/{name}") for name in orphans
            )
            # Then: exactly the true orphan is removed and the root is untouched
            assert removed == 1
            assert backend.list_files("memes") == []
            assert backend.file_exists("meme-index.json")
        finally:
            backend.close()
    finally:
        server.close()


def test_container_sync_cleanup_counts_only_confirmed_webdav_deletes(tmp_path):
    # Given: a Container-bound sync service and a local WebDAV orphan
    from ohmymeme.app.container import Container
    from scripts.local_remote_servers import LocalWebDavServer
    from ohmymeme.services.sync.backends import get_backend

    server = LocalWebDavServer(tmp_path / "dav")
    server.start()
    container = Container(tmp_path / "app")
    try:
        container.config.set("sync_type", "webdav")
        container.config.set("webdav_url", f"http://127.0.0.1:{server.port}")
        container.config.save()
        source = tmp_path / "orphan.bin"
        source.write_bytes(b"orphan")
        backend = get_backend(
            {
                "sync_type": "webdav",
                "webdav_url": f"http://127.0.0.1:{server.port}",
                "webdav_path": "",
            }
        )
        backend.connect()
        try:
            backend.ensure_remote_dir("memes")
            assert backend.upload_file(source, "memes/orphan.bin")
            index = tmp_path / "index.json"
            index.write_text(
                '{"version":3,"memes":[],"collections":[]}', encoding="utf-8"
            )
            assert backend.upload_file(index, "meme-index.json")
        finally:
            backend.close()

        # When: the Container sync cleanup performs the real WebDAV mutations
        result = container.sync.cleanup_remote_orphans(delete=True)

        # Then: only the true orphan is counted and the shared lease commits
        assert result == {"ok": True, "orphans": ["orphan.bin"], "removed": 1}
        assert [
            event["entrypoint"]
            for event in container.remote_mutations.get_transcript()
            if event["event"] == "commit"
        ] == ["sync.cleanup"]
    finally:
        container.close()
        server.close()


def test_local_protocol_fixtures_reject_wrong_credentials_without_mutation(tmp_path):
    # Given: local FTP, WebDAV and S3 fixtures with explicit expected credentials
    from scripts.local_remote_servers import LocalFtpServer, LocalS3Server, LocalWebDavServer
    from ohmymeme.services.sync.backends import SyncError, get_backend

    ftp = LocalFtpServer(tmp_path / "ftp", username="user", password="secret")
    dav = LocalWebDavServer(tmp_path / "dav", username="user", password="secret")
    s3 = LocalS3Server(access_key="access", secret_key="secret")
    ftp.start()
    dav.start()
    s3.start()
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    try:
        wrong_ftp = get_backend(
            {
                "sync_type": "ftp",
                "ftp_host": "127.0.0.1",
                "ftp_port": ftp.port,
                "ftp_user": "wrong",
                "ftp_password": "wrong",
            }
        )
        with pytest.raises(SyncError):
            wrong_ftp.connect()
        wrong_dav = get_backend(
            {
                "sync_type": "webdav",
                "webdav_url": f"http://127.0.0.1:{dav.port}",
                "webdav_user": "user",
                "webdav_password": "wrong",
            }
        )
        wrong_dav.connect()
        assert not wrong_dav.upload_file(source, "memes/blocked.bin")
        wrong_s3 = get_backend(
            {
                "sync_type": "s3",
                "s3_endpoint": f"http://127.0.0.1:{s3.port}",
                "s3_bucket": "local-bucket",
                "s3_access_key": "access",
                "s3_secret_key": "wrong",
                "s3_addressing_style": "path",
                "s3_signature_version": "s3",
                "s3_multipart_threshold": 5 * 1024 * 1024,
                "s3_multipart_part_size": 5 * 1024 * 1024,
            }
        )
        wrong_s3.connect()
        assert not wrong_s3.upload_file(source, "memes/blocked.bin")
        assert "/memes/blocked.bin" not in dav.state.files
        assert ("local-bucket", "memes/blocked.bin") not in s3.state.files
        assert any(entry.get("status") == 401 for entry in dav.state.request_log)
        assert any(entry.get("status") == 403 for entry in s3.state.request_log)
    finally:
        ftp.close()
        dav.close()
        s3.close()


def test_container_push_invalid_webdav_manifest_performs_zero_remote_writes(tmp_path):
    # Given: a non-empty WebDAV root containing an invalid remote manifest
    from ohmymeme.app.container import Container
    from scripts.local_remote_servers import LocalWebDavServer

    server = LocalWebDavServer(tmp_path / "dav")
    server.start()
    container = Container(tmp_path / "app")
    try:
        container.config.set("sync_type", "webdav")
        container.config.set("webdav_url", f"http://127.0.0.1:{server.port}")
        container.config.set("webdav_path", "remote")
        container.config.save()
        local = container.assets.cache_dir / "one.png"
        local.write_bytes(b"local")
        container.db.add_meme("one.png", file_hash="a" * 64, file_size=5)
        container.build_manifest()
        server.state.files["/remote/meme-index.json"] = b"{invalid"

        # When: push reads the invalid manifest under a real WebDAV wire client
        with pytest.raises(Exception):
            container.sync.push()

        # Then: no remote write verb occurs before manifest validation fails
        assert not any(
            entry["method"] in {"MKCOL", "PUT", "DELETE", "COPY", "MOVE"}
            for entry in server.state.request_log
        )
    finally:
        container.close()
        server.close()


def test_upload_index_invalid_webdav_manifest_performs_zero_remote_writes(tmp_path, monkeypatch):
    # Given: a Container whose manifest builder leaves invalid local bytes untouched
    from ohmymeme.app.container import Container
    from scripts.local_remote_servers import LocalWebDavServer

    server = LocalWebDavServer(tmp_path / "dav")
    server.start()
    container = Container(tmp_path / "app")
    try:
        container.config.set("sync_type", "webdav")
        container.config.set("webdav_url", f"http://127.0.0.1:{server.port}")
        container.config.set("webdav_path", "remote")
        container.config.save()
        (container.config.data_dir / "meme-index.json").write_bytes(b"{invalid")
        monkeypatch.setattr(container.sync, "_build_manifest", lambda: None)

        # When: upload_index validates the local manifest before remote setup
        with pytest.raises(Exception):
            container.sync.upload_index()

        # Then: invalid content causes no remote mutation verb
        assert not any(
            entry["method"] in {"MKCOL", "PUT", "DELETE", "COPY", "MOVE"}
            for entry in server.state.request_log
        )
    finally:
        container.close()
        server.close()


def test_local_s3_sigv2_wrong_secret_rejects_multipart_query_without_mutation(tmp_path):
    # Given: a SigV2 client with a wrong secret and a multipart-sized object
    from ohmymeme.services.sync.backends import get_backend
    from scripts.local_remote_servers import LocalS3Server

    server = LocalS3Server(access_key="access", secret_key="secret")
    server.start()
    source = tmp_path / "large.bin"
    source.write_bytes(b"x" * (6 * 1024 * 1024))
    try:
        backend = get_backend(
            {
                "sync_type": "s3",
                "s3_endpoint": f"http://127.0.0.1:{server.port}",
                "s3_bucket": "local-bucket",
                "s3_access_key": "access",
                "s3_secret_key": "wrong",
                "s3_addressing_style": "path",
                "s3_signature_version": "s3",
                "s3_multipart_threshold": 5 * 1024 * 1024,
                "s3_multipart_part_size": 5 * 1024 * 1024,
            }
        )
        backend.connect()
        try:
            # When: multipart query requests are authenticated with the wrong secret
            result = backend.upload_file(source, "memes/blocked-large.bin")
            assert not result, server.state.request_log
        finally:
            backend.close()

        # Then: no multipart object or upload session is persisted
        assert not any(key[1] == "memes/blocked-large.bin" for key in server.state.files)
        assert server.state.uploads == {}
        assert any(
            entry["status"] == 403 and entry["query"]
            for entry in server.state.request_log
        ), server.state.request_log
    finally:
        server.close()


def test_local_r2_profile_observes_declared_head_list_delete_operations(tmp_path):
    # Given: the local R2 differential profile
    from scripts.local_remote_contracts import _r2_profile

    # When: the profile executes its roundtrip
    result = _r2_profile(tmp_path)

    # Then: the report records real head/list/delete wire operations
    assert result["status"] == "pass"
    assert result["supported_operations"] == ["put", "get", "head", "list", "delete"]
    assert result["operation_records"] == {
        "put": 1,
        "get": 1,
        "head": 1,
        "list": 1,
        "delete": 1,
    }
