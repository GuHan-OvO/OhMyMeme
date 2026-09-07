import json
from pathlib import Path

from ohmymeme.app.pull_commit_service import PullCommitError
from ohmymeme.services.sync.service import _publish_heartbeat

from scripts.task_14_fault_matrix import _download, _fixture, _row, _state


def downloaded_invalid(root):
    with _fixture(root) as fixture:
        before = _state(fixture)
        try:
            fixture["service"].commit(fixture["projection"], _download(b"invalid"))
        except PullCommitError as error:
            assert str(error) == "invalid_staged_asset"
        else:
            raise AssertionError("invalid staged bytes were accepted")
        assert _state(fixture) == before
        return _row(fixture, before, {"uploads": [], "deletes": []}, "invalid-image", "download writes invalid bytes", ["no local mutation"])


def staged_valid(root):
    with _fixture(root) as fixture:
        before = _state(fixture)
        result = fixture["service"].commit(fixture["projection"], _download(fixture["data"]))
        assert result == {"ok": True, "cleanup_pending": False}
        assert not fixture["service"].journal_path.exists()
        return _row(fixture, before, {"uploads": [], "deletes": []}, "valid-image", "valid staging commit", ["no remote mutation"])


def file_replaced_db_failed(root):
    with _fixture(root, original=True) as fixture:
        before = _state(fixture)
        fixture["service"]._apply_metadata = lambda _value: (_ for _ in ()).throw(OSError("db failed"))
        try:
            fixture["service"].commit(fixture["projection"], _download(fixture["data"]))
        except PullCommitError as error:
            assert str(error) == "metadata_failed"
        else:
            raise AssertionError("metadata failure was accepted")
        assert (fixture["assets"].cache_dir / "asset.png").read_bytes() == b"old"
        assert _state(fixture)["journal"] == "needs_recovery"
        return _row(fixture, before, {"uploads": [], "deletes": []}, "existing-asset", "metadata raises OSError", ["backup restored", "needs recovery journal"])


def db_committed_manifest_failed(root):
    with _fixture(root) as fixture:
        before = _state(fixture)
        write = fixture["service"]._write_journal

        def fail_commit_marker(record):
            if record["phase"] == "db_committed":
                raise OSError("marker failed")
            write(record)

        fixture["service"]._write_journal = fail_commit_marker
        try:
            fixture["service"].commit(fixture["projection"], _download(fixture["data"]))
        except OSError as error:
            assert str(error) == "marker failed"
        else:
            raise AssertionError("committed marker failure was accepted")
        fixture["service"]._write_journal = write
        assert fixture["service"].recover() is True
        assert fixture["service"].recover() is False
        assert _state(fixture)["journal"] == "absent"
        return _row(fixture, before, {"uploads": [], "deletes": []}, "committed-metadata", "db committed marker raises OSError", ["restart rebuilds manifest", "recovery idempotent"])


def manifest_write_failed(root):
    with _fixture(root) as fixture:
        fixture["manifest"].write_bytes(b'{"collections":[],"memes":[],"version":3}')
        before = _state(fixture)
        fixture["service"]._write_manifest = lambda _value: (_ for _ in ()).throw(OSError("manifest failed"))
        try:
            fixture["service"].commit(fixture["projection"], _download(fixture["data"]))
        except PullCommitError as error:
            assert str(error) == "manifest_failed"
        else:
            raise AssertionError("manifest failure was accepted")
        assert _state(fixture)["journal"] == "db_committed"
        assert _state(fixture)["manifest"] == before["manifest"]
        return _row(fixture, before, {"uploads": [], "deletes": []}, "prior-manifest", "manifest writer raises OSError", ["prior manifest retained", "recovery marker written"])


def staging_cleanup_failed(root):
    with _fixture(root) as fixture:
        before = _state(fixture)
        fixture["service"]._cleanup = lambda _record: (_ for _ in ()).throw(OSError("cleanup failed"))
        result = fixture["service"].commit(fixture["projection"], _download(fixture["data"]))
        assert result == {"ok": True, "cleanup_pending": True}
        assert _state(fixture)["journal"] == "cleanup_pending"
        return _row(fixture, before, {"uploads": [], "deletes": []}, "committed-asset", "cleanup raises OSError", ["committed state retained", "cleanup pending journal"])


def heartbeat_failed(root):
    with _fixture(root) as fixture:
        class Backend:
            def __init__(self):
                self.payload = None

            def upload_file(self, path, _remote_path):
                self.payload = json.loads(Path(path).read_text(encoding="utf-8"))
                return False

        backend = Backend()
        local = {
            "version": 3,
            "memes": [
                {"filename": "confirmed.png", "name": "confirmed", "sha256": "c", "file_size": 1, "mtime": "", "sort_order": 0},
                {"filename": "pending.png", "name": "pending", "sha256": "p", "file_size": 1, "mtime": "", "sort_order": 1},
            ],
            "collections": [],
        }
        remote = {"remote.png": {"filename": "remote.png", "name": "remote", "sha256": "r", "file_size": 1, "mtime": "", "sort_order": 0}}
        _publish_heartbeat(backend, "remote-index", local, remote, ["confirmed.png"], fixture["assets"].data_dir)
        names = [meme["filename"] for meme in backend.payload["memes"]]
        assert names == ["remote.png", "confirmed.png"]
        assert not list(fixture["assets"].data_dir.glob(".manifest-heartbeat-*.json"))
        before = _state(fixture)
        row = _row(fixture, before, {"uploads": [], "deletes": [], "heartbeat_attempted": True, "heartbeat_filenames": names}, "confirmed-upload", "heartbeat upload returns False", ["no phantom entry", "heartbeat temp removed"])
        row["observed"]["final_state"]["manifest"] = {"status": "heartbeat_failed", "filenames": names}
        return row


def restart_with_journal(root):
    with _fixture(root, original=True) as fixture:
        before = _state(fixture)
        fixture["service"]._apply_metadata = lambda _value: (_ for _ in ()).throw(OSError("db failed"))
        try:
            fixture["service"].commit(fixture["projection"], _download(fixture["data"]))
        except PullCommitError:
            pass
        else:
            raise AssertionError("metadata failure was accepted")
        assert fixture["service"].recover() is True
        assert fixture["service"].recover() is False
        assert (fixture["assets"].cache_dir / "asset.png").read_bytes() == b"old"
        return _row(fixture, before, {"uploads": [], "deletes": []}, "recovery-journal", "restart from needs_recovery", ["rollback replayed", "no remote delete"])
