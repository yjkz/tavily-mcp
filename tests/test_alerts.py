"""Alerter unit tests (webhook delivery mocked with respx)."""

from __future__ import annotations

import json
import time

import httpx
import respx

from app.alerts import Alerter


async def _configure(db, **overrides):
    settings = {
        "alert_channel": "generic",
        "alert_webhook_url": "http://hook.test/alert",
    }
    settings.update(overrides)
    for key, value in settings.items():
        await db.set_setting(key, value)


@respx.mock
async def test_key_disabled_alert_sends_and_dedups(db, pool):
    await _configure(db)
    route = respx.post("http://hook.test/alert").mock(
        return_value=httpx.Response(200, json={})
    )
    alerter = Alerter(db)
    ks = pool.get(1)
    await alerter.key_event("key_disabled", ks, "401: invalid api key")
    await alerter.flush()
    assert route.called
    body = json.loads(route.calls.last.request.read())
    assert body["event"] == "key_disabled"
    assert "401" in body["detail"]
    # Second event for the same key within the cooldown is suppressed.
    await alerter.key_event("key_disabled", ks, "401: invalid api key")
    await alerter.flush()
    assert route.call_count == 1
    # A different key still alerts (dedup is per key).
    await alerter.key_event("key_disabled", pool.get(2), "401: invalid")
    await alerter.flush()
    assert route.call_count == 2
    await alerter.aclose()


@respx.mock
async def test_key_exhausted_alert_off_by_default(db, pool):
    await _configure(db)  # alert_on_key_exhausted defaults to "0"
    route = respx.post("http://hook.test/alert").mock(
        return_value=httpx.Response(200, json={})
    )
    alerter = Alerter(db)
    await alerter.key_event("key_exhausted", pool.get(1), "HTTP 432")
    await alerter.flush()
    assert route.call_count == 0
    await alerter.aclose()


@respx.mock
async def test_pool_exhausted_then_recovered(db, pool):
    await _configure(db)
    route = respx.post("http://hook.test/alert").mock(
        return_value=httpx.Response(200, json={})
    )
    alerter = Alerter(db)
    # All keys cooling with a future cooldown: pool is dead.
    for ks in pool.snapshot():
        ks.status = "cooling"
        ks.cooldown_until = time.time() + 999
    await alerter.pool_check(pool)
    await alerter.flush()
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.read())["event"] == "pool_exhausted"

    # Cooldowns expire: pool recovers, a transition message is sent.
    for ks in pool.snapshot():
        ks.cooldown_until = time.time() - 1
    await alerter.pool_check(pool)
    await alerter.flush()
    assert route.call_count == 2
    assert json.loads(route.calls.last.request.read())["event"] == "pool_recovered"
    await alerter.aclose()


@respx.mock
async def test_pool_low_active_threshold(db, pool):
    await _configure(db, alert_pool_min_active="3")
    route = respx.post("http://hook.test/alert").mock(
        return_value=httpx.Response(200, json={})
    )
    alerter = Alerter(db)
    # 3 active keys and threshold 3: alerts (<= comparison).
    await alerter.pool_check(pool)
    await alerter.flush()
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.read())["event"] == "pool_low_active"
    await alerter.aclose()


@respx.mock
async def test_no_url_means_disabled(db, pool):
    await _configure(db, alert_webhook_url="")
    route = respx.post("http://hook.test/alert").mock(
        return_value=httpx.Response(200, json={})
    )
    alerter = Alerter(db)
    await alerter.key_event("key_disabled", pool.get(1), "401: invalid")
    for ks in pool.snapshot():
        ks.status = "cooling"
        ks.cooldown_until = time.time() + 999
    await alerter.pool_check(pool)
    await alerter.flush()
    assert route.call_count == 0
    await alerter.aclose()


# -- email channel -----------------------------------------------------------


async def test_email_channel_delivers(db, pool, monkeypatch):
    await _configure(
        db,
        alert_channel="email",
        alert_email_smtp_host="smtp.test",
        alert_email_username="sender@test.com",
        alert_email_password="auth-code",
        alert_email_to="a@x.com, b@x.com",
    )
    sent: list[tuple[str, str, dict]] = []

    async def fake_send_email(self, cfg, subject, body):
        sent.append((subject, body, cfg))

    monkeypatch.setattr(Alerter, "_send_email", fake_send_email)
    alerter = Alerter(db)
    await alerter.key_event("key_disabled", pool.get(1), "401: invalid api key")
    await alerter.flush()
    assert len(sent) == 1
    subject, body, cfg = sent[0]
    assert subject == "[Tavily Pool] Key 已禁用"
    assert "401" in body
    assert cfg["alert_email_smtp_host"] == "smtp.test"
    await alerter.aclose()


async def test_email_channel_requires_full_config(db, pool, monkeypatch):
    # Email selected but host/credentials/recipients missing.
    await _configure(db, alert_channel="email")
    called = []

    async def fake_send_email(self, cfg, subject, body):
        called.append(subject)

    monkeypatch.setattr(Alerter, "_send_email", fake_send_email)
    alerter = Alerter(db)
    # Event path stays silent when the channel is not fully configured.
    await alerter.key_event("key_disabled", pool.get(1), "401: invalid")
    await alerter.flush()
    assert called == []
    # The test button reports the missing fields.
    ok, error = await alerter.send_test()
    assert ok is False
    assert "SMTP" in error and "收件邮箱" in error
    await alerter.aclose()
