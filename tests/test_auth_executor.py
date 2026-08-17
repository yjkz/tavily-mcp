"""DbTokenVerifier and PooledExecutor failover tests."""

from __future__ import annotations

import hashlib
import time

import pytest
from fastmcp.exceptions import ToolError

from app.mcp_server import DbTokenVerifier, PooledExecutor, RateLimiter
from app.tavily import TavilyError
from tests.conftest import SEARCH_OK, USAGE_OK


async def _insert_token(db, name="dev", tier="standard", rpm=30, **extra) -> int:
    raw = "tpm_" + "x" * 40
    return await db.execute(
        "INSERT INTO access_tokens (name, token_hash, prefix, tier, rpm_limit, is_active, created_at) "
        "VALUES (?,?,?,?,?,1,?)",
        (name, hashlib.sha256(raw.encode()).hexdigest(), raw[:12], tier, rpm, time.time()),
    ), raw


async def test_verifier_accepts_valid_token(db):
    token_id, raw = await _insert_token(db)
    verifier = DbTokenVerifier(db)
    access = await verifier.verify_token(raw)
    assert access is not None
    assert access.client_id == str(token_id)
    assert access.claims["token_id"] == token_id
    assert access.claims["tier"] == "standard"
    row = await db.fetchone("SELECT last_used_at FROM access_tokens WHERE id=?", (token_id,))
    assert row["last_used_at"] is not None


async def test_verifier_rejects_unknown_or_revoked(db):
    token_id, raw = await _insert_token(db)
    verifier = DbTokenVerifier(db)
    assert await verifier.verify_token("tpm_unknown") is None
    assert await verifier.verify_token("wrong-prefix-token") is None
    await db.execute("UPDATE access_tokens SET is_active=0 WHERE id=?", (token_id,))
    assert await verifier.verify_token(raw) is None


def test_rate_limiter_window():
    limiter = RateLimiter()
    assert all(limiter.allow(1, 3) for _ in range(3))
    assert limiter.allow(1, 3) is False
    assert limiter.allow(2, 3) is True  # independent per token


@pytest.fixture
async def executor(state):
    return PooledExecutor(state)


async def test_success_uses_pool_and_logs(executor, state, fake_tavily):
    _, raw = await _insert_token(state.db)
    fake_tavily.default_response = SEARCH_OK
    data = await executor.run("search", {"query": "hello"})
    assert data["answer"] == "42"
    ks = state.pool.get(1)
    assert ks.total_requests == 1
    assert ks.credits_used_month == 1.0
    row = await state.db.fetchone(
        "SELECT token_id, tool, status, credits, tavily_key_id FROM request_logs ORDER BY id DESC"
    )
    assert row["status"] == "success"
    assert row["credits"] == 1.0
    assert row["tavily_key_id"] == 1


async def test_failover_on_429_then_success(executor, state, fake_tavily):
    fake_tavily.queue(
        "tvly-test-key-a", TavilyError(429, "excessive requests")
    )
    fake_tavily.default_response = SEARCH_OK
    data = await executor.run("search", {"query": "hello"})
    assert data["answer"] == "42"
    assert state.pool.get(1).effective_status == "cooling"
    assert len(fake_tavily.calls) == 2


async def test_exhaustion_on_432(executor, state, fake_tavily):
    fake_tavily.queue("tvly-test-key-a", TavilyError(432, "usage limit exceeded"))
    fake_tavily.default_response = SEARCH_OK
    await executor.run("search", {"query": "hello"})
    assert state.pool.get(1).effective_status == "exhausted"


async def test_invalid_key_disabled(executor, state, fake_tavily):
    fake_tavily.queue("tvly-test-key-a", TavilyError(401, "invalid api key"))
    fake_tavily.default_response = SEARCH_OK
    await executor.run("search", {"query": "hello"})
    assert state.pool.get(1).effective_status == "disabled"


async def test_client_error_passes_through_without_failover(executor, state, fake_tavily):
    fake_tavily.queue("tvly-test-key-a", TavilyError(400, "invalid query depth"))
    with pytest.raises(ToolError) as exc:
        await executor.run("search", {"query": "hello"})
    assert "400" in str(exc.value)
    # Exactly one upstream call: a bad request would fail on every key.
    assert len(fake_tavily.calls) == 1


async def test_all_keys_down_raises_pool_exhausted(executor, state, fake_tavily):
    for ks in state.pool.snapshot():
        await state.pool.report_rate_limited(ks)
    with pytest.raises(ToolError) as exc:
        await executor.run("search", {"query": "hello"})
    assert "unavailable" in str(exc.value)
    assert fake_tavily.calls == []


async def test_retry_cap_limits_attempts(executor, state, fake_tavily):
    # 3 keys but cap is 4; make every key return 429 forever.
    fake_tavily.default_response = TavilyError(429, "excessive requests")
    with pytest.raises(ToolError) as exc:
        await executor.run("search", {"query": "hello"})
    assert len(fake_tavily.calls) == 3  # one attempt per key
    assert "failed" in str(exc.value)
