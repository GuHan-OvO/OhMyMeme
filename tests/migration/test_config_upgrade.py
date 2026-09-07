import hashlib
import json
import threading

import pytest

from ohmymeme.core import crypto
from ohmymeme.core.config import Config, ConfigCorrupt


def _secret() -> str:
    return hashlib.sha256(b"config-upgrade-fixture").hexdigest()


def test_historical_config_when_loaded_and_saved_then_known_values_and_future_fields_survive(
    tmp_path,
):
    # Given: a pre-copy-mode config with an encrypted secret and a future field
    config_path = tmp_path / "config.json"
    custom_cache = tmp_path / "custom-cache"
    config_path.write_text(
        json.dumps(
            {
                "cache_dir": str(custom_cache),
                "copy_resize_enabled": False,
                "s3_secret_key": crypto.encrypt_data(_secret()),
                "future_setting": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )

    # When: the current reader migrates and persists the fixture twice
    config = Config(config_path)
    config.save()
    Config(config_path).save()

    # Then: migration, secret readability, cache semantics and future data persist
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert config.get("copy_resize_mode") == 0
    assert config.get("s3_secret_key") == _secret()
    assert config.cache_dir == custom_cache.resolve()
    assert saved["future_setting"] == {"enabled": True}


def test_xor_ciphertext_when_cryptography_becomes_available_then_current_reader_decrypts_it(
    monkeypatch,
):
    # Given: a value written by the historical no-cryptography fallback
    machine_id = "config-fixture-machine"
    monkeypatch.setattr(crypto, "HAS_CRYPTO", False)
    ciphertext = crypto.encrypt_data(_secret(), machine_id)
    monkeypatch.setattr(crypto, "HAS_CRYPTO", True)

    # When: a cryptography-capable runtime reads the historical ciphertext
    value = crypto.decrypt_data(ciphertext, machine_id)

    # Then: the XOR payload remains readable
    assert value == _secret()


def test_ciphertext_when_machine_id_is_wrong_then_decryption_fails_without_disclosing_value():
    # Given: a Fernet ciphertext owned by a different machine identity
    ciphertext = crypto.encrypt_data(_secret(), "config-fixture-machine")

    # When: another machine identity attempts to read it
    value = crypto.decrypt_data(ciphertext, "other-machine")

    # Then: decryption fails deterministically
    assert value == ""


def test_corrupt_json_when_config_opens_then_file_is_retained_and_error_is_deterministic(
    tmp_path,
):
    # Given: an interrupted write left invalid JSON at the authoritative path
    config_path = tmp_path / "config.json"
    original = b'{"hotkey":'
    config_path.write_bytes(original)

    # When/Then: opening fails instead of silently replacing the user's file
    with pytest.raises(ConfigCorrupt, match="invalid configuration JSON"):
        Config(config_path)
    assert config_path.read_bytes() == original


def test_write_interruption_when_fsync_fails_then_original_file_and_dirty_snapshot_remain(
    tmp_path, monkeypatch
):
    # Given: an existing authoritative configuration and a pending update
    config_path = tmp_path / "config.json"
    original = b'{"hotkey":"old"}\n'
    config_path.write_bytes(original)
    config = Config(config_path)
    config.set("hotkey", "new")
    monkeypatch.setattr("ohmymeme.core.config.os.fsync", lambda _descriptor: (_ for _ in ()).throw(OSError("interrupted write")))

    # When: the temporary snapshot cannot be durably flushed
    with pytest.raises(OSError, match="interrupted write"):
        config.save()

    # Then: callers can retry and the previously committed file is untouched
    assert config_path.read_bytes() == original
    assert not config_path.with_name("config.json.tmp").exists()
    assert config._dirty


def test_concurrent_instances_when_saving_distinct_fields_then_neither_update_is_lost(tmp_path):
    # Given: two independently loaded configuration instances
    config_path = tmp_path / "config.json"
    first = Config(config_path)
    first.save()
    left = Config(config_path)
    right = Config(config_path)
    barrier = threading.Barrier(2)
    errors = []

    def save_setting(config, key, value):
        config.set(key, value)
        barrier.wait()
        try:
            config.save()
        except OSError as error:
            errors.append(error)

    # When: both instances persist a different update at the same time
    workers = (
        threading.Thread(target=save_setting, args=(left, "hotkey", "Ctrl+Shift+U")),
        threading.Thread(target=save_setting, args=(right, "theme", "light")),
    )
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    # Then: the file remains valid and contains both snapshots
    assert errors == []
    saved = Config(config_path)
    assert saved.get("hotkey") == "Ctrl+Shift+U"
    assert saved.get("theme") == "light"


def test_malformed_or_missing_secret_when_read_then_legacy_access_contract_is_preserved(tmp_path):
    # Given: historical files with unreadable and absent secret values
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"s3_secret_key":"not-a-ciphertext"}', encoding="utf-8")
    missing = tmp_path / "missing.json"
    missing.write_text("{}", encoding="utf-8")

    # When: legacy Config readers request those settings
    malformed_config = Config(malformed)
    missing_config = Config(missing)

    # Then: get retains the historical unreadable value while UI export stays blank
    assert malformed_config.get("s3_secret_key") == "not-a-ciphertext"
    assert malformed_config.to_dict()["s3_secret_key"] == ""
    assert missing_config.get("s3_secret_key") == ""
