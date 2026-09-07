"""SQLite 最终模式和历史列升级定义。"""

import sqlite3


class UnknownDatabaseSchema(sqlite3.DatabaseError):
    """持久化格式不属于当前或支持的历史数据库。"""


TABLES = frozenset(
    {
        "memes",
        "tags",
        "meme_tags",
        "collections",
        "meme_collections",
        "favorites",
        "recent_uses",
    }
)
FINAL_COLUMNS = {
    "memes": frozenset(
        {
            "id",
            "filename",
            "file_hash",
            "original_name",
            "width",
            "height",
            "file_size",
            "mime_type",
            "sort_order",
            "stego_of_hash",
            "from_stego",
            "created_at",
            "updated_at",
        }
    ),
    "tags": frozenset({"id", "name"}),
    "meme_tags": frozenset({"meme_id", "tag_id"}),
    "collections": frozenset({"id", "name", "parent_id", "sort_order"}),
    "meme_collections": frozenset({"meme_id", "collection_id", "sort_order"}),
    "favorites": frozenset({"meme_id", "added_at"}),
    "recent_uses": frozenset({"meme_id", "used_at"}),
}
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS memes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        file_hash TEXT NOT NULL DEFAULT '',
        original_name TEXT NOT NULL DEFAULT '',
        width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, file_size INTEGER DEFAULT 0,
        mime_type TEXT DEFAULT 'image/png', sort_order INTEGER DEFAULT 0,
        stego_of_hash TEXT DEFAULT NULL, from_stego INTEGER DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",
    """CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE
    )""",
    """CREATE TABLE IF NOT EXISTS meme_tags (
        meme_id INTEGER NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (meme_id, tag_id)
    )""",
    """CREATE TABLE IF NOT EXISTS collections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL COLLATE NOCASE,
        parent_id INTEGER DEFAULT NULL REFERENCES collections(id) ON DELETE CASCADE,
        sort_order INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS meme_collections (
        meme_id INTEGER NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
        collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        sort_order INTEGER DEFAULT 0, PRIMARY KEY (meme_id, collection_id)
    )""",
    """CREATE TABLE IF NOT EXISTS favorites (
        meme_id INTEGER PRIMARY KEY REFERENCES memes(id) ON DELETE CASCADE,
        added_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",
    """CREATE TABLE IF NOT EXISTS recent_uses (
        meme_id INTEGER NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
        used_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        PRIMARY KEY (meme_id)
    )""",
)
MIGRATIONS = (
    ("memes", "sort_order", "INTEGER DEFAULT 0"),
    ("memes", "stego_of_hash", "TEXT DEFAULT NULL"),
    ("memes", "from_stego", "INTEGER DEFAULT 0"),
    (
        "collections",
        "parent_id",
        "INTEGER DEFAULT NULL REFERENCES collections(id) ON DELETE CASCADE",
    ),
    ("collections", "sort_order", "INTEGER DEFAULT 0"),
    ("meme_collections", "sort_order", "INTEGER DEFAULT 0"),
)
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_memes_hash ON memes(file_hash)",
    "CREATE INDEX IF NOT EXISTS idx_memes_name ON memes(filename)",
    "CREATE INDEX IF NOT EXISTS idx_recent_uses_at ON recent_uses(used_at)",
    "CREATE INDEX IF NOT EXISTS idx_memes_stego ON memes(stego_of_hash)",
)
