"""Web app configuration — local-dev defaults, everything overridable by env."""
from __future__ import annotations

import os
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parents[2]


def port() -> int:
    return int(os.environ.get("PORT", "8000"))


def identity_base_url() -> str:
    return os.environ.get("IDENTITY_BASE_URL", "http://localhost:8001")


def work_base_url() -> str:
    return os.environ.get("WORK_SERVICE_URL", "http://localhost:8004")


def blob_root() -> Path:
    """Shared with the identity service's LocalBlobStore root.

    Both services resolve <platform root>/var/blobs so logo uploads and
    dev media serving see the same files. var/ is gitignored.
    """
    return Path(os.environ.get("SEAVIX_BLOB_ROOT", PLATFORM_ROOT / "var" / "blobs"))
