"""Shared test fixtures: in-memory-ish SQLite, KeyPool, AppState, fake Tavily."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Config
from app.db import Database
from app.pool import KeyPool
from app.state import AppState
from app.tavily import TavilyClient, TavilyError


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        admin_password="test-admin-pw",
        session_secret="test-secret",
        data_dir=tmp_path,
        host="127.0.0.1",
        port=0,
        cooldown_seconds=60.0,
        usage_sync_interval_hours=6.0,
        log_retention_days=30,
        default_token_rpm=30,
        max_retries=4,
        character_limit=25000,
        cookie_secure=False,
        dev_mode=True,
    )


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def pool(db: Database) -> KeyPool:
    key_pool = KeyPool(db, cooldown_seconds=60.0)
    await key_pool.add_keys(
        [
            ("tvly-test-key-aaaa1111", "key-A", 1000.0),
            ("tvly-test-key-bbbb2222", "key-B", 1000.0),
            ("tvly-test-key-cccc3333", "key-C", 1000.0),
        ]
    )
    return key_pool


class FakeTavily(TavilyClient):
    """Scriptable stand-in for the upstream; responses are queued per key prefix."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, str]] = []  # (api_key, path)
        self.responses: dict[str, list] = {}  # key prefix -> list of dict | TavilyError
        self.default_response: dict | TavilyError | None = None

    def queue(self, key_prefix: str, *outcomes) -> None:
        self.responses.setdefault(key_prefix, []).extend(outcomes)

    async def _request(self, method, path, api_key, payload=None):
        self.calls.append((api_key, path))
        outcome = None
        for queued_prefix, queue in self.responses.items():
            if api_key.startswith(queued_prefix) and queue:
                outcome = queue.pop(0)
                break
        if outcome is None:
            outcome = self.default_response
        if isinstance(outcome, TavilyError):
            raise outcome
        if outcome is None:
            raise AssertionError(f"no scripted response for key {api_key} at {path}")
        result = dict(outcome)
        if path != "/usage":
            result.setdefault("request_id", "req_fake_1")
        return result


@pytest.fixture
def fake_tavily() -> FakeTavily:
    return FakeTavily()


SEARCH_OK = {
    "query": "test",
    "answer": "42",
    "results": [
        {"title": "Result 1", "url": "https://example.com/1", "content": "hello world", "score": 0.99},
        {"title": "Result 2", "url": "https://example.com/2", "content": "second", "score": 0.5},
    ],
    "usage": {"credits": 1.0},
}


USAGE_OK = {
    "key": {"usage": 150, "limit": 1000, "search_usage": 100},
    "account": {"current_plan": "Free", "plan_usage": 150, "plan_limit": 1000},
}


@pytest.fixture
def state(config, db, pool, fake_tavily) -> AppState:
    return AppState(config=config, db=db, pool=pool, tavily=fake_tavily)
