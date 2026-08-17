"""Central configuration loaded from environment variables."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FREE_PLAN_LIMIT = 1000.0
SESSION_COOKIE = "tpm_admin"
SESSION_MAX_AGE = 7 * 86400


@dataclass(frozen=True)
class Config:
    admin_password: str
    session_secret: str
    data_dir: Path
    host: str
    port: int
    cooldown_seconds: float
    usage_sync_interval_hours: float
    log_retention_days: int
    default_token_rpm: int
    max_retries: int
    character_limit: int
    cookie_secure: bool
    dev_mode: bool


def load_config() -> Config:
    dev_mode = os.environ.get("TAVILY_POOL_DEV", "") == "1"
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if not admin_password:
        if dev_mode:
            admin_password = "admin"
        else:
            raise RuntimeError(
                "ADMIN_PASSWORD is required. Set it in the environment, or use "
                "TAVILY_POOL_DEV=1 for local development (default password 'admin')."
            )

    session_secret = os.environ.get("SESSION_SECRET", "")
    if not session_secret:
        secret_path = data_dir / "session_secret"
        if secret_path.exists():
            session_secret = secret_path.read_text(encoding="utf-8").strip()
        else:
            session_secret = secrets.token_urlsafe(48)
            secret_path.write_text(session_secret, encoding="utf-8")

    return Config(
        admin_password=admin_password,
        session_secret=session_secret,
        data_dir=data_dir,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        cooldown_seconds=float(os.environ.get("KEYPOOL_COOLDOWN_SECONDS", "60")),
        usage_sync_interval_hours=float(os.environ.get("USAGE_SYNC_INTERVAL_HOURS", "6")),
        log_retention_days=int(os.environ.get("LOG_RETENTION_DAYS", "30")),
        default_token_rpm=int(os.environ.get("DEFAULT_TOKEN_RPM", "30")),
        max_retries=int(os.environ.get("KEYPOOL_MAX_RETRIES", "4")),
        character_limit=int(os.environ.get("CHARACTER_LIMIT", "25000")),
        cookie_secure=os.environ.get("COOKIE_SECURE", "0" if dev_mode else "1") == "1",
        dev_mode=dev_mode,
    )
