"""Database bootstrap helpers for the first service milestone."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ruperto.config import Settings


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    """Shared declarative base for ORM models."""


class StoreProfile(Base):
    """Persist the minimal store metadata required by the assistant."""

    __tablename__ = "store_profile"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    store_name: Mapped[str] = mapped_column(String(length=120))
    bot_name: Mapped[str] = mapped_column(String(length=120))
    store_location: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    store_description: Mapped[str] = mapped_column(String(length=500))
    assistant_personality: Mapped[str] = mapped_column(String(length=255))
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


@dataclass(slots=True)
class DatabaseRuntime:
    """Database primitives shared across the FastAPI app lifespan."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


def create_database_runtime(settings: Settings) -> DatabaseRuntime:
    """Create the async engine and session factory for the configured database."""
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return DatabaseRuntime(engine=engine, session_factory=session_factory)


async def init_database(*, settings: Settings, runtime: DatabaseRuntime | None = None) -> DatabaseRuntime:
    """Create tables and ensure the store profile exists.

    If a runtime is not provided, this function creates and returns one so the
    caller can keep using it afterwards.
    """

    active_runtime = runtime or create_database_runtime(settings)
    async with active_runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with active_runtime.session_factory() as session:
        result = await session.execute(select(StoreProfile).where(StoreProfile.id == 1))
        profile = result.scalar_one_or_none()
        if profile is None:
            session.add(
                StoreProfile(
                    id=1,
                    store_name=settings.store_name,
                    bot_name=settings.bot_name,
                    store_location=settings.store_location,
                    store_description=settings.store_description,
                    assistant_personality=settings.assistant_personality,
                )
            )
            await session.commit()
    return active_runtime


async def ping_database(runtime: DatabaseRuntime) -> None:
    """Verify the configured database is reachable."""
    async with runtime.session_factory() as session:
        await session.execute(select(1))
