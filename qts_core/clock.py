"""Exchange-time discipline.

This machine runs on Asia/Riyadh (+7h/+8h vs New York, no DST). Every naive
``datetime.now()`` in trading code is therefore a live bug — the legacy
scanner computed DTE one day short for ~7 hours of every day (finding
TZ-local-now). Rules here:

- All datetimes are timezone-aware. Naive input raises immediately.
- Exchange time is America/New_York, obtained via zoneinfo (DST-correct).
- Session facts (holidays, half-days, actual close) come from the XNYS
  calendar in ``exchange_calendars`` — never assumed.
- ``TradingClock`` is injectable: production wires a real UTC now-source,
  tests inject frozen instants. Nothing else may consult the wall clock.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from functools import lru_cache
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:  # pragma: no cover
    from exchange_calendars import ExchangeCalendar

NY = ZoneInfo("America/New_York")
UTC = dt.UTC


class NaiveDatetimeError(ValueError):
    """A naive datetime crossed into trading code."""


def require_aware(d: dt.datetime) -> dt.datetime:
    if d.tzinfo is None or d.tzinfo.utcoffset(d) is None:
        raise NaiveDatetimeError(f"naive datetime rejected: {d!r}")
    return d


def to_et(d: dt.datetime) -> dt.datetime:
    return require_aware(d).astimezone(NY)


@lru_cache(maxsize=1)
def xnys() -> "ExchangeCalendar":
    """NYSE calendar, loaded once (bundled data — no network)."""
    import exchange_calendars as xcals

    return xcals.get_calendar("XNYS")


def is_trading_day(day: dt.date) -> bool:
    return bool(xnys().is_session(day.isoformat()))


def session_close_et(day: dt.date) -> dt.datetime:
    """Actual close for a session (half-days close 13:00 ET, not 16:00)."""
    close = xnys().session_close(day.isoformat())
    return close.to_pydatetime().astimezone(NY)


def session_open_et(day: dt.date) -> dt.datetime:
    open_ = xnys().session_open(day.isoformat())
    return open_.to_pydatetime().astimezone(NY)


def in_time_window(now: dt.datetime, start: dt.time, end: dt.time) -> bool:
    """Is `now` inside [start, end) measured in exchange time-of-day?"""
    t = to_et(now).time()
    return start <= t < end


def effective_force_flat_et(
    day: dt.date, configured: dt.time, close_buffer: dt.timedelta
) -> dt.datetime:
    """Force-flat instant for a session (finding BLOCK-no-flatten-before-close).

    The earlier of: the configured wall time, or (actual close - buffer).
    On a 13:00 half-day close a 15:30 config would otherwise fire after the
    market is gone — and an ITM 0DTE runner would be auto-exercised into
    ~24x the account's size in stock.
    """
    close = session_close_et(day)
    configured_dt = dt.datetime.combine(day, configured, tzinfo=NY)
    return min(configured_dt, close - close_buffer)


class TradingClock:
    """The single source of 'now'. Inject a frozen now_fn in tests."""

    def __init__(self, now_fn: Callable[[], dt.datetime]) -> None:
        self._now_fn = now_fn

    @classmethod
    def system(cls) -> "TradingClock":
        # The one sanctioned wall-clock read in the codebase.
        return cls(lambda: dt.datetime.now(tz=UTC))  # noqa: DTZ005 - tz IS provided

    def now_utc(self) -> dt.datetime:
        return require_aware(self._now_fn()).astimezone(UTC)

    def now_et(self) -> dt.datetime:
        return self.now_utc().astimezone(NY)

    def session_date(self) -> dt.date:
        """The exchange-local date. NEVER the machine-local date."""
        return self.now_et().date()
