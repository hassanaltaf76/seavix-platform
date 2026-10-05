"""Registry service configuration — env with local-dev defaults."""
from __future__ import annotations

import os
from pathlib import Path

_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
_DOTENV_LOADED = False


def load_dotenv() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    path = _PLATFORM_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def port() -> int:
    return int(os.environ.get("PORT", "8003"))


def database_url() -> str:
    # Service-specific var wins (tests for multiple Postgres services share one
    # pytest process, so a single shared DATABASE_URL env would collide).
    return os.environ.get(
        "REGISTRY_DATABASE_URL",
        os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://seavix:dev@localhost:5432/seavix_registry",
        ),
    )


load_dotenv()
