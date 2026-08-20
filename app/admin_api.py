"""Admin REST API for the dashboard: login session, key/token CRUD, logs, stats."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .alerts import read_alert_settings
from .config import SESSION_COOKIE, SESSION_MAX_AGE, Config
from .mcp_server import TOKEN_PREFIX
from .pool import day_start_ts, month_start_ts
from .state import get_state
from .tasks import sync_all_keys
from .tavily import TavilyError, parse_usage

logger = logging.getLogger("tavily_pool.admin")

LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 600

DEFAULT_ICON_PATH = Path(__file__).resolve().parent.parent / "dashboard" / "dist" / "favicon.png"
ALLOWED_ICON_TYPES = {
    "image/png",
    "image/jpeg",
    "image/svg+xml",
    "image/webp",
    "image/gif",
    "image/x-icon",
    "image/vnd.microsoft.icon",
}


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), 200_000)
    return f"{salt}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), 200_000)
    return secrets.compare_digest(digest.hex(), expected)


def mask_key(key: str) -> str:
    if len(key) <= 12:
        return key[:4] + "…"
    return f"{key[:8]}…{key[-4:]}"


def _normalize_allowed_tools(value: Any) -> Optional[str]:
    """Accept a list or comma-separated string; None/empty → None (no restriction)."""
    if isinstance(value, list):
        cleaned = [str(t).strip() for t in value if str(t).strip()]
    elif isinstance(value, str) and value.strip():
        cleaned = [t.strip() for t in value.split(",") if t.strip()]
    else:
        return None
    return ",".join(cleaned) or None


def _fernet(session_secret: str) -> Fernet:
    """Deterministic Fernet derived from SESSION_SECRET for token export.

    The access-token plaintext stays exportable from the dashboard (one-click
    MCP URL copy) while never sitting in the database as plain text.
    """
    import base64

    key = base64.urlsafe_b64encode(hashlib.sha256(session_secret.encode()).digest())
    return Fernet(key)


class SessionManager:
    def __init__(self, secret: str, secure: bool):
        self._signer = TimestampSigner(secret)
        self._secure = secure

    def issue(self, response: Response) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            self._signer.sign("admin").decode("utf-8"),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=self._secure,
            path="/",
        )

    def clear(self, response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")

    def is_valid(self, request: Request) -> bool:
        value = request.cookies.get(SESSION_COOKIE)
        if not value:
            return False
        try:
            return self._signer.unsign(value, max_age=SESSION_MAX_AGE) == b"admin"
        except (BadSignature, SignatureExpired):
            return False


class LoginThrottle:
    def __init__(self) -> None:
        self._fails: dict[str, tuple[int, float]] = {}

    def check(self, ip: str) -> tuple[bool, int]:
        entry = self._fails.get(ip)
        if not entry:
            return True, 0
        fails, locked_until = entry
        if locked_until > time.time():
            return False, int(locked_until - time.time()) + 1
        return True, 0

    def record_fail(self, ip: str) -> None:
        fails, locked_until = self._fails.get(ip, (0, 0.0))
        fails += 1
        if fails >= LOGIN_MAX_FAILS:
            self._fails[ip] = (0, time.time() + LOGIN_LOCK_SECONDS)
        else:
            self._fails[ip] = (fails, locked_until)

    def record_success(self, ip: str) -> None:
        self._fails.pop(ip, None)


async def read_json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def build_admin_routes(config: Config) -> list[Route]:
    sessions = SessionManager(config.session_secret, config.cookie_secure)
    throttle = LoginThrottle()

    def admin(handler):
        async def wrapped(request: Request) -> Response:
            if not sessions.is_valid(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            try:
                return await handler(request)
            except Exception:
                logger.exception("admin API error in %s", request.url.path)
                return JSONResponse({"error": "internal error"}, status_code=500)

        return wrapped

    # -- session -----------------------------------------------------------

    async def login(request: Request) -> Response:
        ip = request.client.host if request.client else "?"
        allowed, wait = throttle.check(ip)
        if not allowed:
            return JSONResponse(
                {"error": f"失败次数过多,请 {wait} 秒后再试"}, status_code=429
            )
        data = await read_json(request)
        password = str(data.get("password", ""))
        stored_hash = await get_state().db.get_setting("admin_password_hash")
        if stored_hash:
            password_ok = _verify_password(password, stored_hash)
        else:
            password_ok = secrets.compare_digest(password, config.admin_password)
        if not password_ok:
            throttle.record_fail(ip)
            return JSONResponse({"error": "密码错误"}, status_code=401)
        throttle.record_success(ip)
        resp = JSONResponse({"ok": True})
        sessions.issue(resp)
        return resp

    async def logout(request: Request) -> Response:
        resp = JSONResponse({"ok": True})
        sessions.clear(resp)
        return resp

    async def session_check(request: Request) -> Response:
        return JSONResponse({"ok": True})

    # -- overview & stats ----------------------------------------------------

    async def overview(request: Request) -> Response:
        state = get_state()
        day0 = day_start_ts()
        month0 = month_start_ts()
        today = await state.db.fetchone(
            "SELECT COUNT(*) AS c, "
            "TOTAL(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok "
            "FROM request_logs WHERE ts >= ?",
            (day0,),
        )
        month = await state.db.fetchone(
            "SELECT COUNT(*) AS c, TOTAL(credits) AS credits "
            "FROM request_logs WHERE ts >= ? AND status='success'",
            (month0,),
        )
        status_counts = {"active": 0, "cooling": 0, "exhausted": 0, "disabled": 0}
        for ks in state.pool.snapshot():
            eff = ks.effective_status
            if eff in status_counts:
                status_counts[eff] += 1
        tokens_active = await state.db.fetchone(
            "SELECT COUNT(*) AS c FROM access_tokens WHERE is_active = 1"
        )
        keys_total = len(state.pool)
        total_requests = sum(ks.total_requests for ks in state.pool.snapshot())
        return JSONResponse(
            {
                "today_requests": today["c"] if today else 0,
                "today_success": int(today["ok"]) if today else 0,
                "month_requests": month["c"] if month else 0,
                "month_credits": round(month["credits"] or 0.0, 2) if month else 0.0,
                "keys_total": keys_total,
                "keys_status": status_counts,
                "pool_capacity_limit": sum(
                    ks.plan_limit for ks in state.pool.snapshot()
                ),
                "pool_credits_used": round(
                    sum(ks.credits_used_month for ks in state.pool.snapshot()), 2
                ),
                "tokens_active": tokens_active["c"] if tokens_active else 0,
                "total_key_requests": total_requests,
            }
        )

    async def stats_daily(request: Request) -> Response:
        state = get_state()
        try:
            days = min(90, max(1, int(request.query_params.get("days", "14"))))
        except ValueError:
            days = 14
        since = time.time() - days * 86400
        rows = await state.db.fetchall(
            "SELECT date(ts, 'unixepoch') AS d, COUNT(*) AS c, "
            "TOTAL(CASE WHEN status!='success' THEN 1 ELSE 0 END) AS errors, "
            "TOTAL(credits) AS credits "
            "FROM request_logs WHERE ts >= ? GROUP BY d ORDER BY d",
            (since,),
        )
        return JSONResponse(
            [
                {
                    "date": r["d"],
                    "requests": r["c"],
                    "errors": int(r["errors"]),
                    "credits": round(r["credits"] or 0.0, 2),
                }
                for r in rows
            ]
        )

    # -- keys ----------------------------------------------------------------

    async def keys_list(request: Request) -> Response:
        state = get_state()
        items = []
        for ks in state.pool.snapshot():
            items.append(
                {
                    "id": ks.id,
                    "label": ks.label,
                    "masked_key": mask_key(ks.key),
                    "status": ks.effective_status,
                    "stored_status": ks.status,
                    "cooldown_until": ks.cooldown_until,
                    "credits_used_month": round(ks.credits_used_month, 2),
                    "plan_limit": ks.plan_limit,
                    "remaining_credits": round(ks.remaining_credits, 2),
                    "monthly_reset_at": ks.monthly_reset_at,
                    "total_requests": ks.total_requests,
                    "last_used_at": ks.last_used_at,
                    "last_error": ks.last_error,
                }
            )
        return JSONResponse(items)

    async def keys_create(request: Request) -> Response:
        state = get_state()
        data = await read_json(request)
        raw = data.get("keys")
        if isinstance(raw, str):
            candidates = [ln.strip() for ln in raw.splitlines()]
        elif isinstance(raw, list):
            candidates = [str(k).strip() for k in raw]
        else:
            candidates = []
        candidates = [c for c in candidates if c]
        # de-duplicate within the input while preserving order
        seen = set()
        entries = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                entries.append((c, str(data.get("label", "")), float(data.get("plan_limit", 1000))))
        if not entries:
            return JSONResponse({"error": "未提供有效的 key"}, status_code=400)
        added, skipped = await state.pool.add_keys(entries)
        return JSONResponse({"added": added, "skipped_duplicates": skipped})

    async def key_patch(request: Request) -> Response:
        state = get_state()
        key_id = int(request.path_params["key_id"])
        data = await read_json(request)
        if "enabled" in data:
            await state.pool.set_enabled(key_id, bool(data["enabled"]))
        label = data.get("label")
        plan_limit = data.get("plan_limit")
        if label is not None or plan_limit is not None:
            await state.pool.update_meta(
                key_id,
                str(label) if label is not None else None,
                float(plan_limit) if plan_limit is not None else None,
            )
        if state.pool.get(key_id) is None:
            return JSONResponse({"error": "key 不存在"}, status_code=404)
        return JSONResponse({"ok": True})

    async def key_delete(request: Request) -> Response:
        state = get_state()
        key_id = int(request.path_params["key_id"])
        ok = await state.pool.remove_key(key_id)
        if not ok:
            return JSONResponse({"error": "key 不存在"}, status_code=404)
        return JSONResponse({"ok": True})

    async def key_test(request: Request) -> Response:
        """Test-connection button: real GET /usage call with this key."""
        state = get_state()
        key_id = int(request.path_params["key_id"])
        ks = state.pool.get(key_id)
        if ks is None:
            return JSONResponse({"error": "key 不存在"}, status_code=404)
        t0 = time.perf_counter()
        try:
            usage_body = await state.tavily.usage(ks.key)
        except TavilyError as e:
            latency = int((time.perf_counter() - t0) * 1000)
            detail = f"HTTP {e.status}: {e.detail}"
            if e.status == 401:
                await state.pool.report_invalid(ks, detail)
            elif e.status not in (429, 432, 433):
                await state.pool.report_transient(ks, detail)
            logger.info("key %s test failed: %s", key_id, detail)
            return JSONResponse(
                {"ok": False, "latency_ms": latency, "status": e.status, "error": detail}
            )
        latency = int((time.perf_counter() - t0) * 1000)
        parsed = parse_usage(usage_body)
        result: dict[str, Any] = {
            "ok": True,
            "latency_ms": latency,
            "plan": parsed["plan"],
            "source": parsed["source"],
            "calibrated": False,
            "recovered": False,
        }
        if parsed["credits_used"] is not None:
            plan_limit = parsed["plan_limit"] or ks.plan_limit
            recovered = await state.pool.apply_usage(
                key_id,
                parsed["credits_used"],
                plan_limit,
                json.dumps(usage_body),
            )
            result.update(
                {
                    "calibrated": True,
                    "recovered": recovered,
                    "credits_used": parsed["credits_used"],
                    "plan_limit": plan_limit,
                    "remaining": parsed["remaining"],
                }
            )
        logger.info("key %s test ok in %sms: %s", key_id, latency, parsed)
        return JSONResponse(result)

    async def keys_sync_all(request: Request) -> Response:
        """Batch version of the test button: calibrate every key now."""
        state = get_state()
        results = await sync_all_keys(state)
        return JSONResponse(results)

    # -- tokens ---------------------------------------------------------------

    async def tokens_list(request: Request) -> Response:
        state = get_state()
        day0 = day_start_ts()
        month0 = month_start_ts()
        rows = await state.db.fetchall(
            "SELECT id, name, prefix, tier, allowed_tools, rpm_limit, daily_quota, "
            "monthly_credits_limit, is_active, last_used_at, created_at, revoked_at "
            "FROM access_tokens ORDER BY id"
        )
        today_rows = await state.db.fetchall(
            "SELECT token_id, COUNT(*) AS c FROM request_logs "
            "WHERE ts >= ? AND status != 'rate_limited' GROUP BY token_id",
            (day0,),
        )
        month_rows = await state.db.fetchall(
            "SELECT token_id, COUNT(*) AS c, TOTAL(credits) AS credits FROM request_logs "
            "WHERE ts >= ? AND status = 'success' GROUP BY token_id",
            (month0,),
        )
        today_map = {r["token_id"]: r["c"] for r in today_rows}
        month_map = {r["token_id"]: (r["c"], r["credits"] or 0.0) for r in month_rows}
        items = []
        for r in rows:
            month_req, month_credits = month_map.get(r["id"], (0, 0.0))
            items.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "prefix": r["prefix"],
                    "tier": r["tier"],
                    "allowed_tools": r["allowed_tools"],
                    "rpm_limit": r["rpm_limit"],
                    "daily_quota": r["daily_quota"],
                    "monthly_credits_limit": r["monthly_credits_limit"],
                    "is_active": bool(r["is_active"]),
                    "last_used_at": r["last_used_at"],
                    "created_at": r["created_at"],
                    "today_requests": today_map.get(r["id"], 0),
                    "month_requests": month_req,
                    "month_credits": round(month_credits, 2),
                }
            )
        return JSONResponse(items)

    async def tokens_create(request: Request) -> Response:
        state = get_state()
        data = await read_json(request)
        name = str(data.get("name", "")).strip()
        if not name:
            return JSONResponse({"error": "请填写名称"}, status_code=400)
        tier = data.get("tier", "standard")
        if tier not in ("standard", "full"):
            return JSONResponse({"error": "tier 必须是 standard 或 full"}, status_code=400)

        def num(key: str, default=None):
            v = data.get(key)
            return default if v in (None, "") else v

        raw = TOKEN_PREFIX + secrets.token_hex(20)
        token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        token_enc = _fernet(config.session_secret).encrypt(raw.encode("utf-8")).decode("ascii")
        allowed_tools = _normalize_allowed_tools(data.get("allowed_tools"))
        token_id = await state.db.execute(
            "INSERT INTO access_tokens (name, token_hash, prefix, tier, allowed_tools, rpm_limit, "
            "daily_quota, monthly_credits_limit, is_active, created_at, token_enc) "
            "VALUES (?,?,?,?,?,?,?,?,1,?,?)",
            (
                name,
                token_hash,
                raw[:12],
                tier,
                allowed_tools,
                int(num("rpm_limit", config.default_token_rpm)),
                int(num("daily_quota")) if num("daily_quota") is not None else None,
                float(num("monthly_credits_limit"))
                if num("monthly_credits_limit") is not None
                else None,
                time.time(),
                token_enc,
            ),
        )
        # The plaintext token is returned exactly once, never stored.
        return JSONResponse({"id": token_id, "name": name, "tier": tier, "token": raw})

    async def token_patch(request: Request) -> Response:
        state = get_state()
        token_id = int(request.path_params["token_id"])
        data = await read_json(request)
        row = await state.db.fetchone(
            "SELECT id FROM access_tokens WHERE id = ?", (token_id,)
        )
        if row is None:
            return JSONResponse({"error": "token 不存在"}, status_code=404)
        updates: list[tuple[str, Any]] = []
        if "name" in data:
            updates.append(("name", str(data["name"])))
        if "tier" in data and data["tier"] in ("standard", "full"):
            updates.append(("tier", data["tier"]))
        if "allowed_tools" in data:
            updates.append(("allowed_tools", _normalize_allowed_tools(data["allowed_tools"])))
        if "rpm_limit" in data and data["rpm_limit"] is not None:
            updates.append(("rpm_limit", int(data["rpm_limit"])))
        if "daily_quota" in data:
            updates.append(
                ("daily_quota", int(data["daily_quota"]) if data["daily_quota"] is not None else None)
            )
        if "monthly_credits_limit" in data:
            updates.append(
                (
                    "monthly_credits_limit",
                    float(data["monthly_credits_limit"])
                    if data["monthly_credits_limit"] is not None
                    else None,
                )
            )
        if "is_active" in data:
            active = bool(data["is_active"])
            updates.append(("is_active", 1 if active else 0))
            updates.append(("revoked_at", None if active else time.time()))
        for field, value in updates:
            await state.db.execute(
                f"UPDATE access_tokens SET {field} = ? WHERE id = ?", (value, token_id)
            )
        return JSONResponse({"ok": True})

    async def token_reveal(request: Request) -> Response:
        """One-click export: return the decrypted plaintext token."""
        state = get_state()
        token_id = int(request.path_params["token_id"])
        row = await state.db.fetchone(
            "SELECT token_enc FROM access_tokens WHERE id = ?", (token_id,)
        )
        if row is None:
            return JSONResponse({"error": "token 不存在"}, status_code=404)
        if not row["token_enc"]:
            return JSONResponse(
                {"token": None, "reason": "该 Token 创建于导出功能上线前,明文不可恢复,请删除后重新创建"},
            )
        try:
            plaintext = _fernet(config.session_secret).decrypt(
                row["token_enc"].encode("ascii")
            ).decode("utf-8")
        except InvalidToken:
            return JSONResponse(
                {"token": None, "reason": "SESSION_SECRET 已变更,无法解密;请删除后重新创建"},
            )
        return JSONResponse({"token": plaintext})

    async def token_delete(request: Request) -> Response:
        state = get_state()
        token_id = int(request.path_params["token_id"])
        await state.db.execute("DELETE FROM access_tokens WHERE id = ?", (token_id,))
        return JSONResponse({"ok": True})

    # -- logs ------------------------------------------------------------------
    async def logs_list(request: Request) -> Response:
        state = get_state()
        q = request.query_params
        try:
            limit = min(200, max(1, int(q.get("limit", "50"))))
            offset = max(0, int(q.get("offset", "0")))
        except ValueError:
            limit, offset = 50, 0
        where = ["1=1"]
        params: list[Any] = []
        for column, value in (
            ("token_id", q.get("token_id")),
            ("tavily_key_id", q.get("key_id")),
        ):
            if value:
                where.append(f"l.{column} = ?")
                params.append(int(value))
        if q.get("status"):
            where.append("l.status = ?")
            params.append(q.get("status"))
        if q.get("tool"):
            where.append("l.tool = ?")
            params.append(q.get("tool"))
        where_sql = " AND ".join(where)
        total_row = await state.db.fetchone(
            f"SELECT COUNT(*) AS c FROM request_logs l WHERE {where_sql}", tuple(params)
        )
        rows = await state.db.fetchall(
            f"SELECT l.*, t.name AS token_name, k.key AS key_raw "
            f"FROM request_logs l "
            f"LEFT JOIN access_tokens t ON l.token_id = t.id "
            f"LEFT JOIN tavily_keys k ON l.tavily_key_id = k.id "
            f"WHERE {where_sql} ORDER BY l.id DESC LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
        items = [
            {
                "id": r["id"],
                "ts": r["ts"],
                "token_name": r["token_name"] or (f"#{r['token_id']}" if r["token_id"] else "-"),
                "tool": r["tool"],
                "query": r["query"],
                "tavily_key": mask_key(r["key_raw"]) if r["key_raw"] else "-",
                "status": r["status"],
                "http_status": r["http_status"],
                "credits": r["credits"],
                "latency_ms": r["latency_ms"],
                "error_detail": r["error_detail"],
                "request_id": r["request_id"],
                "client_ip": r["client_ip"] or "-",
            }
            for r in rows
        ]
        return JSONResponse({"total": total_row["c"] if total_row else 0, "items": items})

    # -- settings (公告 / 站名 / 图标 / 密码) ------------------------------------

    async def public_info(request: Request) -> Response:
        state = get_state()
        site_name = await state.db.get_setting("site_name") or "Tavily Pool"
        announcement = await state.db.get_setting("announcement") or ""
        updated = await state.db.get_setting("announcement_updated_at")
        return JSONResponse(
            {
                "site_name": site_name,
                "announcement": announcement or None,
                "announcement_updated_at": float(updated) if updated else None,
            }
        )

    async def site_icon(request: Request) -> Response:
        state = get_state()
        icon_path = state.config.data_dir / "site_icon.bin"
        if icon_path.exists():
            content_type = await state.db.get_setting("icon_content_type") or "image/png"
            return FileResponse(
                icon_path,
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=300"},
            )
        if DEFAULT_ICON_PATH.exists():
            return FileResponse(
                DEFAULT_ICON_PATH,
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        return JSONResponse({"error": "icon not found"}, status_code=404)

    async def settings_get(request: Request) -> Response:
        state = get_state()
        site_name = await state.db.get_setting("site_name") or "Tavily Pool"
        announcement = await state.db.get_setting("announcement") or ""
        updated = await state.db.get_setting("announcement_updated_at")
        cfg = await read_alert_settings(state.db)
        return JSONResponse(
            {
                "site_name": site_name,
                "announcement": announcement,
                "announcement_updated_at": float(updated) if updated else None,
                "has_custom_icon": (state.config.data_dir / "site_icon.bin").exists(),
                "alert": {
                    "channel": cfg["alert_channel"],
                    "webhook_url": cfg["alert_webhook_url"],
                    "webhook_secret": cfg["alert_webhook_secret"],
                    "email_smtp_host": cfg["alert_email_smtp_host"],
                    "email_smtp_port": int(cfg["alert_email_smtp_port"] or 465),
                    "email_smtp_ssl": cfg["alert_email_use_ssl"] != "0",
                    "email_username": cfg["alert_email_username"],
                    "email_password": cfg["alert_email_password"],
                    "email_from": cfg["alert_email_from"],
                    "email_to": cfg["alert_email_to"],
                    "on_key_disabled": cfg["alert_on_key_disabled"] == "1",
                    "on_key_exhausted": cfg["alert_on_key_exhausted"] == "1",
                    "on_pool_exhausted": cfg["alert_on_pool_exhausted"] == "1",
                    "pool_min_active": int(float(cfg["alert_pool_min_active"] or 0)),
                    "pool_min_remaining": float(cfg["alert_pool_min_remaining"] or 0),
                },
            }
        )

    async def settings_update(request: Request) -> Response:
        state = get_state()
        data = await read_json(request)
        if "site_name" in data:
            name = str(data["site_name"]).strip()[:40]
            await state.db.set_setting("site_name", name or "Tavily Pool")
        if "announcement" in data:
            text = str(data["announcement"]).strip()[:2000]
            await state.db.set_setting("announcement", text)
            await state.db.set_setting("announcement_updated_at", str(time.time()))
        # -- alert configuration -------------------------------------------------
        if "alert_channel" in data:
            channel = str(data["alert_channel"]).strip()
            if channel not in ("", "feishu", "wecom", "dingtalk", "generic", "email"):
                return JSONResponse({"error": "未知告警渠道"}, status_code=400)
            await state.db.set_setting("alert_channel", channel)
        if "alert_webhook_url" in data:
            await state.db.set_setting("alert_webhook_url", str(data["alert_webhook_url"]).strip()[:500])
        if "alert_webhook_secret" in data:
            await state.db.set_setting("alert_webhook_secret", str(data["alert_webhook_secret"]).strip()[:200])
        # email channel (SMTP): host / port / ssl / mailbox / auth code / sender / recipients
        if "alert_email_smtp_host" in data:
            await state.db.set_setting("alert_email_smtp_host", str(data["alert_email_smtp_host"]).strip()[:200])
        if "alert_email_smtp_port" in data:
            try:
                port = int(data["alert_email_smtp_port"])
            except (TypeError, ValueError):
                port = 465
            await state.db.set_setting("alert_email_smtp_port", str(min(65535, max(1, port))))
        if "alert_email_smtp_ssl" in data:
            await state.db.set_setting("alert_email_use_ssl", "1" if data["alert_email_smtp_ssl"] else "0")
        for key in (
            "alert_email_username",
            "alert_email_password",
            "alert_email_from",
            "alert_email_to",
        ):
            if key in data:
                await state.db.set_setting(key, str(data[key]).strip()[:500])
        for key in ("alert_on_key_disabled", "alert_on_key_exhausted", "alert_on_pool_exhausted"):
            if key in data:
                await state.db.set_setting(key, "1" if data[key] else "0")
        for key, cast in (("alert_pool_min_active", int), ("alert_pool_min_remaining", float)):
            if key in data:
                try:
                    value = max(0, cast(data[key] or 0))
                except (TypeError, ValueError):
                    value = 0
                await state.db.set_setting(key, str(value))
        if state.alerts is not None:
            state.alerts.invalidate_cache()
        return JSONResponse({"ok": True})

    async def settings_alert_test(request: Request) -> Response:
        state = get_state()
        if state.alerts is None:
            return JSONResponse({"ok": False, "error": "告警模块未启用"}, status_code=500)
        ok, error = await state.alerts.send_test()
        return JSONResponse({"ok": ok, "error": error or None})

    async def settings_icon_upload(request: Request) -> Response:
        state = get_state()
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type not in ALLOWED_ICON_TYPES:
            return JSONResponse(
                {"error": f"不支持的图标格式({content_type or '未知'}),请使用 PNG / JPEG / SVG / WebP"},
                status_code=400,
            )
        body = await request.body()
        if not body:
            return JSONResponse({"error": "空的图标文件"}, status_code=400)
        if len(body) > 1024 * 1024:
            return JSONResponse({"error": "图标不能超过 1MB"}, status_code=413)
        (state.config.data_dir / "site_icon.bin").write_bytes(body)
        await state.db.set_setting("icon_content_type", content_type)
        logger.info("site icon updated (%d bytes, %s)", len(body), content_type)
        return JSONResponse({"ok": True, "size": len(body)})

    async def settings_icon_delete(request: Request) -> Response:
        state = get_state()
        icon_path = state.config.data_dir / "site_icon.bin"
        if icon_path.exists():
            icon_path.unlink()
        await state.db.delete_setting("icon_content_type")
        return JSONResponse({"ok": True})

    async def settings_password(request: Request) -> Response:
        state = get_state()
        data = await read_json(request)
        current = str(data.get("current_password", ""))
        new_password = str(data.get("new_password", ""))
        if len(new_password) < 8:
            return JSONResponse({"error": "新密码至少 8 位"}, status_code=400)
        stored_hash = await state.db.get_setting("admin_password_hash")
        if stored_hash:
            current_ok = _verify_password(current, stored_hash)
        else:
            current_ok = secrets.compare_digest(current, config.admin_password)
        if not current_ok:
            return JSONResponse({"error": "当前密码不正确"}, status_code=401)
        await state.db.set_setting("admin_password_hash", _hash_password(new_password))
        logger.info("admin password changed")
        return JSONResponse({"ok": True})

    return [
        Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/session", admin(session_check), methods=["GET"]),
        Route("/api/overview", admin(overview), methods=["GET"]),
        Route("/api/stats/daily", admin(stats_daily), methods=["GET"]),
        Route("/api/keys", admin(keys_list), methods=["GET"]),
        Route("/api/keys", admin(keys_create), methods=["POST"]),
        Route("/api/keys/sync-all", admin(keys_sync_all), methods=["POST"]),
        Route("/api/keys/{key_id:int}", admin(key_patch), methods=["PATCH"]),
        Route("/api/keys/{key_id:int}", admin(key_delete), methods=["DELETE"]),
        Route("/api/keys/{key_id:int}/test", admin(key_test), methods=["POST"]),
        Route("/api/tokens", admin(tokens_list), methods=["GET"]),
        Route("/api/tokens", admin(tokens_create), methods=["POST"]),
        Route("/api/tokens/{token_id:int}", admin(token_patch), methods=["PATCH"]),
        Route("/api/tokens/{token_id:int}", admin(token_delete), methods=["DELETE"]),
        Route("/api/tokens/{token_id:int}/reveal", admin(token_reveal), methods=["GET"]),
        Route("/api/logs", admin(logs_list), methods=["GET"]),
        Route("/api/public-info", public_info, methods=["GET"]),
        Route("/site-icon", site_icon, methods=["GET"]),
        Route("/api/settings", admin(settings_get), methods=["GET"]),
        Route("/api/settings", admin(settings_update), methods=["PUT"]),
        Route("/api/settings/alert-test", admin(settings_alert_test), methods=["POST"]),
        Route("/api/settings/icon", admin(settings_icon_upload), methods=["POST"]),
        Route("/api/settings/icon", admin(settings_icon_delete), methods=["DELETE"]),
        Route("/api/settings/password", admin(settings_password), methods=["POST"]),
    ]
