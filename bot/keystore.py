"""
Per-user API keys (OpenAI, Gyazo) and model choices in SQLite.

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
from paper2md.models import PREFERENCE_NAMES

PROVIDERS = ("openai", "gyazo")
GYAZO_SETTING = "gyazo"  # "on" / "off": upload the figures to Gyazo (Home tab); on when not saved
SETTING_NAMES = (*PREFERENCE_NAMES, GYAZO_SETTING)


def gyazo_enabled(preferences: dict[str, str]) -> bool:
    """
    Whether the user's figures go to Gyazo (when they have registered a token).

    >>> gyazo_enabled({}), gyazo_enabled({"gyazo": "off"})
    (True, False)
    """
    return preferences.get(GYAZO_SETTING) != "off"


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS user_keys (
    key_name   TEXT PRIMARY KEY,
    api_key    TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, name)
);
"""


def _restrict_permissions(path: Path) -> None:
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if candidate.exists():
            os.chmod(candidate, 0o600)


class KeyStore:
    """Async wrapper around two small SQLite tables: ``<user>_<provider>`` -> key, and model choices."""

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
                conn.executescript(_CREATE_SQL)
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

    def _preferences(self, user_id: str) -> dict[str, str]:
        rows = (
            self._connect().execute("SELECT name, value FROM user_preferences WHERE user_id = ?", (user_id,)).fetchall()
        )
        return {name: value for name, value in rows if name in SETTING_NAMES}

    def _set_preferences(self, user_id: str, values: dict[str, str | None]) -> None:
        unknown = set(values) - set(SETTING_NAMES)
        if unknown:
            raise ValueError(f"Unknown preferences: {sorted(unknown)}")
        conn = self._connect()
        with self._lock:
            for name, value in values.items():
                if value is None:
                    conn.execute("DELETE FROM user_preferences WHERE user_id = ? AND name = ?", (user_id, name))
                else:
                    conn.execute(
                        """
                        INSERT INTO user_preferences (user_id, name, value, updated_at)
                        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(user_id, name) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                        """,
                        (user_id, name, value),
                    )
            conn.commit()
        _restrict_permissions(self.path)

    def _clear_preferences(self, user_id: str) -> None:
        """Forget the model choices (the Gyazo switch is not a model choice and stays)."""
        conn = self._connect()
        with self._lock:
            conn.executemany(
                "DELETE FROM user_preferences WHERE user_id = ? AND name = ?",
                [(user_id, name) for name in PREFERENCE_NAMES],
            )
            conn.commit()

    async def get(self, user_id: str, provider: str) -> str | None:
        return await asyncio.to_thread(self._get, user_id, provider)

    async def set(self, user_id: str, provider: str, api_key: str) -> None:
        await asyncio.to_thread(self._set, user_id, provider, api_key)

    async def delete(self, user_id: str, provider: str) -> None:
        await asyncio.to_thread(self._delete, user_id, provider)

    async def keys_for(self, user_id: str) -> dict[str, str | None]:
        return {provider: await self.get(user_id, provider) for provider in PROVIDERS}

    async def preferences(self, user_id: str) -> dict[str, str]:
        """The user's saved model choices (``models.PREFERENCE_NAMES``); missing ones use the server defaults."""
        return await asyncio.to_thread(self._preferences, user_id)

    async def set_preferences(self, user_id: str, values: dict[str, str | None]) -> None:
        """Save model choices; a None value deletes that choice."""
        await asyncio.to_thread(self._set_preferences, user_id, values)

    async def clear_preferences(self, user_id: str) -> None:
        await asyncio.to_thread(self._clear_preferences, user_id)
