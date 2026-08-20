"""Prometheus metrics (text exposition at /metrics).

Counters are incremented from log_request() in mcp_server; gauges are
refreshed on scrape from the in-memory pool snapshot.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from starlette.responses import Response

REQUESTS = Counter(
    "tpm_requests_total", "Gateway tool requests by tool and status", ["tool", "status"]
)
CREDITS = Counter("tpm_credits_total", "Credits consumed by successful tool calls")
POOL_KEYS = Gauge("tpm_pool_keys", "Keys in the pool by effective status", ["status"])
POOL_REMAINING = Gauge("tpm_pool_remaining_credits", "Sum of remaining credits across the pool")
TOKENS_ACTIVE = Gauge("tpm_tokens_active", "Active access tokens")


async def metrics_response(state) -> Response:
    counts = {"active": 0, "cooling": 0, "exhausted": 0, "disabled": 0}
    remaining = 0.0
    for ks in state.pool.snapshot():
        eff = ks.effective_status
        counts[eff] = counts.get(eff, 0) + 1
        remaining += ks.remaining_credits
    for status, n in counts.items():
        POOL_KEYS.labels(status=status).set(n)
    POOL_REMAINING.set(remaining)
    row = await state.db.fetchone("SELECT COUNT(*) AS c FROM access_tokens WHERE is_active = 1")
    TOKENS_ACTIVE.set(row["c"] if row else 0)
    return Response(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)
