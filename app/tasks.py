"""Background maintenance: Tavily usage calibration and log retention."""

from __future__ import annotations

import asyncio
import json
import logging

from .pool import KeyPool
from .state import AppState
from .tavily import TavilyClient, TavilyError, parse_usage

logger = logging.getLogger("tavily_pool.tasks")


async def sync_all_keys(state: AppState) -> dict[str, int]:
    """Poll GET /usage for every non-disabled key and calibrate local state."""
    results = {"ok": 0, "failed": 0, "recovered": 0}
    for ks in state.pool.snapshot():
        if ks.status == "disabled":
            continue
        try:
            body = await state.tavily.usage(ks.key)
        except TavilyError as e:
            results["failed"] += 1
            detail = f"HTTP {e.status}: {e.detail}"
            if e.status == 401:
                await state.pool.report_invalid(ks, detail)
            logger.warning("usage sync failed for key %s: %s", ks.id, detail)
            continue
        parsed = parse_usage(body)
        if parsed["credits_used"] is None:
            results["failed"] += 1
            logger.warning("usage sync got unparseable body for key %s", ks.id)
            continue
        recovered = await state.pool.apply_usage(
            ks.id,
            parsed["credits_used"],
            parsed["plan_limit"] or ks.plan_limit,
            json.dumps(body),
        )
        results["ok"] += 1
        if recovered:
            results["recovered"] += 1
    return results


async def usage_sync_loop(state: AppState, interval_hours: float) -> None:
    while True:
        try:
            results = await sync_all_keys(state)
            logger.info(
                "usage sync done: %(ok)d ok, %(failed)d failed, %(recovered)d recovered",
                results,
            )
            if state.alerts is not None:
                await state.alerts.pool_check(state.pool)
        except Exception:
            logger.exception("usage sync loop crashed; retrying next interval")
        await asyncio.sleep(max(60.0, interval_hours * 3600.0))


async def maintenance_loop(state: AppState, retention_days: int) -> None:
    """Daily: roll monthly counters, prune old request logs."""
    while True:
        try:
            resets = await state.pool.roll_month_if_needed()
            if resets:
                logger.info("monthly reset applied to %d key(s)", resets)
            cutoff_sql = (
                "DELETE FROM request_logs WHERE ts < strftime('%s', 'now', ?)"
            )
            await state.db.execute(cutoff_sql, (f"-{retention_days} days",))
            logger.info("log cleanup done (retention=%dd)", retention_days)
            if state.alerts is not None:
                await state.alerts.pool_check(state.pool)
        except Exception:
            logger.exception("maintenance loop crashed; retrying next interval")
        await asyncio.sleep(6 * 3600.0)


def start_background_tasks(state: AppState) -> list[asyncio.Task]:
    return [
        asyncio.create_task(
            usage_sync_loop(state, state.config.usage_sync_interval_hours),
            name="usage-sync",
        ),
        asyncio.create_task(
            maintenance_loop(state, state.config.log_retention_days),
            name="maintenance",
        ),
    ]


def stop_background_tasks(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    # Callers should await asyncio.gather(*tasks, return_exceptions=True).
