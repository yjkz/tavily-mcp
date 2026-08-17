"""Round-robin Tavily key pool scheduler with SQLite-backed state.

State machine per key:
    active  --429--> cooling --(cooldown expires)--> active
    active  --432/433 or credits>=plan_limit--> exhausted
    exhausted --usage sync shows remaining quota--> active
    any     --401--> disabled (manual re-enable via dashboard)
    disabled --manual enable--> active
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .db import Database


def month_start_ts(now: Optional[float] = None) -> float:
    dt = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def day_start_ts(now: Optional[float] = None) -> float:
    dt = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def next_month_start_ts(now: Optional[float] = None) -> float:
    dt = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    if dt.month == 12:
        nxt = dt.replace(year=dt.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        nxt = dt.replace(month=dt.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()


@dataclass
class KeyState:
    id: int
    key: str
    label: str
    status: str  # active | cooling | exhausted | disabled
    cooldown_until: Optional[float]
    credits_used_month: float
    plan_limit: float
    monthly_reset_at: Optional[float]
    total_requests: int
    last_used_at: Optional[float]
    last_error: Optional[str]
    last_usage_json: Optional[str] = field(default=None, repr=False)

    @property
    def effective_status(self) -> str:
        """Status with lazily-resolved cooldown expiry and local credit ceiling."""
        now = time.time()
        if self.status == "disabled":
            return "disabled"
        if self.status == "exhausted":
            # Explicit exhaustion (upstream 432/433) stands until usage sync
            # or the monthly rollover clears it.
            return "exhausted"
        if self.status == "cooling":
            if self.cooldown_until is not None and self.cooldown_until <= now:
                return "active" if self.credits_used_month < self.plan_limit else "exhausted"
            return "cooling"
        # active
        if self.credits_used_month >= self.plan_limit:
            return "exhausted"
        return "active"

    @property
    def remaining_credits(self) -> float:
        return max(0.0, self.plan_limit - self.credits_used_month)


class KeyPool:
    def __init__(self, db: Database, cooldown_seconds: float = 60.0):
        self.db = db
        self.cooldown_seconds = cooldown_seconds
        self._keys: list[KeyState] = []
        self._cursor = 0
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def load(self) -> None:
        rows = await self.db.fetchall("SELECT * FROM tavily_keys ORDER BY id")
        async with self._lock:
            self._keys = [
                KeyState(
                    id=r["id"],
                    key=r["key"],
                    label=r["label"],
                    status=r["status"],
                    cooldown_until=r["cooldown_until"],
                    credits_used_month=r["credits_used_month"],
                    plan_limit=r["plan_limit"],
                    monthly_reset_at=r["monthly_reset_at"],
                    total_requests=r["total_requests"],
                    last_used_at=r["last_used_at"],
                    last_error=r["last_error"],
                    last_usage_json=r["last_usage_json"],
                )
                for r in rows
            ]
            self._cursor = 0

    def snapshot(self) -> list[KeyState]:
        return list(self._keys)

    def get(self, key_id: int) -> Optional[KeyState]:
        return next((k for k in self._keys if k.id == key_id), None)

    def __len__(self) -> int:
        return len(self._keys)

    # -- scheduling --------------------------------------------------------

    async def acquire(self) -> Optional[KeyState]:
        """Return the next usable key (round-robin), or None if pool is dry."""
        async with self._lock:
            n = len(self._keys)
            for i in range(n):
                idx = (self._cursor + i) % n
                ks = self._keys[idx]
                if ks.effective_status == "active":
                    self._cursor = (idx + 1) % n
                    return ks
            return None

    def next_recovery_hint(self) -> str:
        if not self._keys:
            return "no Tavily keys are configured yet (add keys in the dashboard)"
        now = time.time()
        cooling = sorted(
            ks.cooldown_until for ks in self._keys if ks.effective_status == "cooling" and ks.cooldown_until
        )
        if cooling:
            return f"earliest key cooldown ends in {max(0, int(cooling[0] - now))}s"
        return "monthly quota resets at the start of the next month"

    # -- report callbacks (called after each upstream attempt) --------------

    async def _persist(self, ks: KeyState, fields: str, params: tuple) -> None:
        await self.db.execute(
            f"UPDATE tavily_keys SET {fields} WHERE id = ?", (*params, ks.id)
        )

    async def report_success(self, ks: KeyState, credits: float) -> None:
        async with self._lock:
            ks.total_requests += 1
            ks.credits_used_month += credits
            ks.last_used_at = time.time()
            ks.last_error = None
            if ks.credits_used_month >= ks.plan_limit:
                ks.status = "exhausted"
        await self._persist(
            ks,
            "total_requests=?, credits_used_month=?, last_used_at=?, last_error=?, status=?",
            (ks.total_requests, ks.credits_used_month, ks.last_used_at, None, ks.status),
        )

    async def report_rate_limited(self, ks: KeyState) -> None:
        async with self._lock:
            ks.status = "cooling"
            ks.cooldown_until = time.time() + self.cooldown_seconds
            ks.last_error = "429 rate limited"
        await self._persist(
            ks, "status=?, cooldown_until=?, last_error=?",
            (ks.status, ks.cooldown_until, ks.last_error),
        )

    async def report_exhausted(self, ks: KeyState, detail: str) -> None:
        async with self._lock:
            ks.status = "exhausted"
            ks.last_error = detail
        await self._persist(ks, "status=?, last_error=?", (ks.status, ks.last_error))

    async def report_invalid(self, ks: KeyState, detail: str) -> None:
        async with self._lock:
            ks.status = "disabled"
            ks.last_error = detail
        await self._persist(ks, "status=?, last_error=?", (ks.status, ks.last_error))

    async def report_transient(self, ks: KeyState, detail: str) -> None:
        """5xx / network error: do not punish the key, just remember the error."""
        async with self._lock:
            ks.last_error = detail
        await self._persist(ks, "last_error=?", (detail,))

    # -- management (dashboard + background tasks) --------------------------

    async def add_keys(
        self, entries: list[tuple[str, str, float]], created_at: Optional[float] = None
    ) -> tuple[int, int]:
        """Add (key, label, plan_limit) tuples. Returns (added, skipped_duplicates)."""
        added = skipped = 0
        now = created_at or time.time()
        for key, label, plan_limit in entries:
            try:
                await self.db.execute(
                    "INSERT INTO tavily_keys (key, label, plan_limit, monthly_reset_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (key, label, plan_limit, next_month_start_ts(now), now),
                )
                added += 1
            except Exception:  # UNIQUE constraint on key
                skipped += 1
        await self.load()
        return added, skipped

    async def remove_key(self, key_id: int) -> bool:
        row = await self.db.fetchone("SELECT id FROM tavily_keys WHERE id = ?", (key_id,))
        if row is None:
            return False
        await self.db.execute("DELETE FROM tavily_keys WHERE id = ?", (key_id,))
        await self.load()
        return True

    async def set_enabled(self, key_id: int, enabled: bool) -> None:
        status = "active" if enabled else "disabled"
        await self.db.execute(
            "UPDATE tavily_keys SET status=?, cooldown_until=NULL WHERE id=?",
            (status, key_id),
        )
        await self.load()

    async def update_meta(self, key_id: int, label: Optional[str], plan_limit: Optional[float]) -> None:
        if label is not None:
            await self.db.execute("UPDATE tavily_keys SET label=? WHERE id=?", (label, key_id))
        if plan_limit is not None:
            await self.db.execute("UPDATE tavily_keys SET plan_limit=? WHERE id=?", (plan_limit, key_id))
        await self.load()

    async def apply_usage(
        self,
        key_id: int,
        credits_used: float,
        plan_limit: float,
        usage_json: str,
    ) -> bool:
        """Calibrate a key from a real GET /usage response.

        Returns True if the key was recovered from exhausted -> active.
        """
        ks = self.get(key_id)
        if ks is None:
            return False
        async with self._lock:
            ks.credits_used_month = credits_used
            ks.plan_limit = plan_limit or ks.plan_limit
            ks.last_usage_json = usage_json
            recovered = False
            if ks.status in ("exhausted", "cooling") and credits_used < ks.plan_limit:
                ks.status = "active"
                ks.cooldown_until = None
                recovered = True
        await self._persist(
            ks,
            "credits_used_month=?, plan_limit=?, last_usage_json=?, status=?, cooldown_until=?",
            (ks.credits_used_month, ks.plan_limit, usage_json, ks.status, None),
        )
        return recovered

    async def roll_month_if_needed(self, now: Optional[float] = None) -> int:
        """Reset monthly counters at the month boundary. Returns resets done."""
        now = now or time.time()
        current_month = month_start_ts(now)
        resets = 0
        for ks in list(self._keys):
            if ks.monthly_reset_at is None or now >= ks.monthly_reset_at:
                async with self._lock:
                    ks.credits_used_month = 0.0
                    ks.monthly_reset_at = next_month_start_ts(now)
                    if ks.status == "exhausted":
                        ks.status = "active"  # usage sync will re-verify
                await self._persist(
                    ks,
                    "credits_used_month=?, monthly_reset_at=?, status=?",
                    (ks.credits_used_month, ks.monthly_reset_at, ks.status),
                )
                resets += 1
        return resets
