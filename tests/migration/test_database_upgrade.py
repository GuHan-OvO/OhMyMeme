import hashlib
import json
import os
import sqlite3
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ohmymeme.core.adapters.sqlite.repository import (
    SqliteMemeRepository,
    UnknownDatabaseSchema,
)
from ohmymeme.core.database import MemeDB


ROOT = Path(__file__).resolve().parents[2]
CATALOG_SCRIPT = ROOT / "scripts" / "build_release_catalog.py"


def _legacy_database(path):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE memes ("
        "id INTEGER PRIMARY KEY, filename TEXT NOT NULL, "
        "file_hash TEXT NOT NULL DEFAULT '', original_name TEXT NOT NULL DEFAULT '', "
        "width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, file_size INTEGER DEFAULT 0, "
        "mime_type TEXT DEFAULT 'image/png', created_at TEXT NOT NULL DEFAULT "
        "(datetime('now','localtime')), updated_at TEXT NOT NULL DEFAULT "
        "(datetime('now','localtime')))"
    )
    connection.execute("INSERT INTO memes (filename) VALUES ('legacy.png')")
    connection.commit()
    connection.close()


def test_historical_database_when_opened_twice_then_final_schema_and_data_are_preserved(tmp_path):
    # Given: the oldest supported memes table format
    database_path = tmp_path / "memes.db"
    _legacy_database(database_path)

    # When: two current readers open the persisted database
    first = MemeDB(database_path)
    first.close()
    second = MemeDB(database_path)

    # Then: the old row, WAL, final columns, and indexes remain available
    assert second.get_by_filename("legacy.png")["filename"] == "legacy.png"
    assert second._get_conn().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert {
        row[1]
        for row in second._get_conn().execute("PRAGMA table_info(memes)").fetchall()
    } >= {"sort_order", "stego_of_hash", "from_stego"}
    assert {
        row[1]
        for row in second._get_conn().execute("PRAGMA index_list(memes)").fetchall()
    } >= {"idx_memes_hash", "idx_memes_name", "idx_memes_stego"}
    second.close()


def test_unknown_schema_when_opened_then_adapter_fails_without_resetting_user_data(tmp_path):
    # Given: an unrecognised persisted table beside a valid SQLite file
    database_path = tmp_path / "unknown.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE unknown_future_table (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    original = database_path.read_bytes()

    # When/Then: the adapter refuses to infer a destructive upgrade
    with pytest.raises(UnknownDatabaseSchema):
        SqliteMemeRepository(database_path).open()
    assert database_path.read_bytes() == original


def test_foreign_key_corruption_when_opened_then_adapter_fails_closed(tmp_path):
    # Given: a real final-format database with an orphaned relation
    database_path = tmp_path / "orphan.db"
    database = MemeDB(database_path)
    meme_id = database.add_meme("orphan.png")
    collection_id = database.create_collection("orphaned")
    database.add_to_collection(meme_id, collection_id)
    database.close()
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("DELETE FROM memes WHERE id=?", (meme_id,))
    connection.commit()
    connection.close()

    # When/Then: no implicit reset hides the broken persisted relation
    with pytest.raises(UnknownDatabaseSchema, match="foreign key"):
        SqliteMemeRepository(database_path).open()


def test_todo2_wal_fixture_when_copied_and_reopened_then_historical_query_results_survive(
    tmp_path,
):
    # Given: Todo 2's source-format historical DB/WAL fixture outside the worktree
    catalog_value = os.environ.get("OHMYMEME_TODO2_CATALOG")
    if catalog_value is None:
        pytest.skip("Todo 2 external evidence root is not configured")
    catalog_path = Path(catalog_value)
    if not catalog_path.is_file():
        pytest.skip("Todo 2 external evidence root is not configured")
    catalog = json.loads(catalog_path.read_text("utf-8"))
    fixture_root = catalog_path.parent / "release-fixtures"
    for release in catalog["releases"]:
        pair_root = fixture_root / release["version"]
        assert hashlib.sha256((pair_root / "memes.db").read_bytes()).hexdigest() == release[
            "fixture_sha256"
        ]["database"]
        assert hashlib.sha256((pair_root / "memes.db-wal").read_bytes()).hexdigest() == release[
            "fixture_sha256"
        ]["wal"]
    release = catalog["releases"][0]
    fixture_root = fixture_root / release["version"]
    source_database = fixture_root / "memes.db"
    source_wal = fixture_root / "memes.db-wal"
    database_path = tmp_path / "memes.db"
    wal_path = tmp_path / "memes.db-wal"
    assert source_database.is_file()
    assert source_wal.is_file()
    assert hashlib.sha256(source_database.read_bytes()).hexdigest() == release[
        "fixture_sha256"
    ]["database"]
    assert hashlib.sha256(source_wal.read_bytes()).hexdigest() == release[
        "fixture_sha256"
    ]["wal"]
    shutil.copy2(source_database, database_path)
    shutil.copy2(source_wal, wal_path)
    assert database_path.is_file()
    assert wal_path.is_file()

    # When: current readers open the disposable fixture twice
    first = MemeDB(database_path)
    assert first.count() == 1
    assert first.search()[0]["filename"] == "legacy.png"
    first.close()
    second = MemeDB(database_path)

    # Then: WAL-backed historical data remains queryable after idempotent reopening
    assert second.get_by_filename("legacy.png")["filename"] == "legacy.png"
    second.close()


def test_fresh_catalog_when_todo12_opens_each_declared_fixture_then_db_and_wal_match(
    tmp_path,
):
    # Given: a frozen Todo 2 release snapshot and an unused external catalog root
    snapshot_value = os.environ.get("OHMYMEME_TODO2_SNAPSHOT")
    if snapshot_value is None:
        pytest.skip("Todo 2 release snapshot is not configured")
    snapshot_path = Path(snapshot_value)
    if not snapshot_path.is_file():
        pytest.skip("Todo 2 release snapshot is not configured")
    evidence_root = tmp_path / "catalog"
    temporary_root = tmp_path / "catalog-temp"

    # When: Todo 2 materializes a new fixture catalog
    result = subprocess.run(
        [
            sys.executable,
            str(CATALOG_SCRIPT),
            "--evidence-root",
            str(evidence_root),
            "--temp-root",
            str(temporary_root),
            "--release-snapshot",
            str(snapshot_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    # Then: every catalog-declared DB/WAL pair hashes and opens through Todo 12
    assert result.returncode == 0, result.stderr
    catalog = json.loads((evidence_root / "release-catalog.json").read_text("utf-8"))
    for release in catalog["releases"]:
        fixture_root = evidence_root / "release-fixtures" / release["version"]
        database_path = fixture_root / "memes.db"
        wal_path = fixture_root / "memes.db-wal"
        assert hashlib.sha256(database_path.read_bytes()).hexdigest() == release[
            "fixture_sha256"
        ]["database"]
        assert hashlib.sha256(wal_path.read_bytes()).hexdigest() == release[
            "fixture_sha256"
        ]["wal"]
        copied_database = tmp_path / f"{release['version']}.db"
        copied_wal = tmp_path / f"{release['version']}.db-wal"
        shutil.copy2(database_path, copied_database)
        shutil.copy2(wal_path, copied_wal)
        database = MemeDB(copied_database)
        assert database.get_by_filename("legacy.png")["filename"] == "legacy.png"
        database.close()
    assert not temporary_root.exists()


def test_concurrent_open_when_upgrading_one_legacy_file_then_every_reader_observes_final_schema(
    tmp_path,
):
    # Given: simultaneous readers racing to open one historical database
    database_path = tmp_path / "concurrent.db"
    _legacy_database(database_path)
    barrier = threading.Barrier(2)
    errors = []

    def open_database():
        barrier.wait()
        try:
            database = MemeDB(database_path)
            database.close()
        except sqlite3.Error as error:
            errors.append(error)

    # When: both connections perform startup migration concurrently
    threads = (threading.Thread(target=open_database), threading.Thread(target=open_database))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Then: neither reader sees a partial schema or reports a misleading success
    assert errors == []
    database = MemeDB(database_path)
    assert database.get_by_filename("legacy.png")["filename"] == "legacy.png"
    database.close()
