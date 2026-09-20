import sqlite3
import threading

import pytest

from ohmymeme.core.adapters.sqlite.repository import (
    SqliteMemeRepository,
    UnknownDatabaseSchema,
)
from ohmymeme.core.database import MemeDB


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
