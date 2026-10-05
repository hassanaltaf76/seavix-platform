"""TEMPORARY local RFQ persistence.

The work service (services/work) does not exist yet — this is a deliberate,
clearly-documented shortcut: RFQs are stored in a local SQLite file under
var/ so the public intake flow works end-to-end. When services/work lands,
POST /p/{slug}/rfq must call its API instead and this module gets deleted.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .config import rfq_db_path

_lock = threading.Lock()

SURVEY_TYPES = ("pre-purchase", "condition", "bunker", "loading-discharging")
LOCATION_TYPES = ("terminal", "STS", "anchorage", "OPL", "repair-berth", "dry-dock")


def _connect() -> sqlite3.Connection:
    path: Path = rfq_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rfq (
            id TEXT PRIMARY KEY,
            org_slug TEXT NOT NULL,
            vessel_imo TEXT NOT NULL,
            survey_type TEXT NOT NULL,
            location_type TEXT NOT NULL,
            preferred_date TEXT,
            scope TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    return conn


def save_rfq(
    org_slug: str,
    vessel_imo: str,
    survey_type: str,
    location_type: str,
    preferred_date: str | None,
    scope: str | None,
) -> str:
    if survey_type not in SURVEY_TYPES:
        raise ValueError(f"survey_type must be one of {SURVEY_TYPES}")
    if location_type not in LOCATION_TYPES:
        raise ValueError(f"location_type must be one of {LOCATION_TYPES}")
    rfq_id = str(uuid.uuid4())
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO rfq VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rfq_id,
                org_slug,
                vessel_imo,
                survey_type,
                location_type,
                preferred_date,
                scope,
                time.time(),
            ),
        )
        conn.commit()
    return rfq_id


def list_rfqs(org_slug: str) -> list[dict]:
    cols = [
        "id", "org_slug", "vessel_imo", "survey_type", "location_type",
        "preferred_date", "scope", "created_at",
    ]
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rfq WHERE org_slug = ? ORDER BY created_at", (org_slug,)
        ).fetchall()
    return [dict(zip(cols, r, strict=True)) for r in rows]


class RateLimiter:
    """In-memory fixed-window rate limiter (5/hour/IP). Dev-grade: resets on
    restart and counts per process. Production TODO: shared store + captcha."""

    def __init__(self, limit: int = 5, window_seconds: int = 3600) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        with _lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True
