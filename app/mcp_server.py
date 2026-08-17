"""FastMCP server: pooled Tavily tools, token auth, and per-token policy.

Flow for every tools/call request:
    HTTP bearer token -> DbTokenVerifier (SQLite hash lookup)
    -> GatewayMiddleware.on_call_tool (RPM / tier / daily quota / monthly credits)
    -> tool -> execute_pooled (round-robin key + failover + request log)
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict, deque
from typing import Any, Optional
from urllib.parse import parse_qs

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from pydantic import BaseModel, Field
from typing_extensions import Literal

from .db import Database
from .pool import KeyPool, day_start_ts, month_start_ts
from .state import AppState
from .tavily import TavilyClient, TavilyError, parse_usage

TOKEN_PREFIX = "tpm_"
FULL_TIER_TOOLS = {"tavily_crawl", "tavily_map"}

READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


# ---------------------------------------------------------------------------
# Auth: opaque gateway tokens issued by the dashboard, stored as SHA-256.
# ---------------------------------------------------------------------------


class QueryTokenAuthMiddleware:
    """Promote a `?token=...` query parameter to an Authorization header.

    Lets MCP clients connect with just a URL (same pattern as Tavily's hosted
    MCP, https://mcp.tavily.com/mcp/?tavilyApiKey=...) instead of custom
    headers: https://your-domain/mcp?token=tpm_xxx
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            query = (scope.get("query_string") or b"").decode("latin-1")
            token = parse_qs(query).get("token", [None])[0]
            if token:
                headers = list(scope.get("headers") or [])
                if not any(name.lower() == b"authorization" for name, _ in headers):
                    headers.append((b"authorization", f"Bearer {token}".encode("latin-1")))
                    scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


class DbTokenVerifier(TokenVerifier):
    """Validates Bearer tokens by SHA-256 hash lookup against access_tokens."""

    def __init__(self, db: Database):
        super().__init__()
        self._db = db

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        if not token.startswith(TOKEN_PREFIX):
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        row = await self._db.fetchone(
            "SELECT id, name, tier, rpm_limit, daily_quota, monthly_credits_limit, is_active "
            "FROM access_tokens WHERE token_hash = ?",
            (token_hash,),
        )
        if row is None or not row["is_active"]:
            return None
        await self._db.execute(
            "UPDATE access_tokens SET last_used_at = ? WHERE id = ?",
            (time.time(), row["id"]),
        )
        return AccessToken(
            token=token,
            client_id=str(row["id"]),
            scopes=["tavily", f"tier:{row['tier']}"],
            expires_at=None,
            claims={
                "token_id": row["id"],
                "name": row["name"],
                "tier": row["tier"],
                "rpm_limit": row["rpm_limit"],
                "daily_quota": row["daily_quota"],
                "monthly_credits_limit": row["monthly_credits_limit"],
            },
        )


# ---------------------------------------------------------------------------
# Policy middleware: rate limit, tier gate, quotas, and request logging.
# ---------------------------------------------------------------------------


def _day_start_ts(now: Optional[float] = None) -> float:
    return day_start_ts(now)


def get_client_ip() -> Optional[str]:
    """Best-effort client IP for request logs.

    Prefers the first hop of X-Forwarded-For (set by the trusted Nginx proxy
    in deployment);
    falls back to the socket peer. Direct spoofed XFF is not a concern for
    logging because the app port is not exposed publicly in deployment.
    """
    try:
        request = get_http_request()
    except Exception:
        return None
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def log_request(
    db: Database,
    *,
    token_id: Optional[int],
    tool: str,
    status: str,
    tavily_key_id: Optional[int] = None,
    http_status: Optional[int] = None,
    credits: float = 0.0,
    latency_ms: Optional[int] = None,
    error_detail: Optional[str] = None,
    request_id: Optional[str] = None,
    ts: Optional[float] = None,
    client_ip: Optional[str] = None,
) -> None:
    await db.execute(
        "INSERT INTO request_logs (ts, token_id, tool, tavily_key_id, status, http_status, "
        "credits, latency_ms, error_detail, request_id, client_ip) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            ts if ts is not None else time.time(),
            token_id,
            tool,
            tavily_key_id,
            status,
            http_status,
            credits,
            latency_ms,
            error_detail,
            request_id,
            client_ip,
        ),
    )


class RateLimiter:
    """Sliding-window per-token limiter (requests per minute)."""

    def __init__(self) -> None:
        self._windows: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, token_id: int, limit: int) -> bool:
        now = time.monotonic()
        window = self._windows[token_id]
        while window and window[0] <= now - 60.0:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True


class GatewayMiddleware(Middleware):
    def __init__(self, db: Database):
        super().__init__()
        self._db = db
        self._limiter = RateLimiter()

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        tool = context.message.name
        access = get_access_token()
        client_ip = get_client_ip()
        if access is None:
            # HTTP transports always authenticate before reaching tools; this
            # branch only happens for in-process (stdio/memory) usage.
            return await call_next(context)

        claims = access.claims or {}
        token_id = claims.get("token_id")

        # Tier gate: crawl/map are expensive, full-tier tokens only.
        if tool in FULL_TIER_TOOLS and claims.get("tier") != "full":
            await log_request(
                self._db, token_id=token_id, tool=tool, status="tier_denied", client_ip=client_ip,
                error_detail=f"'{tool}' requires a full-tier token",
            )
            raise ToolError(
                f"Access denied: '{tool}' consumes many credits and requires a "
                "full-tier token. Ask the gateway administrator or use "
                "tavily_search / tavily_extract instead."
            )

        # Requests-per-minute limit.
        rpm_limit = int(claims.get("rpm_limit") or 30)
        if not self._limiter.allow(token_id, rpm_limit):
            await log_request(
                self._db, token_id=token_id, tool=tool, status="rate_limited", client_ip=client_ip,
                error_detail=f"exceeded {rpm_limit} req/min",
            )
            raise ToolError(
                f"Rate limit exceeded: this token allows {rpm_limit} requests per "
                "minute. Wait a moment and retry."
            )

        # Optional daily request quota.
        daily_quota = claims.get("daily_quota")
        if daily_quota is not None:
            row = await self._db.fetchone(
                "SELECT COUNT(*) AS c FROM request_logs "
                "WHERE token_id=? AND ts>=? AND status!='rate_limited'",
                (token_id, _day_start_ts()),
            )
            if row and row["c"] >= int(daily_quota):
                await log_request(
                    self._db, token_id=token_id, tool=tool, status="quota_exceeded",
                    client_ip=client_ip,
                    error_detail=f"daily quota {daily_quota} reached",
                )
                raise ToolError(
                    f"Daily quota exhausted: this token allows {daily_quota} "
                    "requests per day. Try again tomorrow."
                )

        # Optional monthly credits quota.
        monthly_limit = claims.get("monthly_credits_limit")
        if monthly_limit is not None:
            row = await self._db.fetchone(
                "SELECT TOTAL(credits) AS c FROM request_logs "
                "WHERE token_id=? AND ts>=? AND status='success'",
                (token_id, month_start_ts()),
            )
            if row and (row["c"] or 0) >= float(monthly_limit):
                await log_request(
                    self._db, token_id=token_id, tool=tool, status="quota_exceeded",
                    client_ip=client_ip,
                    error_detail=f"monthly credits limit {monthly_limit} reached",
                )
                raise ToolError(
                    f"Monthly credits quota exhausted: this token allows "
                    f"{monthly_limit} credits per month."
                )

        return await call_next(context)


# ---------------------------------------------------------------------------
# Pooled execution with failover.
# ---------------------------------------------------------------------------


class PooledExecutor:
    def __init__(self, state: AppState):
        self.state = state
        self.pool: KeyPool = state.pool
        self.tavily: TavilyClient = state.tavily
        self.db: Database = state.db

    def _endpoint_call(self, endpoint: str):
        mapping = {
            "search": self.tavily.search,
            "extract": self.tavily.extract,
            "crawl": self.tavily.crawl,
            "map": self.tavily.map_url,
        }
        try:
            return mapping[endpoint]
        except KeyError as e:
            raise ValueError(f"unknown endpoint: {endpoint}") from e

    async def run(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Call a Tavily endpoint through the key pool with automatic failover."""
        access = get_access_token()
        token_id = (access.claims or {}).get("token_id") if access else None
        client_ip = get_client_ip()
        tool = f"tavily_{endpoint}"
        call = self._endpoint_call(endpoint)

        max_attempts = max(1, min(len(self.pool), self.state.config.max_retries))
        started = time.perf_counter()
        attempts = 0
        reasons: list[str] = []
        last_key_id: Optional[int] = None

        while attempts < max_attempts:
            ks = await self.pool.acquire()
            if ks is None:
                break
            attempts += 1
            last_key_id = ks.id
            t0 = time.perf_counter()
            try:
                data = await call(ks.key, payload)
            except TavilyError as e:
                reasons.append(f"HTTP {e.status}: {e.detail}")
                if e.status == 429:
                    await self.pool.report_rate_limited(ks)
                    continue
                if e.status in (432, 433):
                    await self.pool.report_exhausted(ks, f"HTTP {e.status}: {e.detail}")
                    continue
                if e.status == 401:
                    await self.pool.report_invalid(ks, f"401: {e.detail}")
                    continue
                if e.status >= 500 or e.status == 0:
                    await self.pool.report_transient(ks, f"HTTP {e.status}: {e.detail}")
                    continue
                # 4xx (bad request): every key would fail identically.
                await self.pool.report_transient(ks, f"HTTP {e.status}: {e.detail}")
                await log_request(
                    self.db, token_id=token_id, tool=tool, status="upstream_error",
                    client_ip=client_ip,
                    tavily_key_id=ks.id, http_status=e.status,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error_detail=f"Tavily rejected the request: {e.detail}",
                )
                raise ToolError(
                    f"Tavily rejected the request (HTTP {e.status}): {e.detail}. "
                    "Fix the tool arguments and retry."
                ) from e

            latency_ms = int((time.perf_counter() - t0) * 1000)
            credits = 0.0
            usage = data.get("usage") or {}
            if isinstance(usage, dict) and isinstance(usage.get("credits"), (int, float)):
                credits = float(usage["credits"])
            await self.pool.report_success(ks, credits)
            await log_request(
                self.db, token_id=token_id, tool=tool, status="success",
                client_ip=client_ip,
                tavily_key_id=ks.id, http_status=200, credits=credits,
                latency_ms=latency_ms, request_id=data.get("request_id"),
            )
            return data

        total_ms = int((time.perf_counter() - started) * 1000)
        if attempts == 0:
            await log_request(
                self.db, token_id=token_id, tool=tool, status="pool_exhausted",
                client_ip=client_ip,
                latency_ms=total_ms,
                error_detail="no usable key in pool",
            )
            raise ToolError(
                "All Tavily keys are currently unavailable (cooling, out of "
                f"quota, or disabled). {self.pool.next_recovery_hint()}. Retry later."
            )
        await log_request(
            self.db, token_id=token_id, tool=tool, status="upstream_error",
            client_ip=client_ip,
            tavily_key_id=last_key_id, latency_ms=total_ms,
            error_detail=f"{attempts} attempts failed: " + "; ".join(reasons[-3:]),
        )
        raise ToolError(
            f"All Tavily keys failed after {attempts} attempts "
            f"({'; '.join(reasons[-3:])}). Retry later."
        )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class SearchInput(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        max_length=400,
        description="The search query, e.g. 'what is model context protocol'",
    )
    search_depth: Literal["basic", "advanced"] = Field(
        "basic", description="'advanced' is slower but returns more relevant content (2 credits)"
    )
    topic: Literal["general", "news", "finance"] = Field(
        "general", description="Category to optimize results for"
    )
    max_results: int = Field(5, ge=1, le=20, description="Number of results (1-20)")
    days: int | None = Field(
        None, ge=1, le=365,
        description="Only for topic='news': include results from the last N days",
    )
    time_range: Literal["day", "week", "month", "year"] | None = Field(
        None, description="Restrict results to a time range"
    )
    include_answer: bool = Field(
        True, description="Include a short LLM-generated answer summarizing the results"
    )
    include_raw_content: bool = Field(
        False, description="Include the cleaned raw HTML content of each page (large!)"
    )
    include_domains: list[str] | None = Field(
        None, max_length=20, description="Restrict search to these domains"
    )
    exclude_domains: list[str] | None = Field(
        None, max_length=20, description="Exclude these domains from search"
    )

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "query": self.query,
            "search_depth": self.search_depth,
            "topic": self.topic,
            "max_results": self.max_results,
            "include_answer": self.include_answer,
            "include_raw_content": self.include_raw_content,
            "include_usage": True,
        }
        optional = {
            "days": self.days,
            "time_range": self.time_range,
            "include_domains": self.include_domains,
            "exclude_domains": self.exclude_domains,
        }
        data.update({k: v for k, v in optional.items() if v is not None})
        return data


class ExtractInput(BaseModel):
    urls: list[str] = Field(
        ..., min_length=1, max_length=20,
        description="URLs to extract content from (1-20)",
    )
    extract_depth: Literal["basic", "advanced"] = Field(
        "basic", description="'advanced' does deeper extraction (2 credits per URL)"
    )
    format: Literal["markdown", "text"] = Field("markdown", description="Output format")

    def payload(self) -> dict[str, Any]:
        return {
            "urls": self.urls,
            "extract_depth": self.extract_depth,
            "format": self.format,
            "include_usage": True,
        }


class CrawlInput(BaseModel):
    url: str = Field(..., min_length=8, description="Starting URL of the website to crawl")
    max_depth: int = Field(2, ge=1, le=5, description="Crawl depth from the starting URL")
    limit: int = Field(25, ge=1, le=100, description="Maximum number of pages to return")
    instructions: str | None = Field(
        None, max_length=500,
        description="Optional natural-language guidance for which pages to keep",
    )

    def payload(self) -> dict[str, Any]:
        data = {
            "url": self.url,
            "max_depth": self.max_depth,
            "limit": self.limit,
            "include_usage": True,
        }
        if self.instructions:
            data["instructions"] = self.instructions
        return data


class MapInput(BaseModel):
    url: str = Field(..., min_length=8, description="Starting URL of the website to map")
    max_depth: int = Field(2, ge=1, le=5, description="Traversal depth from the starting URL")
    limit: int = Field(50, ge=1, le=500, description="Maximum number of URLs to return")

    def payload(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "max_depth": self.max_depth,
            "limit": self.limit,
            "include_usage": True,
        }


def clamp_output(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n\n[... OUTPUT TRUNCATED at {limit} characters. Use narrower "
        "parameters (fewer results, include_raw_content=False, smaller limit) "
        "to get complete data.]"
    )


def format_search(data: dict[str, Any], include_raw: bool) -> str:
    lines: list[str] = []
    answer = data.get("answer")
    if answer:
        lines.append(f"Answer: {answer}")
        lines.append("")
    for i, r in enumerate(data.get("results", []), 1):
        lines.append(f"## {i}. {r.get('title', '(untitled)')}")
        lines.append(f"URL: {r.get('url', '')}")
        score = r.get("score")
        if score is not None:
            lines.append(f"Relevance: {float(score):.2f}")
        published = r.get("published_date")
        if published:
            lines.append(f"Published: {published}")
        content = r.get("content") or ""
        lines.append(content.strip())
        if include_raw and r.get("raw_content"):
            lines.append("Raw content:\n" + r["raw_content"].strip())
        lines.append("")
    return "\n".join(lines).strip() or "No results found."


def format_extract(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for r in data.get("results", []):
        lines.append(f"## {r.get('url', '(unknown url)')}")
        lines.append((r.get("raw_content") or "").strip())
        lines.append("")
    failed = data.get("failed_results") or []
    for r in failed:
        lines.append(f"[FAILED] {r.get('url', '')}: {r.get('error', 'unknown error')}")
    return "\n".join(lines).strip() or "No content extracted."


def format_crawl(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for r in data.get("results", []):
        trail = " > ".join(r.get("breadcrumb", []) or [])
        header = r.get("url", "")
        if trail:
            header = f"{trail}\n{header}"
        lines.append(f"## {header}")
        content = (r.get("raw_content") or "").strip()
        lines.append(content[:2000] + ("..." if len(content) > 2000 else ""))
        lines.append("")
    return "\n".join(lines).strip() or "No pages crawled."


def format_map(data: dict[str, Any]) -> str:
    urls = data.get("results", [])
    return "\n".join(urls) if urls else "No URLs found."


def build_mcp(state: AppState, lifespan: Any = None) -> FastMCP:
    verifier = DbTokenVerifier(state.db)
    mcp = FastMCP(
        name="tavily_pool_mcp",
        lifespan=lifespan,
        instructions=(
            "Tavily web-search gateway backed by a key pool. Use tavily_search "
            "for web searches, tavily_extract to read specific URLs, "
            "tavily_crawl / tavily_map (full-tier tokens only) for site-wide "
            "operations, and get_my_usage to check your own quota."
        ),
        auth=verifier,
        middleware=[GatewayMiddleware(state.db)],
    )
    executor = PooledExecutor(state)
    limit = state.config.character_limit

    @mcp.tool(name="tavily_search", annotations={**READ_ONLY_ANNOTATIONS, "title": "Tavily Search"})
    async def tavily_search(params: SearchInput) -> str:
        """Search the web with Tavily and return ranked results.

        Use this as the default web-search tool. Returns an optional summary
        answer plus a list of results with title, URL, relevance score and a
        content snippet for each.

        Args:
            params: Query and options. search_depth='advanced' and
                topic='news' (with days=N) are useful refinements.

        Returns:
            Markdown: 'Answer:' summary (if any) then numbered results.

        Errors:
            - Fix arguments when told the request was rejected (HTTP 4xx).
            - Retry later when rate-limited or when the key pool is cooling down.
        """
        data = await executor.run("search", params.payload())
        return clamp_output(format_search(data, params.include_raw_content), limit)

    @mcp.tool(name="tavily_extract", annotations={**READ_ONLY_ANNOTATIONS, "title": "Tavily Extract"})
    async def tavily_extract(params: ExtractInput) -> str:
        """Extract clean content from up to 20 specific URLs.

        Prefer this over search when you already know the exact URLs to read.

        Args:
            params: URLs plus extract_depth ('advanced' = better but 2 credits/URL).

        Returns:
            Markdown: one section per URL with its cleaned content.

        Errors:
            Individual unreachable URLs appear as [FAILED] entries; check spelling.
        """
        data = await executor.run("extract", params.payload())
        return clamp_output(format_extract(data), limit)

    @mcp.tool(name="tavily_crawl", annotations={**READ_ONLY_ANNOTATIONS, "title": "Tavily Crawl"})
    async def tavily_crawl(params: CrawlInput) -> str:
        """Crawl an entire website starting from a URL (full-tier tokens only).

        Expensive: consumes ~1 credit per 5 pages. Use tavily_map first if you
        only need the site structure.

        Args:
            params: Start URL, max_depth (1-5), limit (1-100), optional
                natural-language instructions to filter pages.

        Returns:
            Markdown: pages grouped by breadcrumb path with truncated content.
        """
        data = await executor.run("crawl", params.payload())
        return clamp_output(format_crawl(data), limit)

    @mcp.tool(name="tavily_map", annotations={**READ_ONLY_ANNOTATIONS, "title": "Tavily Map"})
    async def tavily_map(params: MapInput) -> str:
        """Map all URLs of a website starting from a URL (full-tier tokens only).

        Cheap structure discovery (~1 credit per 50 URLs). Use before
        tavily_crawl or tavily_extract to pick interesting pages.

        Args:
            params: Start URL, max_depth (1-5), limit (1-500).

        Returns:
            Plain list of discovered URLs, one per line.
        """
        data = await executor.run("map", params.payload())
        return clamp_output(format_map(data), limit)

    @mcp.tool(name="get_my_usage", annotations={
        **READ_ONLY_ANNOTATIONS, "title": "Get My Usage", "openWorldHint": False,
    })
    async def get_my_usage() -> str:
        """Return your access token's usage: requests today, credits this month,
        and the limits configured for this token."""
        access = get_access_token()
        if access is None:
            return json.dumps({"error": "no token context"})
        token_id = (access.claims or {}).get("token_id")
        row = await state.db.fetchone(
            "SELECT name, tier, rpm_limit, daily_quota, monthly_credits_limit "
            "FROM access_tokens WHERE id = ?",
            (token_id,),
        )
        if row is None:
            return json.dumps({"error": "token not found"})
        today_row = await state.db.fetchone(
            "SELECT COUNT(*) AS c FROM request_logs "
            "WHERE token_id=? AND ts>=? AND status!='rate_limited'",
            (token_id, _day_start_ts()),
        )
        month_row = await state.db.fetchone(
            "SELECT COUNT(*) AS c, TOTAL(credits) AS credits FROM request_logs "
            "WHERE token_id=? AND ts>=? AND status='success'",
            (token_id, month_start_ts()),
        )
        announcement = await state.db.get_setting("announcement") or ""
        site_name = await state.db.get_setting("site_name") or "Tavily Pool"
        return json.dumps(
            {
                "site_name": site_name,
                "announcement": announcement or None,
                "token_name": row["name"],
                "tier": row["tier"],
                "requests_today": today_row["c"] if today_row else 0,
                "daily_quota": row["daily_quota"],
                "requests_this_month": month_row["c"] if month_row else 0,
                "credits_this_month": round(month_row["credits"] or 0.0, 2) if month_row else 0.0,
                "monthly_credits_limit": row["monthly_credits_limit"],
                "rpm_limit": row["rpm_limit"],
            },
            indent=2,
            ensure_ascii=False,
        )

    return mcp
