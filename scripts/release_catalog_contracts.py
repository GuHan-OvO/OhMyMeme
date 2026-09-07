"""Release catalog parsing and fixture validation contracts."""

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path


GENERATOR_VERSION = "todo-2-release-catalog/1"
REQUIRED_PLATFORMS = (
    ("windows-x86_64", lambda name: name.lower().endswith("-setup.exe")),
    ("linux-x86_64-appimage", lambda name: name.endswith(".AppImage")),
    ("linux-x86_64-deb", lambda name: name.endswith(".deb")),
    ("linux-x86_64-rpm", lambda name: name.endswith(".rpm")),
    ("macos-arm64", lambda name: name.endswith("-arm64.dmg")),
    ("macos-x86_64", lambda name: name.endswith("-x86_64.dmg")),
)
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_WAL_MAGIC = (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83")


class CatalogBlocked(Exception):
    """A release catalog input or fixture cannot be trusted."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def sha256_path(path):
    """Return the SHA-256 digest of one fixture or source file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_snapshot(path):
    """Parse the timestamped Releases API snapshot without accepting malformed data."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as error:
        raise CatalogBlocked("BLOCKED_INVALID_RELEASE_SNAPSHOT") from error
    if not isinstance(payload, dict):
        raise CatalogBlocked("BLOCKED_INVALID_RELEASE_SNAPSHOT")
    fetched_at = payload.get("fetched_at_utc")
    releases = payload.get("releases")
    if not isinstance(fetched_at, str) or not fetched_at.endswith("Z") or not isinstance(releases, list):
        raise CatalogBlocked("BLOCKED_INVALID_RELEASE_SNAPSHOT")
    return fetched_at, releases


def stable_releases(releases, current_version):
    """Select the exact current stable release and two immediately preceding ones."""
    stable = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or "nightly" in tag.lower():
            continue
        match = _VERSION.fullmatch(tag)
        if match is None:
            continue
        stable.append((tuple(int(value) for value in match.groups()), release))
    stable.sort(reverse=True, key=lambda item: item[0])
    supported = stable[:3]
    if len(supported) != 3:
        raise CatalogBlocked("BLOCKED_MISSING_STABLE_RELEASE")
    versions = [".".join(str(value) for value in item[0]) for item in supported]
    if versions[0] != current_version:
        raise CatalogBlocked("BLOCKED_STALE_RELEASE_SNAPSHOT")
    return supported


def platform_assets(release):
    """Return original API-backed assets for every required platform or block."""
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise CatalogBlocked("BLOCKED_MISSING_RELEASE_ASSET")
    records = []
    for platform, matches_platform in REQUIRED_PLATFORMS:
        matches = [
            asset
            for asset in assets
            if isinstance(asset, dict)
            and isinstance(asset.get("name"), str)
            and matches_platform(asset["name"])
        ]
        if len(matches) != 1:
            raise CatalogBlocked("BLOCKED_MISSING_RELEASE_ASSET")
        asset = matches[0]
        digest = asset.get("digest")
        url = asset.get("browser_download_url")
        if (
            not isinstance(url, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        ):
            raise CatalogBlocked("BLOCKED_MISSING_RELEASE_ASSET")
        records.append(
            {
                "platform": platform,
                "asset_name": asset["name"],
                "asset_url": url,
                "sha256": digest.removeprefix("sha256:"),
                "provenance": "github-release-original",
                "original_asset_available": True,
                "unavailable_reason": None,
                "signature": {
                    "status": "not-published-by-release-api",
                    "digest_source": "github-release-api",
                },
                "capabilities": {
                    "data_upgrade": "fixture-recorded",
                    "install": "artifact-recorded-not-executed",
                    "rollback_reader": "fixture-recorded",
                },
            }
        )
    return records


def validate_config_fixture(path):
    """Require readable JSON and every recorded secret to decrypt locally."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as error:
        raise CatalogBlocked("BLOCKED_INVALID_CONFIG_FIXTURE") from error
    if not isinstance(raw, dict):
        raise CatalogBlocked("BLOCKED_INVALID_CONFIG_FIXTURE")
    from ohmymeme.core.config import _SECRET_KEYS
    from ohmymeme.core.crypto import decrypt_data

    for key in _SECRET_KEYS:
        value = raw.get(key)
        if not isinstance(value, str) or not decrypt_data(value):
            raise CatalogBlocked("BLOCKED_INVALID_CONFIG_FIXTURE")


def validate_database_fixture(database_path, wal_path):
    """Require a structurally valid SQLite database and WAL sidecar."""
    try:
        wal = wal_path.read_bytes()
        if len(wal) < 32 or wal[:4] not in _WAL_MAGIC:
            raise CatalogBlocked("BLOCKED_INVALID_WAL_FIXTURE")
        connection = sqlite3.connect(database_path)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise CatalogBlocked("BLOCKED_INVALID_DATABASE_FIXTURE")
        finally:
            connection.close()
    except CatalogBlocked:
        raise
    except (OSError, sqlite3.DatabaseError) as error:
        raise CatalogBlocked("BLOCKED_INVALID_DATABASE_FIXTURE") from error


def validate_manifest_fixture(path):
    """Require a legacy v2 manifest object that current readers can migrate."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as error:
        raise CatalogBlocked("BLOCKED_INVALID_MANIFEST_FIXTURE") from error
    if not isinstance(payload, dict) or payload.get("version") != 2:
        raise CatalogBlocked("BLOCKED_INVALID_MANIFEST_FIXTURE")


def build_fixture_set(root, version):
    """Materialize one source-derived legacy data shape and return its hashes."""
    fixture_root = root / version
    fixture_root.mkdir(parents=True)
    config_path = fixture_root / "config.json"
    database_path = fixture_root / "memes.db"
    wal_path = fixture_root / "memes.db-wal"
    manifest_path = fixture_root / "meme-index.json"
    from ohmymeme.core.config import Config, _SECRET_KEYS

    config = Config(config_path)
    for secret in _SECRET_KEYS:
        config.set(secret, "release-catalog-fixture")
    config.set("version", version)
    config.save()
    with tempfile.TemporaryDirectory(dir=root) as staging:
        staged_database = Path(staging) / "memes.db"
        connection = sqlite3.connect(staged_database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE memes (id INTEGER PRIMARY KEY, filename TEXT NOT NULL, "
                "file_hash TEXT NOT NULL DEFAULT '', original_name TEXT NOT NULL DEFAULT '', "
                "width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, file_size INTEGER DEFAULT 0, "
                "mime_type TEXT DEFAULT 'image/png', created_at TEXT NOT NULL DEFAULT "
                "(datetime('now','localtime')), updated_at TEXT NOT NULL DEFAULT "
                "(datetime('now','localtime')))"
            )
            connection.execute("INSERT INTO memes (filename) VALUES ('legacy.png')")
            connection.commit()
            staged_wal = staged_database.with_name("memes.db-wal")
            if not staged_wal.exists():
                raise CatalogBlocked("BLOCKED_INVALID_WAL_FIXTURE")
            shutil.copy2(staged_database, database_path)
            shutil.copy2(staged_wal, wal_path)
        finally:
            connection.close()
    manifest_path.write_text(
        json.dumps({"version": 2, "memes": [], "collections": [{"name": "legacy", "filenames": ["legacy.png"]}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    validate_config_fixture(config_path)
    with tempfile.TemporaryDirectory(dir=root) as validation:
        validation_database = Path(validation) / "memes.db"
        validation_wal = Path(validation) / "memes.db-wal"
        shutil.copy2(database_path, validation_database)
        shutil.copy2(wal_path, validation_wal)
        validate_database_fixture(validation_database, validation_wal)
    validate_manifest_fixture(manifest_path)
    return {
        "config": sha256_path(config_path),
        "database": sha256_path(database_path),
        "wal": sha256_path(wal_path),
        "manifest": sha256_path(manifest_path),
    }
