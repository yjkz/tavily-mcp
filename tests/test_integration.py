"""End-to-end integration: real server thread + real MCP client + fake Tavily.

Covers: unauthenticated rejection, token auth, tool call through the pool,
failover, tier gate, admin login/token issuance, and the key test button.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from app.admin_api import build_admin_routes
from app.db import Database
from app.mcp_server import QueryTokenAuthMiddleware, build_mcp
from app.pool import KeyPool
from app.state import AppState, set_state
from app.tavily import TavilyError
from app.tasks import start_background_tasks
from tests.conftest import RESEARCH_DONE, RESEARCH_PENDING, SEARCH_OK, USAGE_OK


class ServerHandle:
    def __init__(self, app, host="127.0.0.1"):
        import uvicorn

        config = uvicorn.Config(app, host=host, port=0, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and not self.server.started:
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("server failed to start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base = f"http://{host}:{self.port}"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


@contextlib.asynccontextmanager
async def running_server(config, fake_tavily):
    db = Database(config.data_dir / "itest.db")
    pool = KeyPool(db, cooldown_seconds=config.cooldown_seconds)
    state = AppState(config=config, db=db, pool=pool, tavily=fake_tavily)
    set_state(state)

    @contextlib.asynccontextmanager
    async def lifespan(server):
        await db.connect()
        await pool.add_keys(
            [
                ("tvly-test-key-aaaa1111", "key-A", 1000.0),
                ("tvly-test-key-bbbb2222", "key-B", 1000.0),
                ("tvly-test-key-cccc3333", "key-C", 1000.0),
            ]
        )
        tasks = start_background_tasks(state)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await db.close()

    mcp = build_mcp(state, lifespan=lifespan)
    app = mcp.http_app(path="/mcp")
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    app.routes.append(Route("/health", lambda r: JSONResponse({"status": "ok"}), methods=["GET"]))
    app.routes.extend(build_admin_routes(config))

    # Same wrapping as app.main.create_app: outermost ?token= promotion.
    asgi_app = QueryTokenAuthMiddleware(app)
    handle = ServerHandle(asgi_app)
    try:
        yield handle, state
    finally:
        handle.stop()


@pytest.fixture
def usage_first_tavily(fake_tavily):
    """Background usage sync runs at startup; give it an answer."""
    fake_tavily.default_response = USAGE_OK
    return fake_tavily


async def _create_token_via_admin(base: str, password: str = "test-admin-pw") -> str:
    async with httpx.AsyncClient(base_url=base) as client:
        resp = await client.post("/api/login", json={"password": password})
        assert resp.status_code == 200, resp.text
        resp = await client.post("/api/tokens", json={"name": "itest", "tier": "standard"})
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]


async def test_unauthenticated_mcp_is_rejected(config, usage_first_tavily):
    async with running_server(config, usage_first_tavily) as (handle, _):
        transport = StreamableHttpTransport(url=f"{handle.base}/mcp")
        with pytest.raises(Exception):
            async with Client(transport) as client:
                await client.list_tools()


async def test_mcp_via_url_query_token(config, usage_first_tavily):
    """Tavily-style URL auth: /mcp?token=tpm_... works without headers."""
    async with running_server(config, usage_first_tavily) as (handle, state):
        await state.pool.add_keys([("tvly-itest-url-key-4444", "url", 1000.0)])
        token = await _create_token_via_admin(handle.base)
        usage_first_tavily.responses.clear()
        usage_first_tavily.default_response = SEARCH_OK

        transport = StreamableHttpTransport(url=f"{handle.base}/mcp?token={token}")
        async with Client(transport) as client:
            result = await client.call_tool("tavily_search", {"params": {"query": "hi"}})
            assert "42" in result.content[0].text

        # A bogus query token is still rejected.
        bad = StreamableHttpTransport(url=f"{handle.base}/mcp?token=tpm_bogus")
        with pytest.raises(Exception):
            async with Client(bad) as client:
                await client.list_tools()


async def test_full_flow_auth_search_failover(config, usage_first_tavily):
    async with running_server(config, usage_first_tavily) as (handle, state):
        await state.pool.add_keys(
            [("tvly-itest-key-zzz9999", "extra", 1000.0)]
        )
        token = await _create_token_via_admin(handle.base)

        transport = StreamableHttpTransport(
            url=f"{handle.base}/mcp",
            auth=token,
        )
        async with Client(transport) as client:
            tools = await client.list_tools()
            assert {t.name for t in tools} >= {
                "tavily_search", "tavily_extract", "tavily_crawl", "tavily_map",
                "tavily_research", "get_my_usage",
            }

            # Script: key1 exhausted -> failover to another key succeeds.
            usage_first_tavily.responses.clear()
            usage_first_tavily.queue("tvly-test-key-aaaa1111", TavilyError(432, "usage limit exceeded"))
            usage_first_tavily.default_response = SEARCH_OK

            result = await client.call_tool("tavily_search", {"params": {"query": "hello"}})
            text = result.content[0].text
            assert "42" in text
            assert "https://example.com/1" in text
            assert state.pool.get(1).effective_status == "exhausted"

            # Tier gate: crawl is full-tier only.
            with pytest.raises(ToolError) as exc:
                await client.call_tool("tavily_crawl", {"params": {"url": "https://example.com"}})
            assert "full-tier" in str(exc.value)

            usage = await client.call_tool("get_my_usage", {})
            assert "requests_today" in usage.content[0].text


async def test_rate_limit_kicks_in(config, usage_first_tavily):
    async with running_server(config, usage_first_tavily) as (handle, state):
        async with httpx.AsyncClient(base_url=handle.base) as client:
            await client.post("/api/login", json={"password": "test-admin-pw"})
            resp = await client.post(
                "/api/tokens", json={"name": "fast", "tier": "standard", "rpm_limit": 2}
            )
            token = resp.json()["token"]

        usage_first_tavily.responses.clear()
        usage_first_tavily.default_response = SEARCH_OK
        transport = StreamableHttpTransport(url=f"{handle.base}/mcp", auth=token)
        async with Client(transport) as client:
            for _ in range(2):
                await client.call_tool("tavily_search", {"params": {"query": "x"}})
            with pytest.raises(ToolError) as exc:
                await client.call_tool("tavily_search", {"params": {"query": "x"}})
            assert "Rate limit" in str(exc.value)


async def test_research_end_to_end(config, usage_first_tavily, monkeypatch):
    """Full-tier token runs research through submit+poll; standard tier is gated."""
    import app.mcp_server as m

    monkeypatch.setattr(m, "RESEARCH_POLL_INTERVAL", 0.01)
    async with running_server(config, usage_first_tavily) as (handle, state):
        async with httpx.AsyncClient(base_url=handle.base) as client:
            await client.post("/api/login", json={"password": "test-admin-pw"})
            resp = await client.post(
                "/api/tokens", json={"name": "researcher", "tier": "full"}
            )
            full_token = resp.json()["token"]
            resp = await client.post(
                "/api/tokens", json={"name": "basic", "tier": "standard"}
            )
            std_token = resp.json()["token"]

        usage_first_tavily.responses.clear()
        usage_first_tavily.queue("tvly-test-key-aaaa1111", RESEARCH_PENDING)
        usage_first_tavily.default_response = RESEARCH_DONE

        transport = StreamableHttpTransport(url=f"{handle.base}/mcp?token={full_token}")
        async with Client(transport) as client:
            result = await client.call_tool(
                "tavily_research", {"params": {"input": "test topic"}}
            )
            text = result.content[0].text
            assert "Research Report" in text
            assert "https://example.com/1" in text

        # The task was submitted and polled on the same key.
        research_calls = [c for c in usage_first_tavily.calls if "research" in c[1]]
        assert research_calls[0] == ("tvly-test-key-aaaa1111", "/research")
        assert research_calls[-1] == ("tvly-test-key-aaaa1111", "/research/req_research_1")

        # Standard tier is rejected by the tier gate before any upstream call.
        calls_before = len(usage_first_tavily.calls)
        transport = StreamableHttpTransport(url=f"{handle.base}/mcp?token={std_token}")
        async with Client(transport) as client:
            with pytest.raises(ToolError) as exc:
                await client.call_tool(
                    "tavily_research", {"params": {"input": "test topic"}}
                )
            assert "full-tier" in str(exc.value)
        assert len(usage_first_tavily.calls) == calls_before


async def test_token_tool_allowlist(config, usage_first_tavily):
    """allowed_tools gates tools beyond the tier check; get_my_usage stays open."""
    async with running_server(config, usage_first_tavily) as (handle, state):
        async with httpx.AsyncClient(base_url=handle.base) as client:
            await client.post("/api/login", json={"password": "test-admin-pw"})
            resp = await client.post(
                "/api/tokens",
                json={"name": "restricted", "tier": "full", "allowed_tools": "tavily_search"},
            )
            token = resp.json()["token"]

        usage_first_tavily.responses.clear()
        usage_first_tavily.default_response = SEARCH_OK
        transport = StreamableHttpTransport(url=f"{handle.base}/mcp?token={token}")
        async with Client(transport) as client:
            result = await client.call_tool("tavily_search", {"params": {"query": "hi"}})
            assert "42" in result.content[0].text
            # extract is not in the allowlist -> denied before any upstream call.
            calls_before = len(usage_first_tavily.calls)
            with pytest.raises(ToolError) as exc:
                await client.call_tool(
                    "tavily_extract", {"params": {"urls": ["https://example.com"]}}
                )
            assert "not enabled" in str(exc.value)
            assert len(usage_first_tavily.calls) == calls_before
            # Self-inspection is always allowed.
            usage = await client.call_tool("get_my_usage", {})
            assert "requests_today" in usage.content[0].text


async def test_sync_all_endpoint(config, usage_first_tavily):
    async with running_server(config, usage_first_tavily) as (handle, state):
        async with httpx.AsyncClient(base_url=handle.base) as client:
            await client.post("/api/login", json={"password": "test-admin-pw"})
            r = await client.post("/api/keys/sync-all")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] == 3
            assert body["failed"] == 0
            # Calibration from USAGE_OK is reflected in the pool.
            for ks in state.pool.snapshot():
                assert ks.credits_used_month == 150.0


async def test_admin_key_test_button(config, usage_first_tavily):
    async with running_server(config, usage_first_tavily) as (handle, state):
        usage_first_tavily.responses.clear()
        usage_first_tavily.default_response = USAGE_OK
        async with httpx.AsyncClient(base_url=handle.base) as client:
            await client.post("/api/login", json={"password": "test-admin-pw"})
            resp = await client.post("/api/keys/1/test")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["ok"] is True
            assert body["calibrated"] is True
            assert body["credits_used"] == 150.0
            assert body["remaining"] == 850.0
            # Calibration is reflected in the pool.
            assert state.pool.get(1).credits_used_month == 150.0

            # A broken key reports failure and gets disabled.
            usage_first_tavily.queue("tvly-test-key-bbbb2222", TavilyError(401, "invalid"))
            resp = await client.post("/api/keys/2/test")
            assert resp.json()["ok"] is False
            assert resp.json()["status"] == 401
            assert state.pool.get(2).effective_status == "disabled"


async def test_token_reveal(config, usage_first_tavily):
    async with running_server(config, usage_first_tavily) as (handle, state):
        async with httpx.AsyncClient(base_url=handle.base) as client:
            await client.post("/api/login", json={"password": "test-admin-pw"})
            r = await client.post("/api/tokens", json={"name": "export"})
            token, token_id = r.json()["token"], r.json()["id"]
            r = await client.get(f"/api/tokens/{token_id}/reveal")
            assert r.status_code == 200
            assert r.json()["token"] == token

            # Legacy row created before the export feature has no stored ciphertext.
            legacy_id = await state.db.execute(
                "INSERT INTO access_tokens (name, token_hash, prefix, tier, rpm_limit, "
                "is_active, created_at) VALUES (?,?,?,?,?,1,?)",
                ("legacy", "x" * 64, "tpm_legacy", "standard", 30, time.time()),
            )
            r = await client.get(f"/api/tokens/{legacy_id}/reveal")
            assert r.json()["token"] is None
            assert "重新创建" in r.json()["reason"]


async def test_settings_flow(config, usage_first_tavily):
    async with running_server(config, usage_first_tavily) as (handle, state):
        async with httpx.AsyncClient(base_url=handle.base) as client:
            # Public info has sensible defaults and needs no session.
            r = await client.get("/api/public-info")
            assert r.json()["site_name"] == "Tavily Pool"
            assert r.json()["announcement"] is None
            # Settings changes require an admin session.
            r = await client.put("/api/settings", json={"site_name": "X"})
            assert r.status_code == 401

            await client.post("/api/login", json={"password": "test-admin-pw"})
            r = await client.put(
                "/api/settings", json={"site_name": "我的网关", "announcement": "今晚维护"}
            )
            assert r.status_code == 200
            r = await client.get("/api/public-info")
            body = r.json()
            assert body["site_name"] == "我的网关"
            assert body["announcement"] == "今晚维护"
            assert body["announcement_updated_at"] is not None

            # Icon upload / serve / reset.
            png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
            r = await client.post(
                "/api/settings/icon", content=png, headers={"Content-Type": "image/png"}
            )
            assert r.status_code == 200
            r = await client.get("/site-icon")
            assert r.status_code == 200
            assert r.content == png
            assert r.headers["content-type"].startswith("image/png")
            assert (await client.get("/api/settings")).json()["has_custom_icon"] is True
            r = await client.delete("/api/settings/icon")
            assert r.status_code == 200
            assert (await client.get("/api/settings")).json()["has_custom_icon"] is False

            # Password change: wrong current rejected, old password invalidated.
            r = await client.post(
                "/api/settings/password",
                json={"current_password": "wrong", "new_password": "new-password-123"},
            )
            assert r.status_code == 401
            r = await client.post(
                "/api/settings/password",
                json={"current_password": "test-admin-pw", "new_password": "new-password-123"},
            )
            assert r.status_code == 200
            r = await client.post(
                "/api/settings/password",
                json={"current_password": "test-admin-pw", "new_password": "another-456"},
            )
            assert r.status_code == 401

        async with httpx.AsyncClient(base_url=handle.base) as client2:
            r = await client2.post("/api/login", json={"password": "new-password-123"})
            assert r.status_code == 200
            r = await client2.post("/api/login", json={"password": "test-admin-pw"})
            assert r.status_code == 401


async def test_logs_have_client_ip(config, usage_first_tavily):
    async with running_server(config, usage_first_tavily) as (handle, state):
        await state.pool.add_keys([("tvily-itest-ip-key-9999", "ip", 1000.0)])
        usage_first_tavily.responses.clear()
        usage_first_tavily.default_response = SEARCH_OK
        async with httpx.AsyncClient(base_url=handle.base) as client:
            await client.post("/api/login", json={"password": "test-admin-pw"})
            r = await client.post("/api/tokens", json={"name": "ip-check"})
            token = r.json()["token"]

        transport = StreamableHttpTransport(url=f"{handle.base}/mcp?token={token}")
        async with Client(transport) as client:
            await client.call_tool("tavily_search", {"params": {"query": "ip"}})

        async with httpx.AsyncClient(base_url=handle.base) as client:
            await client.post("/api/login", json={"password": "test-admin-pw"})
            r = await client.get("/api/logs?limit=5")
            items = r.json()["items"]
            assert items
            assert items[0]["client_ip"] == "127.0.0.1"


async def test_admin_requires_session(config, usage_first_tavily):
    async with running_server(config, usage_first_tavily) as (handle, _):
        async with httpx.AsyncClient(base_url=handle.base) as client:
            resp = await client.get("/api/keys")
            assert resp.status_code == 401
            resp = await client.post("/api/login", json={"password": "wrong"})
            assert resp.status_code == 401
            resp = await client.post("/api/login", json={"password": "test-admin-pw"})
            assert resp.status_code == 200
            resp = await client.get("/api/keys")
            assert resp.status_code == 200
            resp = await client.get("/api/overview")
            assert resp.status_code == 200
            body = resp.json()
            assert body["keys_total"] == 3
