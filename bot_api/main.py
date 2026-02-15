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

    # Start queue worker
    from services.queue_worker import start_worker, stop_worker
    await start_worker()
    logger.info("Queue worker started")

    yield

    # Shutdown
    logger.info("Shutting down...")
    await stop_worker()
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
