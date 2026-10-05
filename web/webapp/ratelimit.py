"""In-memory fixed-window rate limiter (dev-grade).

Resets on restart, per-process. Production TODO: shared store.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()


class RateLimiter:
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
