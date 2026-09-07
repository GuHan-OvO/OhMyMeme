import hashlib
import threading
import time

from ohmymeme.services import updates as updater


def test_download_release_rejects_unregistered_asset_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        updater,
        "_urlretrieve_mirror",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network")),
    )

    assert updater.download_release("https://public.example/release.exe") is None


def test_release_asset_digest_is_required_and_recorded(monkeypatch):
    url = "https://github.example/release.exe"
    monkeypatch.setattr(updater.platform, "system", lambda: "Windows")

    assert updater._pick_asset_url(
        [{"name": "OhMyMeme-setup.exe", "browser_download_url": url}]
    ) == url
    assert url not in updater._ASSET_HASHES
    assert updater._pick_asset_url(
        [
            {
                "name": "OhMyMeme-setup.exe",
                "browser_download_url": url,
                "digest": "sha256:" + "a" * 64,
            }
        ]
    ) == url
    assert updater._ASSET_HASHES[url] == "a" * 64


def test_download_release_rejects_hash_mismatch_without_publishing(tmp_path, monkeypatch):
    url = "https://public.example/release.exe"
    expected = hashlib.sha256(b"trusted").hexdigest()
    monkeypatch.setitem(updater._ASSET_HASHES, url, expected)
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))

    def write_untrusted(_url, destination, *_args, **_kwargs):
        destination_path = __import__("pathlib").Path(destination)
        destination_path.write_bytes(b"untrusted")

    monkeypatch.setattr(updater, "_urlretrieve_mirror", write_untrusted)

    assert updater.download_release(url) is None
    assert not (tmp_path / "release.exe").exists()


def test_run_installer_requires_verified_asset_hash(tmp_path, monkeypatch):
    installer = tmp_path / "release.exe"
    installer.write_bytes(b"trusted")
    started = []
    monkeypatch.setattr(updater.platform, "system", lambda: "Windows")
    monkeypatch.setattr(updater.os, "startfile", started.append)

    assert updater.run_installer(str(installer)) is False
    assert updater.run_installer(
        str(installer), hashlib.sha256(b"trusted").hexdigest()
    ) is True
    assert started == [str(installer)]


def test_start_download_rejects_unsigned_url():
    assert updater.start_download("https://public.example/release.exe") is False


def test_start_download_does_not_mark_untrusted_bytes_done(tmp_path, monkeypatch):
    url = "https://public.example/async-release.exe"
    monkeypatch.setitem(updater._ASSET_HASHES, url, hashlib.sha256(b"trusted").hexdigest())
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
    completed = threading.Event()

    def write_untrusted(_url, destination, *_args, **_kwargs):
        __import__("pathlib").Path(destination).write_bytes(b"untrusted")
        completed.set()

    monkeypatch.setattr(updater, "_urlretrieve_mirror", write_untrusted)
    with updater._download_lock:
        updater._download_state.update(status="idle", progress=0, error="", path=None)

    assert updater.start_download(url) is True
    assert completed.wait(2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if updater.get_download_progress()["status"] == "error":
            break
        completed.wait(0.01)
    assert updater.get_download_progress()["status"] == "error"
    assert not (tmp_path / "async-release.exe").exists()
