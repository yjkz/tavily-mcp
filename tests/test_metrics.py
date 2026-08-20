"""Prometheus /metrics endpoint unit tests."""

from __future__ import annotations

from app.metrics import metrics_response
from app.mcp_server import log_request


async def test_metrics_exposition(state):
    await log_request(
        state.db, token_id=None, tool="tavily_search", status="success",
        credits=2.0, query="hello",
    )
    await log_request(
        state.db, token_id=None, tool="tavily_extract", status="rate_limited",
    )
    resp = await metrics_response(state)
    assert resp.status_code == 200
    body = resp.body.decode("utf-8")
    assert "tpm_requests_total" in body
    assert 'tool="tavily_search"' in body
    assert 'status="rate_limited"' in body
    # Counters are process-global and accumulate across tests; just check the
    # credits counter exists and includes this test's contribution.
    credits_line = next(
        (ln for ln in body.splitlines() if ln.startswith("tpm_credits_total ")), None
    )
    assert credits_line is not None
    assert float(credits_line.split()[-1]) >= 2.0
    assert "tpm_pool_keys" in body
    assert "tpm_pool_remaining_credits" in body
    assert "tpm_tokens_active" in body
