import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ohmymeme.core.config import Config
from ohmymeme.core.database import MemeDB
from ohmymeme.core import manifest
from ohmymeme import __version__
from scripts.baseline_contracts import canonical_bytes
from scripts.release_catalog_contracts import (
    CatalogBlocked,
    validate_config_fixture,
    validate_database_fixture,
    validate_manifest_fixture,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_release_catalog.py"
PLAN = ROOT / ".omo" / "plans" / "full-project-modular-refactor.md"


def _release(version: str, assets: list[dict[str, str]]) -> dict[str, object]:
    return {
        "id": int(version.replace(".", "")),
        "tag_name": f"v{version}",
        "name": version,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-09-01T00:00:00Z",
        "html_url": f"https://github.com/OhMyMeme/OhMyMeme/releases/tag/v{version}",
        "assets": assets,
    }


def _asset(name: str) -> dict[str, str]:
    return {
        "name": name,
        "browser_download_url": f"https://example.invalid/{name}",
        "digest": "sha256:" + "a" * 64,
    }


def _snapshot() -> list[dict[str, object]]:
    names = (
        "OhMyMeme-v{version}-setup.exe",
        "OhMyMeme-v{version}-x86_64.AppImage",
        "ohmymeme_{version}_amd64.deb",
        "ohmymeme-{version}-1.x86_64.rpm",
        "OhMyMeme-v{version}-arm64.dmg",
        "OhMyMeme-v{version}-x86_64.dmg",
    )
    current = tuple(int(part) for part in __version__.split("."))
    versions = [
        ".".join(str(part) for part in (current[0], current[1], current[2] - offset))
        for offset in range(3)
    ]
    return [
        _release(version, [_asset(name.format(version=version)) for name in names])
        for version in versions
    ]


def test_legacy_data_baseline_when_config_wal_and_manifest_are_read(tmp_path, monkeypatch):
    # Given: legacy-shaped config, SQLite and manifest data
    config_path = tmp_path / "config.json"
    config = Config(config_path)
    config.set("s3_secret_key", "fixture-secret")
    config.save()
    database_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(database_path)
    legacy.execute(
        "CREATE TABLE memes (id INTEGER PRIMARY KEY, filename TEXT NOT NULL, "
        "file_hash TEXT NOT NULL DEFAULT '', original_name TEXT NOT NULL DEFAULT '', "
        "width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, file_size INTEGER DEFAULT 0, "
        "mime_type TEXT DEFAULT 'image/png', created_at TEXT NOT NULL DEFAULT "
        "(datetime('now','localtime')), updated_at TEXT NOT NULL DEFAULT "
        "(datetime('now','localtime')))"
    )
    legacy.execute("INSERT INTO memes (filename) VALUES ('legacy.png')")
    legacy.commit()
    legacy.close()
    manifest_path = tmp_path / "meme-index.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 2,
                "memes": [],
                "collections": [{"name": "legacy", "filenames": ["legacy.png"]}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(manifest, "_index_path", lambda: manifest_path)

    # When: current readers consume the legacy-shaped fixtures
    upgraded = MemeDB(database_path)
    loaded = manifest.load()

    # Then: encrypted values, WAL migration and v2 manifest conversion remain readable
    assert Config(config_path).get("s3_secret_key") == "fixture-secret"
    assert upgraded.get_by_filename("legacy.png")["filename"] == "legacy.png"
    assert upgraded._get_conn().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert loaded == {
        "version": 3,
        "memes": [],
        "collections": [{"name": "legacy", "filenames": ["legacy.png"]}],
    }
    upgraded.close()


def test_release_catalog_when_snapshot_contains_three_stable_releases(tmp_path):
    # Given: a reproducible Releases API snapshot with original platform artifacts
    snapshot = tmp_path / "releases.json"
    prerelease = _release("9.9.9", _snapshot()[0]["assets"])
    prerelease["tag_name"] = "v9.9.9-nightly"
    prerelease["prerelease"] = True
    snapshot.write_text(
        json.dumps(
            {
                "fetched_at_utc": "2026-09-01T00:00:00Z",
                "releases": [prerelease, *_snapshot()],
            }
        ),
        encoding="utf-8",
    )
    evidence_root = tmp_path / "evidence"
    temp_root = tmp_path / "task-temp"

    # When: the catalog builder records the supported release window
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(ROOT),
            "--plan",
            str(PLAN),
            "--evidence-root",
            str(evidence_root),
            "--temp-root",
            str(temp_root),
            "--release-snapshot",
            str(snapshot),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    # Then: canonical catalog records every original asset and data fixture hash
    assert result.returncode == 0, result.stderr
    catalog = json.loads((evidence_root / "release-catalog.json").read_text(encoding="utf-8"))
    assert catalog["verdict"] == "pass"
    assert (evidence_root / "release-catalog.json").read_bytes() == canonical_bytes(catalog)
    assert [release["version"] for release in catalog["releases"]] == [
        release["tag_name"].removeprefix("v") for release in _snapshot()
    ]
    assert all(len(release["platforms"]) == 6 for release in catalog["releases"])
    assert all(
        asset["provenance"] == "github-release-original"
        and asset["sha256"] == "a" * 64
        for release in catalog["releases"]
        for asset in release["platforms"]
    )
    assert all(
        set(release["fixture_sha256"]) == {"config", "database", "wal", "manifest"}
        for release in catalog["releases"]
    )
    fixture_names = {
        "config": "config.json",
        "database": "memes.db",
        "wal": "memes.db-wal",
        "manifest": "meme-index.json",
    }
    for release in catalog["releases"]:
        fixture_root = evidence_root / "release-fixtures" / release["version"]
        for fixture, filename in fixture_names.items():
            fixture_path = fixture_root / filename
            assert fixture_path.is_file()
            assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == release[
                "fixture_sha256"
            ][fixture]
    task = json.loads((evidence_root / "task-2.json").read_text(encoding="utf-8"))
    assert task["generator_version"] == "todo-2-release-catalog/1"
    assert task["baseline_commit"] == catalog["baseline_commit"]
    assert (evidence_root / "task-2.json").read_bytes() == canonical_bytes(task)
    assert not temp_root.exists()


@pytest.mark.parametrize(
    ("snapshot_data", "expected"),
    [
        ("{", "BLOCKED_INVALID_RELEASE_SNAPSHOT"),
        (_snapshot()[:2], "BLOCKED_MISSING_STABLE_RELEASE"),
        (
            [_release("9.9.9", _snapshot()[0]["assets"]), *_snapshot()[1:]],
            "BLOCKED_STALE_RELEASE_SNAPSHOT",
        ),
        (
            [_release(__version__, [_asset(f"OhMyMeme-v{__version__}-setup.exe")]), *_snapshot()[1:]],
            "BLOCKED_MISSING_RELEASE_ASSET",
        ),
    ],
)
def test_release_catalog_rejects_incomplete_or_malformed_history(
    tmp_path, snapshot_data, expected
):
    # Given: a malformed or insufficient release snapshot
    snapshot = tmp_path / "releases.json"
    snapshot.write_text(
        snapshot_data
        if isinstance(snapshot_data, str)
        else json.dumps({"fetched_at_utc": "2026-09-01T00:00:00Z", "releases": snapshot_data}),
        encoding="utf-8",
    )

    # When: the builder reads the snapshot
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--temp-root",
            str(tmp_path / "task-temp"),
            "--release-snapshot",
            str(snapshot),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    # Then: incomplete history cannot produce a misleading successful catalog
    assert result.returncode != 0
    assert result.stderr.strip() == expected


def test_release_catalog_rejects_unavailable_release_api(tmp_path):
    # Given: a runner where the GitHub CLI cannot be executed
    environment = dict(__import__("os").environ)
    environment["PATH"] = ""

    # When: the builder has no supplied snapshot and must query the release API
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--temp-root",
            str(tmp_path / "task-temp"),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    # Then: unavailable network tooling cannot be reported as a successful catalog
    assert result.returncode != 0
    assert result.stderr.strip() == "BLOCKED_RELEASE_API_UNAVAILABLE"


def test_fixture_validators_fail_closed_for_corrupt_legacy_data(tmp_path):
    # Given: malformed config JSON, unreadable ciphertext, WAL and manifest fixtures
    config_path = tmp_path / "config.json"
    database_path = tmp_path / "memes.db"
    wal_path = tmp_path / "memes.db-wal"
    manifest_path = tmp_path / "meme-index.json"
    config_path.write_text("{", encoding="utf-8")

    # When: each malformed fixture reaches its catalog validation boundary
    with pytest.raises(CatalogBlocked, match="BLOCKED_INVALID_CONFIG_FIXTURE"):
        validate_config_fixture(config_path)
    config_path.write_text('{"s3_access_key":"unreadable"}', encoding="utf-8")
    with pytest.raises(CatalogBlocked, match="BLOCKED_INVALID_CONFIG_FIXTURE"):
        validate_config_fixture(config_path)
    wal_path.write_bytes(b"not-a-wal")
    with pytest.raises(CatalogBlocked, match="BLOCKED_INVALID_WAL_FIXTURE"):
        validate_database_fixture(database_path, wal_path)
    manifest_path.write_text("{", encoding="utf-8")
    with pytest.raises(CatalogBlocked, match="BLOCKED_INVALID_MANIFEST_FIXTURE"):
        validate_manifest_fixture(manifest_path)

    # Then: none of the malformed legacy inputs is accepted as an available fixture
