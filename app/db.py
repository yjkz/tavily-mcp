"""SQLite storage layer (WAL mode, single shared aiosqlite connection)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tavily_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    cooldown_until REAL,
    credits_used_month REAL NOT NULL DEFAULT 0,
    plan_limit REAL NOT NULL DEFAULT 1000,
    monthly_reset_at REAL,
    total_requests INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL,
    last_error TEXT,
    last_usage_json TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tavily_keys_status ON tavily_keys(status);

CREATE TABLE IF NOT EXISTS access_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'standard',
    rpm_limit INTEGER NOT NULL DEFAULT 30,
    daily_quota INTEGER,
    monthly_credits_limit REAL,
    is_active INTEGER NOT NULL DEFAULT 1,
    last_used_at REAL,
    created_at REAL NOT NULL,
    revoked_at REAL
);

CREATE TABLE IF NOT EXISTS request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    token_id INTEGER,
    tool TEXT NOT NULL,
    tavily_key_id INTEGER,
    status TEXT NOT NULL,
    http_status INTEGER,
    credits REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    error_detail TEXT,
    request_id TEXT,
    client_ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_request_logs_ts ON request_logs(ts);
CREATE INDEX IF NOT EXISTS idx_request_logs_token ON request_logs(token_id, ts);
CREATE INDEX IF NOT EXISTS idx_request_logs_key ON request_logs(tavily_key_id, ts);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class Database:
    """Small wrapper around a shared aiosqlite connection.

    aiosqlite serializes statements on a background thread, so concurrent
    coroutines can safely share one connection.
    """

    def __init__(self, path: Path):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Column additions for databases created by older versions."""
        rows = await self.fetchall("PRAGMA table_info(access_tokens)")
        names = {r["name"] for r in rows}
        if "token_enc" not in names:
            await self._conn.execute("ALTER TABLE access_tokens ADD COLUMN token_enc TEXT")
        rows = await self.fetchall("PRAGMA table_info(request_logs)")
        names = {r["name"] for r in rows}
        if "client_ip" not in names:
            await self._conn.execute("ALTER TABLE request_logs ADD COLUMN client_ip TEXT")

    async def get_setting(self, key: str) -> Optional[str]:
        row = await self.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, time.time()),
        )

    async def delete_setting(self, key: str) -> None:
        await self.execute("DELETE FROM settings WHERE key = ?", (key,))

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    async def execute(self, sql: str, params: tuple = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self._conn.commit()
        return cur.lastrowid or 0

    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)
