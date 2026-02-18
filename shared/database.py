"""SQLAlchemy async engine, session factory, and ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    ForeignKey,
    Index,
    UniqueConstraint,
    func,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from shared.config import settings

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Engine & session
# ---------------------------------------------------------------------------

def _build_db_engine_url_and_args() -> tuple[str, dict]:
    """Build DB URL + connect_args robustly for Railway deployments.

    Notes:
    - The SQLAlchemy asyncpg dialect forwards URL query params as kwargs to
      ``asyncpg.connect()``.
    - ``asyncpg.connect`` does **not** accept ``sslmode``. If ``sslmode`` is
      present in DATABASE_URL, the app will crash with:
      ``TypeError: connect() got an unexpected keyword argument 'sslmode'``.
    - Railway private Postgres (``*.railway.internal``) is typically plain TCP
      (no TLS), so forcing SSL can hang/cancel during startup.

    We therefore:
      1) Strip ``sslmode`` (and ``ssl``) from the URL.
      2) Translate those values into a proper boolean ``ssl`` connect arg.
      3) Default to ``ssl=False`` for Railway private domains unless the user
         explicitly requested SSL.
    """

    raw_url = settings.async_database_url
    parts = urlsplit(raw_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))

    connect_args: dict = {}

    sslmode = query.pop("sslmode", None)
    if sslmode:
        sslmode_l = str(sslmode).strip().lower()
        if sslmode_l in {"disable", "allow", "prefer"}:
            connect_args["ssl"] = False
        elif sslmode_l == "require":
            connect_args["ssl"] = True

    # Also accept `ssl=` in the URL but normalize to bool.
    ssl_q = query.pop("ssl", None)
    if ssl_q is not None and "ssl" not in connect_args:
        ssl_q_l = str(ssl_q).strip().lower()
        if ssl_q_l in {"0", "false", "no", "off", "disable"}:
            connect_args["ssl"] = False
        elif ssl_q_l in {"1", "true", "yes", "on", "require"}:
            connect_args["ssl"] = True

    host = parts.hostname or ""
    if "railway.internal" in host and "ssl" not in connect_args:
        connect_args["ssl"] = False

    cleaned = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )
    return cleaned, connect_args


_db_url, _connect_args = _build_db_engine_url_and_args()

engine = create_async_engine(
    _db_url,
    echo=False,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency / context-manager for obtaining a DB session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generate_ticket_id() -> str:
    """Generate a short unique ticket identifier for support messages."""
    return uuid.uuid4().hex[:8].upper()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    balance: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    referral_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    # referred_by stores telegram_id of the referrer — NO FK constraint
    referred_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )

    # Relationships
    generations: Mapped[list["Generation"]] = relationship(back_populates="user", lazy="selectin")
    payments: Mapped[list["Payment"]] = relationship(back_populates="user", lazy="selectin")
    support_messages: Mapped[list["SupportMessage"]] = relationship(back_populates="user", lazy="selectin")
    ledger_entries: Mapped[list["CreditLedger"]] = relationship(back_populates="user", lazy="noload")

    @staticmethod
    def generate_referral_code() -> str:
        return uuid.uuid4().hex[:10]


class Generation(Base):
    __tablename__ = "generations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    tariff: Mapped[str] = mapped_column(String(32), nullable=False, server_default="nano_banana_pro")
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cost: Mapped[int] = mapped_column(Integer, nullable=False, server_default="11")
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="generations")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    amount_rub: Mapped[int] = mapped_column(Integer, nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending")
    yookassa_payment_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="payments")

    __table_args__ = (
        UniqueConstraint("yookassa_payment_id", name="uq_payments_yookassa_payment_id"),
    )


class CreditLedger(Base):
    """Immutable journal of every credit change for audit / dispute resolution."""
    __tablename__ = "credit_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # positive = credit, negative = debit
    reason: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # payment / nano / pro / refund / referral / welcome
    reference_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )  # payment_id or generation request_id
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="ledger_entries")

    __table_args__ = (
        # Prevent accidental double-credits for the same payment/event
        UniqueConstraint("reason", "reference_id", name="uq_credit_ledger_reason_reference"),
        Index("ix_credit_ledger_user_created", "user_id", "created_at"),
    )


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(
        String(16), unique=True, nullable=False, index=True, default=generate_ticket_id
    )
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    admin_reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    replied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="support_messages")


# ---------------------------------------------------------------------------
# Table creation helper
# ---------------------------------------------------------------------------

async def create_tables() -> None:
    """Create all tables if they don't exist.

    Uses a non-blocking PostgreSQL advisory lock to prevent race conditions.
    If another instance is creating tables, this will skip gracefully.
    """
    from sqlalchemy import text
    import logging
    
    logger = logging.getLogger(__name__)

    async with engine.begin() as conn:
        # Try non-blocking advisory lock
        result = await conn.execute(text("SELECT pg_try_advisory_lock(12345)"))
        acquired = result.scalar()
        
        if not acquired:
            logger.info("Another instance is creating tables, skipping...")
            return
        
        try:
            logger.info("Creating database tables...")
            await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables created")
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(12345)"))
