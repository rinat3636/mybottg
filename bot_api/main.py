"""FastAPI application entry point.

- Health check GET /health (no 307 redirect — redirect_slashes=False)
- Telegram webhook
- YooKassa webhook
- Background queue worker
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    logger.info("Starting up...")

    # Validate environment configuration
    from shared.config import validate_settings
    validate_settings()

    # Optional: Sentry error tracking
    import os
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if dsn:
        try:
            import sentry_sdk  # type: ignore
            sentry_sdk.init(
                dsn=dsn,
                traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")),
                environment=os.getenv("SENTRY_ENV", os.getenv("ENV", "production")),
            )
            logger.info("Sentry enabled")
        except Exception:
            logger.exception("Failed to initialize Sentry")

    # Create DB tables
    from shared.database import create_tables
    await create_tables()
    logger.info("Database tables ready")

    # Initialize Redis
    from shared.redis_client import get_redis, close_redis
    await get_redis()
    logger.info("Redis connected")

    # Create bot
    from bot_api.bot import create_bot, setup_webhook, shutdown_bot
    await create_bot()
    await setup_webhook()
    logger.info("Bot initialized")

    # Start payment reconciler (missed webhooks fallback) - only if YooKassa is configured
    from services.payment_reconcile import start_reconciler, stop_reconciler
    from shared.config import settings as config_settings
    if config_settings.YOOKASSA_SHOP_ID and config_settings.YOOKASSA_SECRET_KEY:
        await start_reconciler()
        logger.info("Payment reconciler started")
    else:
        logger.info("Payment reconciler skipped (YooKassa not configured)")

    # Start queue worker
    from services.queue_worker import start_worker, stop_worker
    await start_worker()
    logger.info("Queue worker started")

    yield

    # Shutdown
    logger.info("Shutting down...")
    await stop_worker()
    if config_settings.YOOKASSA_SHOP_ID and config_settings.YOOKASSA_SECRET_KEY:
        await stop_reconciler()
    await shutdown_bot()
    await close_redis()
    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Nano Banana Bot",
    version="3.0.0",
    lifespan=lifespan,
    redirect_slashes=False,
)


# ---------------------------------------------------------------------------
# Basic request size guard for webhook endpoints
# ---------------------------------------------------------------------------

MAX_WEBHOOK_BODY_BYTES = int(__import__("os").getenv("MAX_WEBHOOK_BODY_BYTES", str(1_000_000)))


@app.middleware("http")
async def limit_webhook_body_size(request, call_next):
    try:
        if request.method in ("POST", "PUT", "PATCH") and request.url.path.startswith(("/webhook/telegram/", "/yookassa/webhook/")):
            cl = request.headers.get("content-length")
            if cl and int(cl) > MAX_WEBHOOK_BODY_BYTES:
                return Response(status_code=413)
    except Exception:
        pass
    return await call_next(request)


# ---------------------------------------------------------------------------
# Health check (GET /health and GET /health/ — no trailing slash redirect)
# ---------------------------------------------------------------------------

@app.get("/health")
@app.get("/health/")
async def health_check() -> Response:
    """Health check endpoint for Railway / Docker.

    Both /health and /health/ return the same response to avoid
    307 redirects in any Railway / reverse-proxy configuration.
    """
    return Response(content='{"status":"ok"}', media_type="application/json")


# ---------------------------------------------------------------------------
# Include routers
# ---------------------------------------------------------------------------

from bot_api.webhooks.telegram import router as telegram_router
from bot_api.webhooks.yookassa import router as yookassa_router

app.include_router(telegram_router)
app.include_router(yookassa_router)
