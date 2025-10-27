"""
責務：SQLite にユーザー鍵を保存・取得
依存：Python 標準 sqlite3 のみ
"""
import asyncio
import os
import sqlite3
from pathlib import Path
from typing import Optional

# ---------- DB 初期化 ------------------------------------------------------

_DB_PATH = Path(os.environ.get("KEY_DB_PATH", "user_keys.db"))

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS user_keys (
    key_name      TEXT PRIMARY KEY,
    api_key       TEXT NOT NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# シングルトン接続（check_same_thread=False でスレッド間共有可）
_conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL")  # 軽い同時アクセス耐性
_conn.execute("PRAGMA foreign_keys=ON")
_conn.execute(CREATE_SQL)
_conn.commit()

# ---------- CRUD -----------------------------------------------------------


async def _run_blocking(fn, *args, **kwargs):
    """sqlite3 はブロッキングなのでスレッドプールで回避"""
    return await asyncio.to_thread(fn, *args, **kwargs)


class UserKeyStore:
    """ユーザー別 OpenAI API Key を SQLite に保存"""

    async def init(self) -> None:
        """PostgreSQL 版との互換のためダミー実装（何もしない）"""
        return

    async def save_key(self, key_name: str, api_key: str) -> None:
        """UPSERT 相当: 主キー重複なら置換"""

        def _save():
            _conn.execute(
                """
                INSERT INTO user_keys (key_name, api_key, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key_name)
                DO UPDATE SET api_key = excluded.api_key,
                                        updated_at = CURRENT_TIMESTAMP
                """,
                (key_name, api_key),
            )
            _conn.commit()

        await _run_blocking(_save)

    async def get_key(self, key_name: str) -> Optional[str]:
        def _get():
            cur = _conn.execute(
                "SELECT api_key FROM user_keys WHERE key_name = ?",
                (key_name,),
            )
            row = cur.fetchone()
            return row[0] if row else None

        return await _run_blocking(_get)

    async def has_key(self, key_name: str) -> bool:
        return (await self.get_key(key_name)) is not None


# シングルトンインスタンス
keystore = UserKeyStore()
