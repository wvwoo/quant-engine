"""Disk cache + client-side rate limiting for market-data calls.

The mandate asked for both, and the legacy scanner had neither: it fired ~20
unthrottled Yahoo requests per run and refetched a year of daily history every
time (finding no-rate-limit-no-cache). yfinance raises YFRateLimitError when
Yahoo throttles, so an unthrottled loop is a self-inflicted outage.

Design notes:
- Cache keys include the session date and a coarse time bucket, so intraday
  quotes expire naturally while daily history is reused all day.
- Writes are atomic (temp file + os.replace) so a crash cannot leave a
  half-written JSON that poisons the next run.
- The limiter is per-process and monotonic-clock based; it never sleeps
  negative and never consults the wall clock (which this codebase bans).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path("qts_v8/state/cache")


class RateLimiter:
    """Minimum interval between calls, thread-safe, monotonic."""

    def __init__(self, min_interval_s: float, sleep: Callable[[float], None] | None = None) -> None:
        if min_interval_s < 0:
            raise ValueError(f"min_interval_s must be >= 0: {min_interval_s}")
        self._min = min_interval_s
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._last: float | None = None

    def acquire(self, now: float | None = None) -> float:
        """Block until the minimum spacing has elapsed. Returns seconds waited."""
        with self._lock:
            t = time.monotonic() if now is None else now
            if self._last is None:
                self._last = t
                return 0.0
            elapsed = t - self._last
            wait = self._min - elapsed
            if wait > 0:
                self._sleep(wait)
                self._last = t + wait
                return wait
            self._last = t
            return 0.0


class DiskCache:
    """Tiny JSON cache. Keys are caller-supplied and must be filename-safe."""

    def __init__(self, directory: Path | str = DEFAULT_CACHE_DIR) -> None:
        self.dir = Path(directory)

    def _path(self, key: str) -> Path:
        # Dots are NOT allowed through: a key like "a/../b" would otherwise
        # keep its ".." segment in the filename. Only the suffix we append
        # may contain a dot, so the result is always a plain child of self.dir.
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
        return self.dir / f"{safe}.json"

    def get(self, key: str) -> Any | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None  # corrupt entry behaves as a miss, never as a crash

    def put(self, key: str, value: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self._path(key)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(value, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)  # atomic within the same filesystem
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def get_or_fetch(self, key: str, fetch: Callable[[], Any]) -> tuple[Any, bool]:
        """Returns (value, was_cached)."""
        hit = self.get(key)
        if hit is not None:
            return hit, True
        value = fetch()
        self.put(key, value)
        return value, False
