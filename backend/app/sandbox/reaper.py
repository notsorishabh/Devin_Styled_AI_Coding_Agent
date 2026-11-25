import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal
from app.models import Sandbox
from app.sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("creating", "running")


async def reap_expired_sandboxes(
    provider: SandboxProvider,
    *,
    db: AsyncSession | None = None,
    now: datetime | None = None,
    timeout_seconds: int | None = None,
) -> list[str]:
    """Destroy sandboxes older than the hard timeout. Returns destroyed ids."""
    cutoff = (now or datetime.now(UTC)) - timedelta(
        seconds=timeout_seconds if timeout_seconds is not None else settings.sandbox_timeout_seconds
    )
    own_session = db is None
    session = db or SessionLocal()
    destroyed: list[str] = []
    try:
        result = await session.execute(
            select(Sandbox).where(
                Sandbox.status.in_(ACTIVE_STATUSES),
                Sandbox.created_at <= cutoff,
            )
        )
        rows = list(result.scalars())
        for row in rows:
            logger.info("sandbox timeout auto-destroy id=%s", row.id)
            try:
                provider.destroy(row.id)
            except Exception:
                logger.exception("sandbox timeout destroy failed id=%s", row.id)
            row.status = "destroyed"
            row.destroyed_at = datetime.now(UTC)
            destroyed.append(row.id)
        if rows:
            await session.commit()
    finally:
        if own_session:
            await session.close()
    return destroyed


async def timeout_watchdog(provider: SandboxProvider, stop: asyncio.Event) -> None:
    interval = settings.sandbox_reap_interval_seconds
    while not stop.is_set():
        try:
            await reap_expired_sandboxes(provider)
        except Exception:
            logger.exception("sandbox watchdog tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
