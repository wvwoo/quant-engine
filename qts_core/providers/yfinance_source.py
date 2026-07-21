"""yfinance implementation of the chain/bars source (ADR-007 day-1 tier).

Thin I/O shell: everything testable lives in the pure row-conversion helpers;
the network methods only fetch and delegate. Known limits, stated honestly:
- No greeks (B1) — delta comes from qts_core.pricing (ADR-002).
- Yahoo's IV is unreliable; it is passed through ONLY as iv_hint.
- 15-20 min delayed quotes on the free tier; fine for paper, useless for HFT.
- auto_adjust is EXPLICITLY False: adjusted history back-applies future splits
  onto the past (finding LA-auto-adjust).

The in-progress bar problem: yfinance returns the currently-forming bar. Its
close time is in the future, so the MarketView firewall would reject it — the
converter drops any bar whose close is after `now` BEFORE the view is built.
"""

from __future__ import annotations

import datetime as dt
import math
import os
from typing import Any

from qts_core.cache import DiskCache, RateLimiter
from qts_core.clock import NY, to_et
from qts_core.models import Bar, MarketView, OptionQuote
from qts_core.money import MoneyError, cents_from_quote


def _clean_cents(value: object) -> int | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0:
        return None
    try:
        return int(cents_from_quote(round(f, 2)))
    except MoneyError:
        return None


def bars_from_rows(
    rows: list[tuple[dt.datetime, float, float, float, float, float]],
    *,
    now: dt.datetime,
    interval_min: int,
) -> list[Bar]:
    """(open_ts, o, h, l, c, v) rows -> completed, valid Bars only.

    Drops: NaN rows, zero/negative prices, and any bar still forming
    (close time > now). Row timestamps are bar OPEN times (yfinance
    convention); Bar identity is the CLOSE time.
    """
    out: list[Bar] = []
    for open_ts, o, h, low, c, v in rows:
        ts_close = to_et(open_ts) + dt.timedelta(minutes=interval_min)
        if ts_close > now:
            continue  # in-progress bar — invisible until complete
        vals = (o, h, low, c)
        if any((x != x or x is None or x <= 0) for x in vals):
            continue
        volume = 0 if (v != v) else int(v)
        try:
            out.append(Bar(ts_close=ts_close, open=o, high=h, low=low, close=c, volume=volume))
        except Exception:
            continue  # disordered OHLC from provider glitch: skip, never crash
    return out


def quotes_from_chain_rows(
    rows: list[dict[str, Any]],
    *,
    underlying: str,
    expiry: dt.date,
    right: str,
    now: dt.datetime,
    spot: float,
    max_strikes_around_atm: int = 12,
) -> list[OptionQuote]:
    """Provider chain rows -> validated OptionQuotes near the money.

    A row with unusable bid/ask still becomes a quote (bid/ask 0) so the
    checklist can REPORT 'no market' instead of silently losing the strike."""
    typed: list[OptionQuote] = []
    for r in rows:
        strike_c = _clean_cents(r.get("strike"))
        if strike_c is None:
            continue
        bid = _clean_cents(r.get("bid")) or 0
        ask = _clean_cents(r.get("ask")) or 0
        vol_raw = r.get("volume")
        oi_raw = r.get("openInterest")
        iv_raw = r.get("impliedVolatility")
        try:
            volume = 0 if vol_raw is None or vol_raw != vol_raw else int(vol_raw)
        except (TypeError, ValueError):
            volume = 0
        try:
            oi = 0 if oi_raw is None or oi_raw != oi_raw else int(oi_raw)
        except (TypeError, ValueError):
            oi = 0
        iv_hint: float | None
        try:
            fiv = float(iv_raw)  # type: ignore[arg-type]
            iv_hint = fiv if 0.0 < fiv < 5.0 else None
        except (TypeError, ValueError):
            iv_hint = None
        typed.append(
            OptionQuote(
                underlying=underlying,
                expiry=expiry,
                strike_cents=strike_c,
                right="C" if right == "C" else "P",
                bid_cents=bid,
                ask_cents=ask,
                volume=volume,
                open_interest=oi,
                received_at=now,
                iv_hint=iv_hint,
            )
        )
    typed.sort(key=lambda q: abs(q.strike_cents - int(spot * 100)))
    return sorted(typed[:max_strikes_around_atm], key=lambda q: q.strike_cents)


# One shared limiter per process: Yahoo throttles per client, not per symbol,
# so a per-source limiter would let a 3-symbol run fire 3x the rate.
_LIMITER = RateLimiter(float(os.environ.get("QTS_MIN_REQUEST_INTERVAL_S", "1.0")))


class YFinanceSource:
    """Live source. Network I/O lives here and ONLY here.

    Every outbound call passes the shared rate limiter; expiration lists are
    disk-cached per (symbol, session date) because the listed expiries do not
    change intraday. Both were asked for in the mandate and were absent from
    the legacy scanner (finding no-rate-limit-no-cache).
    """

    def __init__(self, symbol: str, cache: DiskCache | None = None) -> None:
        self.symbol = symbol.upper()
        self.cache = cache if cache is not None else DiskCache()

    def _ticker(self):  # type: ignore[no-untyped-def]
        import yfinance as yf

        _LIMITER.acquire()
        return yf.Ticker(self.symbol)

    def expirations(self, session_date: dt.date | None = None) -> list[dt.date]:
        if session_date is None:
            return [dt.date.fromisoformat(e) for e in self._ticker().options]
        key = f"expiries-{self.symbol}-{session_date.isoformat()}"
        raw, _ = self.cache.get_or_fetch(key, lambda: list(self._ticker().options))
        return [dt.date.fromisoformat(e) for e in raw]

    def has_same_day_expiry(self, session_date: dt.date) -> bool:
        """B4 gate: 0DTE availability is CHECKED, never assumed."""
        return session_date in self.expirations(session_date)

    def fetch_bars(self, *, now: dt.datetime, days: int = 10, interval_min: int = 5) -> list[Bar]:
        hist = self._ticker().history(
            period=f"{days}d",
            interval=f"{interval_min}m",
            auto_adjust=False,  # finding LA-auto-adjust — NEVER adjusted intraday
            prepost=False,
        )
        rows: list[tuple[dt.datetime, float, float, float, float, float]] = [
            (
                idx.to_pydatetime(),
                float(r["Open"]),
                float(r["High"]),
                float(r["Low"]),
                float(r["Close"]),
                float(r["Volume"]),
            )
            for idx, r in hist.iterrows()
        ]
        return bars_from_rows(rows, now=now, interval_min=interval_min)

    def fetch_chain(
        self, *, session_date: dt.date, now: dt.datetime, spot: float
    ) -> list[OptionQuote]:
        chain = self._ticker().option_chain(session_date.isoformat())
        calls = getattr(chain, "calls", None)
        if calls is None or calls.empty:
            return []  # listed expiry with an empty book: report, never crash
        rows = [dict(r) for _, r in calls.iterrows()]
        return quotes_from_chain_rows(
            rows, underlying=self.symbol, expiry=session_date, right="C", now=now, spot=spot
        )

    def build_view(self, *, now: dt.datetime, session_date: dt.date) -> MarketView:
        all_bars = self.fetch_bars(now=now)
        session_bars = tuple(b for b in all_bars if to_et(b.ts_close).date() == session_date)
        prior_dates = sorted({to_et(b.ts_close).date() for b in all_bars} - {session_date})
        prior_sessions = tuple(
            tuple(b for b in all_bars if to_et(b.ts_close).date() == d) for d in prior_dates
        )
        spot = session_bars[-1].close if session_bars else None
        chain: tuple[OptionQuote, ...] = ()
        if spot is not None and self.has_same_day_expiry(session_date):
            chain = tuple(self.fetch_chain(session_date=session_date, now=now, spot=spot))
        return MarketView(
            now=now,
            session_date=session_date,
            bars=session_bars,
            prior_sessions=prior_sessions,
            chain=chain,
            underlying_last=spot,
            meta={"symbol": self.symbol, "provider": "yfinance", "delayed": "true"},
        )


NY_TZ = NY  # re-export for CLI convenience
