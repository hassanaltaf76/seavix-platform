"""Identity service configuration — all values from env with local-dev defaults."""
from __future__ import annotations

import os
from pathlib import Path

_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
_DOTENV_LOADED = False


def load_dotenv() -> None:
    """Load the platform-root .env (setdefault semantics — real env wins).

    Lets `uvicorn app.main:app` (which doesn't auto-load .env) see OPENFGA_*
    and FIREBASE_* values without shell exports.
    """
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
    return int(os.environ.get("PORT", "8001"))


def database_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://seavix:dev@localhost:5432/seavix_identity",
    )


def blob_root() -> Path:
    """Shared with the web app's dev media route (same var/blobs)."""
    return Path(os.environ.get("SEAVIX_BLOB_ROOT", _PLATFORM_ROOT / "var" / "blobs"))


load_dotenv()
