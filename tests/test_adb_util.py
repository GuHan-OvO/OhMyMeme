"""adb_util._find_qq_favorite_dir 分支测试（mock _run_adb，不访问真机）"""

import subprocess
import sys
import hashlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ohmymeme.integrations.imports.adb_qq as A

SUFFIX = A._QQ_FAVORITE_SUFFIX
PRIMARY = "/storage/emulated/0/" + SUFFIX
SDCARD = "/sdcard/" + SUFFIX
TF = "/storage/1234-5678/" + SUFFIX


class _R:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out


def _fake_run(calls, hits):
    def fake_run(adb, args, timeout=30):
        calls.append(args)
        if args[0:3] == ["shell", "ls", "/storage/"]:
            return _R(0, "emulated\nself\n1234-5678\n")
        if args[0:2] == ["shell", "test"]:
            return _R(0 if args[-1] in hits else 1)
        return _R(1)

    return fake_run


def _setup(monkeypatch, hits, cancelled=False):
    calls = []
    monkeypatch.setattr(A, "_run_adb", _fake_run(calls, hits))
    monkeypatch.setattr(A, "_check_cancel", lambda: cancelled)
    return calls


def test_primary_hit(monkeypatch):
    calls = _setup(monkeypatch, {PRIMARY})
    assert A._find_qq_favorite_dir("adb") == PRIMARY
    assert calls[0] == ["shell", "ls", "/storage/"]
    assert calls[1] == ["shell", "test", "-d", PRIMARY]
    assert len(calls) == 2  # 命中即返回，不再探测后续候选


def test_sdcard_fallback(monkeypatch):
    calls = _setup(monkeypatch, {SDCARD})
    assert A._find_qq_favorite_dir("adb") == SDCARD
    assert calls[1] == ["shell", "test", "-d", PRIMARY]
    assert calls[2] == ["shell", "test", "-d", SDCARD]


def test_external_volume_hit_and_skips_non_volume(monkeypatch):
    calls = _setup(monkeypatch, {TF})
    assert A._find_qq_favorite_dir("adb") == TF
    probed = [c for c in calls if c[0:2] == ["shell", "test"]]
    # emulated / self 被过滤，不进入候选
    assert all("/storage/emulated/" not in c[-1] or c[-1] == PRIMARY for c in probed)
    assert all("/storage/self" not in c[-1] for c in probed)
    assert ["shell", "test", "-d", TF] in probed


def test_all_candidates_fail_returns_none(monkeypatch):
    calls = _setup(monkeypatch, set())
    assert A._find_qq_favorite_dir("adb") is None
    probed = [c for c in calls if c[0:2] == ["shell", "test"]]
    # 主存储、sdcard、TF 卡卷都探测过
    assert len(probed) == 3


def test_probe_timeout_propagates(monkeypatch):
    def boom(adb, args, timeout=30):
        raise subprocess.TimeoutExpired(["adb"], timeout)

    monkeypatch.setattr(A, "_run_adb", boom)
    monkeypatch.setattr(A, "_check_cancel", lambda: False)
    with pytest.raises(subprocess.TimeoutExpired):
        A._find_qq_favorite_dir("adb")


def test_cancel_during_probe_returns_none(monkeypatch):
    _setup(monkeypatch, {PRIMARY}, cancelled=True)
    assert A._find_qq_favorite_dir("adb") is None


def test_adb_download_requires_configured_archive_and_binary_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_get_adb_dir", lambda: tmp_path / ".adb")
    monkeypatch.setattr(A, "_adb_download_url", lambda: "https://public.example/adb.zip")
    monkeypatch.setitem(A._ADB_SHA256, "Windows", "")
    monkeypatch.setitem(A._ADB_BINARY_SHA256, "Windows", "")
    monkeypatch.setattr(A.platform, "system", lambda: "Windows")

    assert A._download_with_progress() is False
    assert not (tmp_path / ".adb" / "platform-tools.zip").exists()


def test_adb_download_rejects_archive_hash_mismatch_before_extract(tmp_path, monkeypatch):
    import io
    import zipfile

    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        archive.writestr("platform-tools/adb", b"adb")

    class Policy:
        def download_to(self, url, destination, **kwargs):
            destination.write_bytes(archive_data.getvalue())

    monkeypatch.setattr(A, "_get_adb_dir", lambda: tmp_path / ".adb")
    monkeypatch.setattr(A, "_adb_download_url", lambda: "https://public.example/adb.zip")
    monkeypatch.setattr(A, "_FETCH_POLICY", Policy())
    monkeypatch.setattr(A.platform, "system", lambda: "Windows")
    monkeypatch.setitem(A._ADB_SHA256, "Windows", "0" * 64)
    monkeypatch.setitem(A._ADB_BINARY_SHA256, "Windows", hashlib.sha256(b"adb").hexdigest())

    assert A._download_with_progress() is False
    assert not (tmp_path / ".adb" / "platform-tools" / "adb").exists()
