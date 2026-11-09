"""
Per-user API keys (OpenAI, Gyazo) in SQLite.

The database file (and its -wal/-shm files) is made readable by the owner only:
on a shared server anyone could otherwise copy every user's keys.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from pathlib import Path

from paper2md.config import Settings

PROVIDERS = ("openai", "gyazo")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS user_keys (
    key_name   TEXT PRIMARY KEY,
    api_key    TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _restrict_permissions(path: Path) -> None:
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if candidate.exists():
            os.chmod(candidate, 0o600)


class KeyStore:
    """Async wrapper around a small SQLite table (``<user>_<provider>`` -> key)."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path else Settings.from_env().keystore_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.touch(mode=0o600, exist_ok=True)
                conn = sqlite3.connect(self.path, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(_CREATE_SQL)
                conn.commit()
                _restrict_permissions(self.path)
                self._conn = conn
            return self._conn

    @staticmethod
    def _name(user_id: str, provider: str) -> str:
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider: {provider}")
        return f"{user_id}_{provider}"

    def _get(self, user_id: str, provider: str) -> str | None:
        row = (
            self._connect()
            .execute("SELECT api_key FROM user_keys WHERE key_name = ?", (self._name(user_id, provider),))
            .fetchone()
        )
        return row[0] if row else None

    def _set(self, user_id: str, provider: str, api_key: str) -> None:
        conn = self._connect()
        with self._lock:
            conn.execute(
                """
                INSERT INTO user_keys (key_name, api_key, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key_name) DO UPDATE SET api_key = excluded.api_key, updated_at = CURRENT_TIMESTAMP
                """,
                (self._name(user_id, provider), api_key),
            )
            conn.commit()
        _restrict_permissions(self.path)

    def _delete(self, user_id: str, provider: str) -> None:
        conn = self._connect()
        with self._lock:
            conn.execute("DELETE FROM user_keys WHERE key_name = ?", (self._name(user_id, provider),))
            conn.commit()

    async def get(self, user_id: str, provider: str) -> str | None:
        return await asyncio.to_thread(self._get, user_id, provider)

    async def set(self, user_id: str, provider: str, api_key: str) -> None:
        await asyncio.to_thread(self._set, user_id, provider, api_key)

    async def delete(self, user_id: str, provider: str) -> None:
        await asyncio.to_thread(self._delete, user_id, provider)

    async def keys_for(self, user_id: str) -> dict[str, str | None]:
        return {provider: await self.get(user_id, provider) for provider in PROVIDERS}
