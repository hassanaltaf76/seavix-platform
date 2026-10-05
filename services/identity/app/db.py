"""Async SQLAlchemy engine/session for the identity service."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import database_url

engine = create_async_engine(database_url())
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
