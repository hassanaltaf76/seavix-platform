"""Identity service configuration — all values from env with local-dev defaults."""
from __future__ import annotations

import os


def port() -> int:
    return int(os.environ.get("PORT", "8001"))


def database_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://seavix:dev@localhost:5432/seavix_identity",
    )
