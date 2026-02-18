"""Application configuration loaded from environment variables.

All settings are read once at import time.  Missing **required** variables
cause an immediate ``SystemExit`` with a clear error message so that
Railway / Docker logs show exactly what is wrong.

Optional variables produce a warning but do not block startup.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Credit packages: amount_rub → credits (1₽ = 1 credit, no bonus)
# ---------------------------------------------------------------------------

CREDIT_PACKAGES: Dict[int, int] = {
    100: 100,
    200: 200,
    300: 300,
    500: 500,
}

# ---------------------------------------------------------------------------
# Generation cost in credits
# ---------------------------------------------------------------------------

GENERATION_COST: Dict[str, int] = {
    "nano_banana_pro": 19,
    "riverflow_pro": 45,
    "flux_2_pro": 9,
    "kling_video_5s": 70,
    "kling_video_10s": 140,
}

# ---------------------------------------------------------------------------
# Rate-limit defaults
# ---------------------------------------------------------------------------

DEFAULT_CMD_RATE_LIMIT: int = 5        # commands per minute
DEFAULT_CMD_RATE_WINDOW: int = 60      # seconds
DEFAULT_MEDIA_RATE_LIMIT: int = 2      # media per minute
DEFAULT_MEDIA_RATE_WINDOW: int = 60    # seconds

# ---------------------------------------------------------------------------
# Queue & generation limits (defaults tuned for ~5k users)
# ---------------------------------------------------------------------------

# Max queued tasks per user (not counting the active processing task)
DEFAULT_MAX_QUEUED_TASKS_PER_USER: int = 3

# Global queue hard limit to protect workers/Redis
DEFAULT_MAX_GLOBAL_QUEUE_SIZE: int = 500

# Per-user generation lock TTL (seconds)
DEFAULT_GENERATION_LOCK_TTL: int = 300

# Overall generation timeout in the worker (seconds)
DEFAULT_GENERATION_TIMEOUT: int = 200


@dataclass(frozen=True)
class Config:
    """Immutable application configuration."""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_WEBHOOK_URL: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""

    # Replicate
    REPLICATE_API_TOKEN: str = ""

    # Database
    DATABASE_URL: str = ""
    DB_POOL_SIZE: int = 3
    DB_MAX_OVERFLOW: int = 2

    # Redis
    REDIS_URL: str = "redis://localhost:6379"
    REDIS_SSL: bool = False

    # YooKassa
    YOOKASSA_SHOP_ID: str = ""
    YOOKASSA_SECRET_KEY: str = ""
    YOOKASSA_WEBHOOK_SECRET: str = ""

    # Admin IDs (list of telegram user ids)
    ADMIN_IDS: List[int] = field(default_factory=list)

    # Support (optional): direct contact link, e.g. https://t.me/yourname
    SUPPORT_TG_URL: str = ""

    # Limits
    MAX_QUEUED_TASKS_PER_USER: int = DEFAULT_MAX_QUEUED_TASKS_PER_USER
    MAX_GLOBAL_QUEUE_SIZE: int = DEFAULT_MAX_GLOBAL_QUEUE_SIZE
    GENERATION_LOCK_TTL: int = DEFAULT_GENERATION_LOCK_TTL
    GENERATION_TIMEOUT: int = DEFAULT_GENERATION_TIMEOUT

    # Server
    PORT: int = 8080

    # --------------- derived properties ---------------

    @property
    def async_database_url(self) -> str:
        """Convert DATABASE_URL to asyncpg-compatible format."""
        url = self.DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif not url.startswith("postgresql+asyncpg://"):
            url = f"postgresql+asyncpg://{url}"
        return url

    @property
    def redis_ssl_enabled(self) -> bool:
        """Whether Redis connection should use SSL/TLS."""
        return self.REDIS_SSL or self.REDIS_URL.startswith("rediss://")

    @property
    def webhook_path(self) -> str:
        return f"/webhook/telegram/{self.TELEGRAM_WEBHOOK_SECRET}"

    @property
    def full_webhook_url(self) -> str:
        base = self.TELEGRAM_WEBHOOK_URL.rstrip("/")
        return f"{base}{self.webhook_path}"


# ---------------------------------------------------------------------------
# ENV validation
# ---------------------------------------------------------------------------

# Railway (and some add-ons) may expose slightly different variable names.
# We accept a few common aliases to make deployments smoother.
_ENV_ALIASES = {
    "DATABASE_URL": ["DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL", "PGDATABASE_URL"],
    "REDIS_URL": ["REDIS_URL", "REDIS_PRIVATE_URL", "REDIS_PUBLIC_URL"],
}

_REQUIRED_VARS: List[Tuple[str, str]] = [
    ("TELEGRAM_BOT_TOKEN", "Telegram bot token from @BotFather"),
    ("DATABASE_URL", "PostgreSQL connection string (DATABASE_URL / POSTGRES_URL / POSTGRESQL_URL)"),
    ("REDIS_URL", "Redis connection string (REDIS_URL / REDIS_PRIVATE_URL / REDIS_PUBLIC_URL)"),
]

_OPTIONAL_VARS: List[Tuple[str, str]] = [
    ("YOOKASSA_SHOP_ID", "YooKassa shop ID — payments will not work without it"),
    ("YOOKASSA_SECRET_KEY", "YooKassa secret key — payments will not work without it"),
    ("TELEGRAM_WEBHOOK_URL", "Public base URL for webhooks — webhook setup will be skipped"),
]


def _env_first(*names: str, default: str = "") -> str:
    """Return first non-empty env var among names."""
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return default


def _check_env(database_url: str, redis_url: str, webhook_url: str, webhook_secret: str) -> None:
    """Validate required ENV vars, warn about optional ones."""
    missing: List[str] = []

    if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        missing.append("  • TELEGRAM_BOT_TOKEN — Telegram bot token from @BotFather")

    if not database_url:
        missing.append("  • DATABASE_URL — PostgreSQL connection string (or POSTGRES_URL / POSTGRESQL_URL)")

    if not redis_url:
        missing.append("  • REDIS_URL — Redis connection string (or REDIS_PRIVATE_URL / REDIS_PUBLIC_URL)")

    if not os.getenv("REPLICATE_API_TOKEN", "").strip():
        missing.append("  • REPLICATE_API_TOKEN — Replicate API token (required for image generation)")

    # If webhook is enabled (URL provided), secret MUST be set and must not be a default.
    if webhook_url and (not webhook_secret or webhook_secret == "changeme"):
        missing.append("  • TELEGRAM_WEBHOOK_SECRET — set a strong secret when TELEGRAM_WEBHOOK_URL is set")

    # If payments are enabled, webhook secret MUST be set (otherwise users will pay but credits won't accrue).
    yookassa_enabled = bool(os.getenv("YOOKASSA_SHOP_ID", "").strip() and os.getenv("YOOKASSA_SECRET_KEY", "").strip())
    if yookassa_enabled and not os.getenv("YOOKASSA_WEBHOOK_SECRET", "").strip():
        missing.append("  • YOOKASSA_WEBHOOK_SECRET — required when YooKassa credentials are set")

    if missing:
        msg = (
            "FATAL: missing/invalid environment variables:\n"
            + "\n".join(missing)
            + "\nSet them and restart."
        )
        logger.critical(msg)
        print(msg, file=sys.stderr)
        raise RuntimeError(msg)

    # Optional warnings
    for var, desc in _OPTIONAL_VARS:
        if not os.getenv(var, "").strip():
            logger.warning("Optional ENV not set: %s — %s", var, desc)


def load_config(*, validate: bool = True) -> Config:
    """Load configuration from environment variables."""
    database_url = _env_first(*_ENV_ALIASES["DATABASE_URL"])
    redis_url = _env_first(*_ENV_ALIASES["REDIS_URL"], default="redis://localhost:6379")

    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip() or "changeme"
    if validate:
        _check_env(database_url, redis_url, webhook_url, webhook_secret)

    admin_ids_raw = os.getenv("ADMIN_IDS", "")
    admin_ids: List[int] = []
    if admin_ids_raw.strip():
        for part in admin_ids_raw.split(","):
            part = part.strip()
            if part.isdigit():
                admin_ids.append(int(part))

    redis_ssl_env = os.getenv("REDIS_SSL", "").strip().lower()
    redis_ssl = redis_ssl_env in ("true", "1", "yes")

    return Config(
        TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        TELEGRAM_WEBHOOK_URL=webhook_url,
        TELEGRAM_WEBHOOK_SECRET=webhook_secret,
        REPLICATE_API_TOKEN=os.getenv("REPLICATE_API_TOKEN", "").strip(),
        DATABASE_URL=database_url,
        DB_POOL_SIZE=int(os.getenv("DB_POOL_SIZE", "3")),
        DB_MAX_OVERFLOW=int(os.getenv("DB_MAX_OVERFLOW", "2")),
        REDIS_URL=redis_url,
        REDIS_SSL=redis_ssl,
        YOOKASSA_SHOP_ID=os.getenv("YOOKASSA_SHOP_ID", "").strip(),
        YOOKASSA_SECRET_KEY=os.getenv("YOOKASSA_SECRET_KEY", "").strip(),
        YOOKASSA_WEBHOOK_SECRET=os.getenv("YOOKASSA_WEBHOOK_SECRET", "").strip(),
        ADMIN_IDS=admin_ids,
        SUPPORT_TG_URL=os.getenv("SUPPORT_TG_URL", "").strip(),
        MAX_QUEUED_TASKS_PER_USER=int(os.getenv("MAX_QUEUED_TASKS_PER_USER", str(DEFAULT_MAX_QUEUED_TASKS_PER_USER))),
        MAX_GLOBAL_QUEUE_SIZE=int(os.getenv("MAX_GLOBAL_QUEUE_SIZE", str(DEFAULT_MAX_GLOBAL_QUEUE_SIZE))),
        GENERATION_LOCK_TTL=int(os.getenv("GENERATION_LOCK_TTL", str(DEFAULT_GENERATION_LOCK_TTL))),
        GENERATION_TIMEOUT=int(os.getenv("GENERATION_TIMEOUT", str(DEFAULT_GENERATION_TIMEOUT))),
        PORT=int(os.getenv("PORT", "8080")),
    )


# Singleton instance (loaded without validation to avoid import-time crashes)
settings = load_config(validate=False)


def validate_settings() -> None:
    """Validate ENV and loaded settings at application startup.

    Call this once from the FastAPI lifespan before initializing external services.
    """
    _check_env(settings.DATABASE_URL, settings.REDIS_URL, settings.TELEGRAM_WEBHOOK_URL, settings.TELEGRAM_WEBHOOK_SECRET)
