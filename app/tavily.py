"""Async client for the Tavily API with typed errors.

All knowledge about Tavily's wire format lives here: error body parsing,
the /usage response shape, and credit accounting fields.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

BASE_URL = "https://api.tavily.com"


class TavilyError(Exception):
    """Upstream error with the HTTP status code (0 for network failures)."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"Tavily API error {status}: {detail}")


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "")[:200] or "unknown error"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("error") or detail)
    if detail:
        return str(detail)
    return str(body)[:200]


def parse_usage(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize a GET /usage response.

    Documented shape: {"key": {"usage": 150, "limit": 1000}, "account": {...}}.
    In practice Tavily only tracks key-level numbers when the key has its own
    limit configured; otherwise key.usage stays 0 / key.limit null and the
    real monthly quota lives on the account plan. Fall back accordingly.
    """

    def num(value: Any) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    key_info = body.get("key") or {}
    account = body.get("account") or {}
    used = num(key_info.get("usage"))
    limit = num(key_info.get("limit"))
    source = "key"
    if limit is None:
        account_used = num(account.get("plan_usage"))
        account_limit = num(account.get("plan_limit"))
        if account_used is not None or account_limit is not None:
            used = account_used if account_used is not None else used
            limit = account_limit if account_limit is not None else limit
            source = "account"
    remaining = (
        limit - used
        if used is not None and limit is not None and limit >= used
        else None
    )
    return {
        "credits_used": used,
        "plan_limit": limit,
        "remaining": remaining,
        "plan": account.get("current_plan"),
        "source": source,
        "raw": body,
    }


class TavilyClient:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 60.0):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        try:
            resp = await self._client.request(
                method,
                path,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.TimeoutException as e:
            raise TavilyError(0, f"upstream timeout: {type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise TavilyError(0, f"network error: {e}") from e
        if resp.status_code >= 400:
            raise TavilyError(resp.status_code, _error_detail(resp))
        try:
            return resp.json()
        except ValueError as e:
            raise TavilyError(0, "upstream returned non-JSON body") from e

    async def search(self, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/search", api_key, payload)

    async def extract(self, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/extract", api_key, payload)

    async def crawl(self, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/crawl", api_key, payload)

    async def map_url(self, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/map", api_key, payload)

    async def research(self, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit an async research task; returns 201 + {request_id, status, ...}."""
        return await self._request("POST", "/research", api_key, payload)

    async def get_research(self, api_key: str, request_id: str) -> dict[str, Any]:
        """Poll a research task; returns {status, content, sources, ...} when done."""
        return await self._request("GET", f"/research/{request_id}", api_key)

    async def usage(self, api_key: str) -> dict[str, Any]:
        return await self._request("GET", "/usage", api_key)
