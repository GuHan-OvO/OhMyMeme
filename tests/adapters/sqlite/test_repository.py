import sqlite3

import pytest

from ohmymeme.core.adapters.sqlite.repository import (
    SqliteMemeQuery,
    SqliteMemeRepository,
)
from ohmymeme.core.database import MemeDB


def test_typed_query_when_relations_filters_and_sorting_are_present_then_matches_legacy_results(
    tmp_path,
):
    # Given: all relation filters plus custom collection ordering
    database_path = tmp_path / "memes.db"
    legacy = MemeDB(database_path)
    first = legacy.add_meme("first.png", tags=["shared", "first"])
    second = legacy.add_meme("second.png", tags=["shared"])
    collection = legacy.create_collection("parity")
    legacy.add_to_collection(first, collection)
    legacy.add_to_collection(second, collection)
    legacy.reorder_collection_members(collection, [second, first])

    # When: the typed adapter runs the equivalent search and count
    adapter = SqliteMemeRepository(database_path)
    adapter.open()
    query = SqliteMemeQuery(
        tags=("shared",), collection_ids=(collection,), limit=20
    )
    rows = adapter.search(query)

    # Then: relation intersection, count, and member sort equal the public facade
    assert [row.id for row in rows] == [second, first]
    assert adapter.count(query) == legacy.count(tags=["shared"], collection_id=collection)
    legacy.close()
    adapter.close()


def test_migration_interruption_when_alter_fails_then_database_rolls_back_without_partial_columns(
    tmp_path,
):
    # Given: a legacy database and a connection that loses storage during migration
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE memes (id INTEGER PRIMARY KEY, filename TEXT NOT NULL)")
    connection.commit()
    connection.close()

    class InterruptingConnection:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self._alters = 0

        def execute(self, statement, *parameters):
            if statement.startswith("ALTER TABLE"):
                self._alters += 1
                if self._alters == 2:
                    raise sqlite3.OperationalError("disk full")
            return self._wrapped.execute(statement, *parameters)

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    def connect(*args, **kwargs):
        return InterruptingConnection(sqlite3.connect(*args, **kwargs))

    # When: a migration loses storage after its first ALTER
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        SqliteMemeRepository(database_path, connect).open()

    # Then: the transaction leaves the original legacy table untouched
    check = sqlite3.connect(database_path)
    columns = {row[1] for row in check.execute("PRAGMA table_info(memes)").fetchall()}
    check.close()
    assert columns == {"id", "filename"}
