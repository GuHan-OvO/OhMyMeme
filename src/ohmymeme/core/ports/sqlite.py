"""SQLite 表情查询端口。"""

from collections.abc import Sequence
from typing import Protocol

from ohmymeme.core.adapters.sqlite.repository import MemeRecord, SqliteMemeQuery


class MemeQueryPort(Protocol):
    def search(self, query: SqliteMemeQuery) -> Sequence[MemeRecord]: ...

    def count(self, query: SqliteMemeQuery) -> int: ...
