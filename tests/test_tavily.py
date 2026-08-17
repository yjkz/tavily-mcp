"""TavilyClient and error/usage parsing tests (upstream mocked with respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.tavily import BASE_URL, TavilyClient, TavilyError, parse_usage


@respx.mock
async def test_success_sends_bearer_and_parses_json():
    route = respx.post(f"{BASE_URL}/search").mock(
        return_value=httpx.Response(200, json={"results": [], "usage": {"credits": 1}})
    )
    client = TavilyClient()
    data = await client.search("tvly-abc", {"query": "hi"})
    assert data["usage"]["credits"] == 1
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer tvly-abc"
    await client.aclose()


@respx.mock
async def test_error_detail_from_nested_dict():
    respx.post(f"{BASE_URL}/search").mock(
        return_value=httpx.Response(
            432, json={"detail": {"error": "usage limit exceeded", "code": 432}}
        )
    )
    client = TavilyClient()
    with pytest.raises(TavilyError) as exc:
        await client.search("tvly-abc", {"query": "hi"})
    assert exc.value.status == 432
    assert "usage limit exceeded" in exc.value.detail
    await client.aclose()


@respx.mock
async def test_network_error_maps_to_status_zero():
    respx.post(f"{BASE_URL}/search").mock(side_effect=httpx.ConnectError("boom"))
    client = TavilyClient()
    with pytest.raises(TavilyError) as exc:
        await client.search("tvly-abc", {"query": "hi"})
    assert exc.value.status == 0
    await client.aclose()


@respx.mock
async def test_usage_endpoint_get():
    respx.get(f"{BASE_URL}/usage").mock(
        return_value=httpx.Response(
            200,
            json={
                "key": {"usage": 150, "limit": 1000, "search_usage": 100},
                "account": {"current_plan": "Free"},
            },
        )
    )
    client = TavilyClient()
    body = await client.usage("tvly-abc")
    parsed = parse_usage(body)
    assert parsed["credits_used"] == 150.0
    assert parsed["plan_limit"] == 1000.0
    assert parsed["remaining"] == 850.0
    assert parsed["plan"] == "Free"
    await client.aclose()


def test_parse_usage_tolerates_unknown_shape():
    parsed = parse_usage({"unexpected": True})
    assert parsed["credits_used"] is None
    assert parsed["remaining"] is None
    assert parsed["raw"] == {"unexpected": True}


def test_parse_usage_falls_back_to_account_when_key_limit_missing():
    """Real-world shape: key.usage stuck at 0 with key.limit null; the actual
    monthly quota lives on the account plan."""
    parsed = parse_usage(
        {
            "key": {"usage": 0, "limit": None, "search_usage": 0},
            "account": {
                "current_plan": "Researcher",
                "plan_usage": 83,
                "plan_limit": 1000,
                "search_usage": 74,
            },
        }
    )
    assert parsed["credits_used"] == 83.0
    assert parsed["plan_limit"] == 1000.0
    assert parsed["remaining"] == 917.0
    assert parsed["source"] == "account"
    assert parsed["plan"] == "Researcher"


def test_parse_usage_prefers_key_level_when_limit_configured():
    parsed = parse_usage(
        {"key": {"usage": 150, "limit": 500}, "account": {"plan_usage": 999, "plan_limit": 5000}}
    )
    assert parsed["credits_used"] == 150.0
    assert parsed["plan_limit"] == 500.0
    assert parsed["remaining"] == 350.0
    assert parsed["source"] == "key"
