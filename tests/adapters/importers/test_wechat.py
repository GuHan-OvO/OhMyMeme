import hashlib

from ohmymeme.integrations.imports import wechat


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

    assert wechat._apply_wal(output, b"key", str(wal)) == 0
    assert bytes(output) == before
