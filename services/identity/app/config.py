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


# ------------------------------------------------------------- authN (D1)

def otp_ttl_seconds() -> int:
    return int(os.environ.get("SEAVIX_OTP_TTL_SECONDS", "600"))  # 10 min


def otp_pepper() -> str:
    """Server-side pepper for the at-rest OTP hash.

    Dev default is public and intentionally weak — prod must inject a real
    secret via the SecretsProvider/env (SEAVIX_OTP_PEPPER).
    """
    return os.environ.get("SEAVIX_OTP_PEPPER", "dev-otp-pepper")


def otp_attempt_limit() -> int:
    return int(os.environ.get("SEAVIX_OTP_ATTEMPT_LIMIT", "5"))


def session_ttl_seconds() -> int:
    return int(os.environ.get("SEAVIX_SESSION_TTL_SECONDS", str(12 * 3600)))


def dev_otp_endpoint_enabled() -> bool:
    """Dev-only last-code endpoint (D1 dev readability fallback).

    Off by default; enabled explicitly in dev/test via
    SEAVIX_DEV_OTP_ENDPOINT=1. Never enable in prod.
    """
    return os.environ.get("SEAVIX_DEV_OTP_ENDPOINT", "") == "1"


load_dotenv()
