"""KeyPool state machine tests."""

from __future__ import annotations

import time

from app.pool import KeyPool, month_start_ts, next_month_start_ts


async def test_round_robin_acquisition(pool: KeyPool):
    ids = [(await pool.acquire()).id for _ in range(6)]
    assert ids == [1, 2, 3, 1, 2, 3]


async def test_rate_limit_cools_and_recovers(pool: KeyPool, db):
    ks1 = pool.get(1)
    await pool.report_rate_limited(ks1)
    assert ks1.status == "cooling"
    assert ks1.effective_status == "cooling"

    # Round-robin now yields only keys 2 and 3.
    acquired = [(await pool.acquire()).id for _ in range(4)]
    assert set(acquired) == {2, 3}

    # Expire the cooldown: key 1 becomes usable again.
    ks1.cooldown_until = time.time() - 1
    assert ks1.effective_status == "active"
    assert (await pool.acquire()).id in {1, 2, 3}

    row = await db.fetchone("SELECT status, cooldown_until FROM tavily_keys WHERE id=1")
    assert row["status"] == "cooling"


async def test_exhausted_key_is_skipped(pool: KeyPool):
    ks2 = pool.get(2)
    await pool.report_exhausted(ks2, "HTTP 432: quota exceeded")
    acquired = [(await pool.acquire()).id for _ in range(4)]
    assert set(acquired) == {1, 3}


async def test_invalid_key_is_disabled(pool: KeyPool):
    ks3 = pool.get(3)
    await pool.report_invalid(ks3, "401: invalid key")
    assert ks3.effective_status == "disabled"
    acquired = [(await pool.acquire()).id for _ in range(4)]
    assert set(acquired) == {1, 2}


async def test_success_crossing_limit_marks_exhausted(pool: KeyPool):
    ks1 = pool.get(1)
    ks1.plan_limit = 10.0
    await pool.report_success(ks1, 6.0)
    assert ks1.effective_status == "active"
    await pool.report_success(ks1, 6.0)  # 12/10 -> exhausted
    assert ks1.effective_status == "exhausted"
    acquired = [(await pool.acquire()).id for _ in range(4)]
    assert set(acquired) == {2, 3}


async def test_apply_usage_recovers_exhausted_key(pool: KeyPool):
    ks1 = pool.get(1)
    await pool.report_exhausted(ks1, "HTTP 432")
    recovered = await pool.apply_usage(1, credits_used=900.0, plan_limit=1000.0, usage_json="{}")
    assert recovered is True
    assert ks1.effective_status == "active"
    assert ks1.credits_used_month == 900.0


async def test_apply_usage_keeps_exhausted_when_still_full(pool: KeyPool):
    ks1 = pool.get(1)
    await pool.report_exhausted(ks1, "HTTP 432")
    recovered = await pool.apply_usage(1, credits_used=1000.0, plan_limit=1000.0, usage_json="{}")
    assert recovered is False
    assert ks1.effective_status == "exhausted"


async def test_month_rollover_resets_counters(pool: KeyPool):
    for ks in pool.snapshot():
        await pool.report_exhausted(ks, "HTTP 432")
    # Backdate every key's reset time so the roll triggers now.
    for ks in pool.snapshot():
        ks.monthly_reset_at = time.time() - 1
    resets = await pool.roll_month_if_needed()
    assert resets == 3
    for ks in pool.snapshot():
        assert ks.credits_used_month == 0.0
        assert ks.effective_status == "active"
        assert ks.monthly_reset_at > time.time()


async def test_add_keys_dedupes(pool: KeyPool):
    added, skipped = await pool.add_keys(
        [("tvly-test-key-aaaa1111", "dup", 1000.0), ("tvly-brand-new-key", "fresh", 1000.0)]
    )
    assert (added, skipped) == (1, 1)
    assert len(pool) == 4


async def test_remove_key(pool: KeyPool):
    assert await pool.remove_key(2) is True
    assert await pool.remove_key(999) is False
    assert len(pool) == 2
    acquired = [(await pool.acquire()).id for _ in range(4)]
    assert set(acquired) == {1, 3}


async def test_set_enabled(pool: KeyPool):
    await pool.set_enabled(1, False)
    assert pool.get(1).effective_status == "disabled"
    await pool.set_enabled(1, True)
    assert pool.get(1).effective_status == "active"


async def test_next_recovery_hint(pool: KeyPool):
    await pool.report_rate_limited(pool.get(1))
    hint = pool.next_recovery_hint()
    assert "cooldown" in hint


def test_month_helpers():
    now = time.time()
    assert next_month_start_ts(now) > month_start_ts(now)
    assert month_start_ts(next_month_start_ts(now)) == next_month_start_ts(now)
