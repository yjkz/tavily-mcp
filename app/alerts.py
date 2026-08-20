"""Webhook / email alerting for pool health events.

Events are queued from the request path (never blocking tool calls) and
drained by a background sender task. Channels:
  - feishu / wecom / dingtalk (webhook, optional HMAC signing)
  - generic JSON webhook
  - email via SMTP (e.g. 163/QQ mailboxes with an authorization code as the
    password; sending only needs SMTP, IMAP is for reading and not required)

All settings live in the `settings` KV table (`alert_*` keys) and are cached
in memory for CONFIG_TTL seconds.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import smtplib
import time
import urllib.parse
from email.message import EmailMessage
from typing import Any, Optional

import httpx

from .db import Database

logger = logging.getLogger("tavily_pool.alerts")

KEY_EVENT_COOLDOWN = 1800.0   # per (event, key): 30 min
POOL_EVENT_COOLDOWN = 600.0   # per event: 10 min
CONFIG_TTL = 60.0             # settings cache lifetime

WEBHOOK_CHANNELS = {"feishu", "wecom", "dingtalk", "generic"}

ALERT_SETTINGS_DEFAULTS = {
    "alert_channel": "",               # "" | feishu | wecom | dingtalk | generic | email
    "alert_webhook_url": "",
    "alert_webhook_secret": "",
    # email channel (SMTP only; IMAP is for receiving and not needed here)
    "alert_email_smtp_host": "",       # e.g. smtp.163.com
    "alert_email_smtp_port": "465",    # 465 = SSL, 587 = STARTTLS
    "alert_email_use_ssl": "1",
    "alert_email_username": "",        # mailbox address
    "alert_email_password": "",        # authorization code (授权码) for 163/QQ etc.
    "alert_email_from": "",            # sender address, defaults to username
    "alert_email_to": "",              # recipients, comma-separated
    "alert_on_key_disabled": "1",      # any key dies with 401
    "alert_on_key_exhausted": "0",     # any key exhausted (noisy for free pools)
    "alert_on_pool_exhausted": "1",    # zero usable keys
    "alert_pool_min_active": "0",      # int, 0 = off: active keys <= N
    "alert_pool_min_remaining": "0",   # float, 0 = off: remaining credits <= N
}


def _channel_ready(cfg: dict[str, str]) -> bool:
    """Whether the selected channel has enough configuration to deliver."""
    channel = cfg["alert_channel"]
    if channel in WEBHOOK_CHANNELS:
        return bool(cfg["alert_webhook_url"])
    if channel == "email":
        return all(
            cfg[k]
            for k in (
                "alert_email_smtp_host",
                "alert_email_username",
                "alert_email_password",
                "alert_email_to",
            )
        )
    return False


async def read_alert_settings(db: Database) -> dict[str, str]:
    """Raw alert_* settings merged over defaults (single source of truth)."""
    rows = await db.fetchall("SELECT key, value FROM settings WHERE key LIKE 'alert_%'")
    cfg = dict(ALERT_SETTINGS_DEFAULTS)
    cfg.update({r["key"]: r["value"] for r in rows})
    return cfg


def _mask(key: str) -> str:
    if len(key) <= 12:
        return key[:4] + "…"
    return f"{key[:8]}…{key[-4:]}"


def _feishu_signed_url(url: str, secret: str) -> str:
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timestamp={timestamp}&sign={sign}"


def _dingtalk_signed_url(url: str, secret: str) -> str:
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timestamp={timestamp}&sign={sign}"


class Alerter:
    """Queue-based notifier; safe to call from the request hot path."""

    def __init__(self, db: Database):
        self._db = db
        self._client = httpx.AsyncClient(timeout=10.0)
        self._queue: asyncio.Queue[tuple[str, str, str]] = asyncio.Queue()
        self._last_sent: dict[tuple[str, int], float] = {}
        self._pool_dead = False
        self._cfg_cache: tuple[float, dict[str, str]] = (0.0, dict(ALERT_SETTINGS_DEFAULTS))
        self._sender: Optional[asyncio.Task] = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._sender is None:
            self._sender = asyncio.create_task(self._sender_loop(), name="alerts-sender")

    async def aclose(self) -> None:
        if self._sender is not None:
            self._sender.cancel()
            await asyncio.gather(self._sender, return_exceptions=True)
            self._sender = None
        await self._client.aclose()

    def invalidate_cache(self) -> None:
        self._cfg_cache = (0.0, dict(ALERT_SETTINGS_DEFAULTS))

    # -- config / dedup ------------------------------------------------------

    async def _cfg(self) -> dict[str, str]:
        now = time.monotonic()
        if now - self._cfg_cache[0] < CONFIG_TTL:
            return self._cfg_cache[1]
        cfg = await read_alert_settings(self._db)
        self._cfg_cache = (now, cfg)
        return cfg

    def _dedup(self, event: str, key_id: int, cooldown: float) -> bool:
        now = time.monotonic()
        if now - self._last_sent.get((event, key_id), 0.0) < cooldown:
            return False
        self._last_sent[(event, key_id)] = now
        return True

    # -- public API (called from pool / tasks / admin) ------------------------

    async def key_event(self, event: str, ks, detail: str) -> None:
        """event: 'key_disabled' | 'key_exhausted'."""
        try:
            cfg = await self._cfg()
            if not _channel_ready(cfg):
                return
            flag = "alert_on_key_disabled" if event == "key_disabled" else "alert_on_key_exhausted"
            if cfg[flag] != "1":
                return
            if not self._dedup(event, ks.id, KEY_EVENT_COOLDOWN):
                return
            title = "Key 已禁用" if event == "key_disabled" else "Key 配额耗尽"
            body = f"Key #{ks.id} {ks.label or ''}({_mask(ks.key)})\n原因:{detail}"
            self._queue.put_nowait((event, title, body))
        except Exception:
            logger.exception("alert key_event failed")

    async def pool_check(self, pool) -> None:
        """Evaluate pool-level conditions; call after any state change."""
        try:
            cfg = await self._cfg()
            if not _channel_ready(cfg):
                return
            keys = pool.snapshot()
            if not keys:
                return
            active = sum(1 for ks in keys if ks.effective_status == "active")
            remaining = sum(ks.remaining_credits for ks in keys)

            if active == 0:
                self._pool_dead = True
                if (
                    cfg["alert_on_pool_exhausted"] == "1"
                    and self._dedup("pool_exhausted", 0, POOL_EVENT_COOLDOWN)
                ):
                    self._queue.put_nowait((
                        "pool_exhausted",
                        "Key 池告警:全池不可用",
                        f"共 {len(keys)} 个 key,当前 0 个可用。{pool.next_recovery_hint()}。",
                    ))
                return

            if self._pool_dead:
                self._pool_dead = False
                self._queue.put_nowait((
                    "pool_recovered",
                    "Key 池已恢复",
                    f"当前可用 key {active} 个。",
                ))

            min_active = int(float(cfg["alert_pool_min_active"] or 0))
            if (
                min_active > 0
                and active <= min_active
                and self._dedup("pool_low_active", 0, POOL_EVENT_COOLDOWN)
            ):
                self._queue.put_nowait((
                    "pool_low_active",
                    "Key 池可用数量过低",
                    f"可用 key 仅 {active} 个(阈值 {min_active})。",
                ))

            min_remaining = float(cfg["alert_pool_min_remaining"] or 0)
            if (
                min_remaining > 0
                and remaining <= min_remaining
                and self._dedup("pool_low_remaining", 0, POOL_EVENT_COOLDOWN)
            ):
                self._queue.put_nowait((
                    "pool_low_remaining",
                    "Key 池剩余配额过低",
                    f"全池剩余约 {remaining:.0f} credits(阈值 {min_remaining:.0f})。",
                ))
        except Exception:
            logger.exception("alert pool_check failed")

    async def send_test(self) -> tuple[bool, str]:
        """Direct send for the dashboard test button; returns (ok, error)."""
        cfg = await self._cfg()
        if not cfg["alert_channel"]:
            return False, "未选择告警渠道"
        if cfg["alert_channel"] == "email":
            missing = [
                label
                for key, label in (
                    ("alert_email_smtp_host", "SMTP 服务器"),
                    ("alert_email_username", "发信邮箱"),
                    ("alert_email_password", "邮箱密码/授权码"),
                    ("alert_email_to", "收件邮箱"),
                )
                if not cfg[key]
            ]
            if missing:
                return False, "缺少:" + "、".join(missing)
        elif not cfg["alert_webhook_url"]:
            return False, "未配置告警 Webhook 地址"
        try:
            await self._send(
                cfg, "test", "Tavily Pool 测试告警",
                "如果你看到这条消息,说明告警配置已生效。",
            )
            return True, ""
        except Exception as e:
            return False, f"发送失败:{e}"

    async def flush(self) -> None:
        """Drain pending notifications immediately (tests, admin test button)."""
        while not self._queue.empty():
            event, title, body = self._queue.get_nowait()
            try:
                await self._send(await self._cfg(), event, title, body)
            except Exception:
                logger.exception("alert flush send failed")

    # -- delivery ------------------------------------------------------------

    async def _sender_loop(self) -> None:
        while True:
            event, title, body = await self._queue.get()
            try:
                await self._send(await self._cfg(), event, title, body)
            except Exception:
                logger.exception("alert send failed")

    async def _send(self, cfg: dict[str, str], event: str, title: str, body: str) -> None:
        channel = cfg["alert_channel"]
        if channel == "email":
            await self._send_email(cfg, f"[Tavily Pool] {title}", body)
            return
        url = cfg["alert_webhook_url"]
        secret = cfg["alert_webhook_secret"]
        text = f"[Tavily Pool] {title}\n{body}"
        payload: dict[str, Any]
        if channel == "feishu":
            payload = {"msg_type": "text", "content": {"text": text}}
            if secret:
                url = _feishu_signed_url(url, secret)
        elif channel == "wecom":
            payload = {"msgtype": "text", "text": {"content": text}}
        elif channel == "dingtalk":
            payload = {"msgtype": "text", "text": {"content": text}}
            if secret:
                url = _dingtalk_signed_url(url, secret)
        else:  # generic JSON webhook
            payload = {"event": event, "title": title, "detail": body, "ts": time.time()}
        resp = await self._client.post(url, json=payload)
        if resp.status_code >= 400:
            logger.warning("alert webhook returned %s: %s", resp.status_code, resp.text[:200])

    # -- email channel ---------------------------------------------------------

    async def _send_email(self, cfg: dict[str, str], subject: str, body: str) -> None:
        """Send via SMTP; blocking smtplib runs on a worker thread."""
        await asyncio.to_thread(self._send_smtp_sync, cfg, subject, body)

    @staticmethod
    def _sender_address(raw_from: str, username: str) -> str:
        """Normalize the From value.

        Accepts three forms and tolerates a display-name-only entry:
          ""                          -> username
          "a@b.com"                    -> a@b.com
          "Name <a@b.com>"             -> Name <a@b.com> (display name kept)
          "Tavily Pool 网关"            -> "Tavily Pool 网关" <username>
        A bare display name without "<...>" would otherwise be parsed as an
        (invalid) mailbox and rejected by EmailMessage as a multiple-address
        / international-address error.
        """
        raw = (raw_from or "").strip()
        if not raw:
            return username
        if "@" in raw:
            return raw
        # Display name only: quote it and pair with the login mailbox.
        name = raw.replace('"', "'")
        return f'"{name}" <{username}>'

    def _send_smtp_sync(self, cfg: dict[str, str], subject: str, body: str) -> None:
        host = cfg["alert_email_smtp_host"]
        port = int(cfg["alert_email_smtp_port"] or 465)
        use_ssl = cfg["alert_email_use_ssl"] != "0"
        username = cfg["alert_email_username"]
        password = cfg["alert_email_password"]
        sender = self._sender_address(cfg["alert_email_from"], username)
        recipients = [r.strip() for r in cfg["alert_email_to"].split(",") if r.strip()]
        if not recipients:
            raise ValueError("no recipients configured")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        msg.set_content(body, charset="utf-8")

        if use_ssl:
            client = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            client = smtplib.SMTP(host, port, timeout=10)
        try:
            if not use_ssl:
                try:
                    client.starttls()
                except smtplib.SMTPException:
                    pass  # plain SMTP on internal relays (port 25)
            client.login(username, password)
            client.send_message(msg)
        finally:
            client.quit()
