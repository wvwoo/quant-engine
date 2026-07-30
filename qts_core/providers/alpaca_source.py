"""Alpaca market-data source (OFF by default; select with --provider alpaca).

WHY THIS PROVIDER FIRST. It reuses the SAME key pair the owner already creates
for the paper broker (APCA_API_KEY_ID / APCA_API_SECRET_KEY), so "one key and it
runs" becomes literally true rather than aspirational. Auth is two request
headers over plain JSON HTTPS, so it needs no dependency at all — stdlib urllib,
like broker_alpaca.py.

WHAT IT DOES AND DOES NOT IMPROVE, stated up front because the opposite would be
a false claim:
- Equity BARS on the free tier are real-time IEX. That is a genuine improvement
  over Yahoo's delayed bars, and bars are what feed RVOL, ORB and VWAP.
- Option quotes on the free tier come from Alpaca's "Indicative Pricing Feed", a
  free DERIVATIVE of OPRA. Precisely what the vendor documents: the quotes are
  derivative quotes rather than actual OPRA quotes, and the TRADES are delayed by
  15 minutes. A specific per-quote delay figure is NOT documented, so this module
  does not claim one either way — it marks the feed as derived and delayed and
  leaves the magnitude unasserted (feed="alpaca-indicative", delayed="true").
  Anyone expecting this provider to deliver real-time OPRA option quotes on the
  free tier is wrong; that is the $99/mo tier.
- No greeks, for 0DTE or otherwise: Alpaca states 0DTE contracts "won't have
  Greeks" because Black-Scholes divides by days-to-expiry. This ALIGNS with
  ADR-002 — delta and IV are ours, from qts_core.pricing, and always were.

STRUCTURAL SAFETY. The data host is a module constant, like the broker's paper
host, and this class has NO order-placing path: it issues GET requests only, and
a test asserts the class never references an orders endpoint. A data client
cannot be repurposed into a trading client by editing a config value.

ASSUMPTION (tagged per protocol): Alpaca publishes no dedicated "list the
expiries for this underlying" endpoint that this round verified. `expirations()`
therefore DERIVES the expiry set from option-snapshot keys, which are OCC
symbols encoding the expiry as YYMMDD. `has_same_day_expiry()` does not depend
on that derivation — it asks the snapshot endpoint for one exact expiration_date
and reports whether anything came back, which is precisely the B4 question.

ASSUMPTION: the free tier's option feed is named "indicative" and the equity
feed "iex"; both are passed explicitly rather than relying on a server-side
default, because the documented default for options is "opra" (paid) and a
silent fallback to a paid feed would fail confusingly on a free account.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from qts_core import secrets
from qts_core.broker_alpaca import AlpacaAccountRejected, AlpacaUnreachable
from qts_core.cache import DiskCache
from qts_core.clock import TradingClock, to_et
from qts_core.config import StrategyConfig
from qts_core.models import Bar, MarketView, OptionQuote
from qts_core.providers import registry
from qts_core.providers.yfinance_source import bars_from_rows, limiter, quotes_from_chain_rows

# NOT configurable, and NOT the trading host. Market data only.
ALPACA_DATA_BASE_URL = "https://data.alpaca.markets"

OPTION_FEED = "indicative"  # free tier; "opra" is the paid real-time feed
EQUITY_FEED = "iex"  # free tier real-time; SIP requires Algo Trader Plus

# (method, path) -> (http_status, decoded_json)
Transport = Callable[[str, str], tuple[int, dict[str, Any]]]


class AlpacaRateLimited(RuntimeError):
    """HTTP 429. The free tier allows 200 historical calls/min."""


class AlpacaBadResponse(RuntimeError):
    """The venue answered 200 with something this code cannot read."""


def _default_transport(key_id: str, secret: str) -> Transport:
    def request(method: str, path: str) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(
            ALPACA_DATA_BASE_URL + path,
            method=method,
            headers={
                "APCA-API-KEY-ID": key_id,
                "APCA-API-SECRET-KEY": secret,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                return exc.code, {}
        except urllib.error.URLError as exc:
            raise AlpacaUnreachable(
                f"alpaca data host unreachable ({ALPACA_DATA_BASE_URL}): {exc.reason}"
            ) from exc

    return request


def expiry_from_occ(occ: str) -> dt.date | None:
    """SPY260722C00743000 -> 2026-07-22. None if the shape is not recognised.

    Returning None rather than raising: one malformed key in a large snapshot
    must not cost the whole chain, and the caller reports what it could parse.
    """
    digits = "".join(ch for ch in occ if ch.isdigit())
    # underlying letters, then YYMMDD, then C/P, then an 8-digit strike.
    if len(digits) < 14:
        return None
    try:
        # dt.date built directly rather than via strptime: strptime returns a
        # NAIVE datetime, which this repo bans outright (ruff DTZ) because the
        # machine runs 7h from ET. A date needs no timezone, so constructing one
        # sidesteps the naive-datetime intermediate entirely.
        return dt.date(2000 + int(digits[0:2]), int(digits[2:4]), int(digits[4:6]))
    except ValueError:
        return None


class AlpacaDataSource:
    """Implements OptionChainSource against Alpaca's market-data API."""

    def __init__(
        self,
        symbol: str,
        cache: DiskCache | None = None,
        cfg: StrategyConfig | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.cache = cache if cache is not None else DiskCache()
        self.cfg = cfg if cfg is not None else StrategyConfig()
        if transport is None:
            # Same loader as the broker, for the same reason: a LaunchAgent
            # inherits none of the login shell environment.
            key_id = secrets.get("APCA_API_KEY_ID", required=False)
            api_secret = secrets.get("APCA_API_SECRET_KEY", required=False)
            if not key_id or not api_secret:
                from qts_core.broker_alpaca import AlpacaCredentialsMissing

                raise AlpacaCredentialsMissing(
                    "set APCA_API_KEY_ID and APCA_API_SECRET_KEY (PAPER account keys) "
                    f"in the environment or in {secrets.secrets_file_path()} (mode 0600) "
                    "to use --provider alpaca. Steps: KEY_ACQUISITION.md"
                )
            transport = _default_transport(key_id, api_secret)
        self._request = transport

    # ------------------------------------------------------------------ http
    def _get(self, path: str) -> dict[str, Any]:
        limiter().acquire()  # shared per-process spacing, same as yfinance
        status, body = self._request("GET", path)
        if status in (401, 403):
            raise AlpacaAccountRejected(
                secrets.redact(
                    f"alpaca REJECTED these keys for market data (HTTP {status}). The keys are "
                    "present but not accepted — check they came from the PAPER dashboard. "
                    f"Venue said: {body}"
                )
            )
        if status == 429:
            raise AlpacaRateLimited(
                "alpaca rate limit hit (HTTP 429). The free tier allows 200 historical "
                "calls/min; raise QTS_MIN_REQUEST_INTERVAL_S to slow this client down."
            )
        if status != 200:
            raise AlpacaBadResponse(secrets.redact(f"alpaca data HTTP {status}: {body}"))
        if not isinstance(body, dict):
            raise AlpacaBadResponse(f"alpaca data returned {type(body).__name__}, expected object")
        return body

    # --------------------------------------------------------------- options
    def _snapshots(self, params: dict[str, str]) -> dict[str, Any]:
        query = urllib.parse.urlencode({"feed": OPTION_FEED, **params})
        body = self._get(f"/v1beta1/options/snapshots/{self.symbol}?{query}")
        snaps = body.get("snapshots")
        if snaps is None:
            return {}
        if not isinstance(snaps, dict):
            raise AlpacaBadResponse("alpaca snapshots payload is not an object")
        return snaps

    def expirations(self, session_date: dt.date | None = None) -> list[dt.date]:
        if session_date is None:
            # NOT dt.date.today(): this machine runs +7h from ET, so a naive
            # local date is the WRONG session for several hours every day. The
            # exchange session date is the only correct starting point, and the
            # DTZ ruff rule exists in this repo to catch exactly this slip —
            # which it did, on the first run of ci.sh after I wrote it.
            return self._fetch_expirations(TradingClock.system().session_date())
        key = f"alpaca-expiries-{self.symbol}-{session_date.isoformat()}"
        raw, _ = self.cache.get_or_fetch(
            key, lambda: [d.isoformat() for d in self._fetch_expirations(session_date)]
        )
        return [dt.date.fromisoformat(e) for e in raw]

    def _fetch_expirations(self, frm: dt.date) -> list[dt.date]:
        snaps = self._snapshots({"expiration_date_gte": frm.isoformat()})
        found = {e for occ in snaps if (e := expiry_from_occ(occ)) is not None}
        return sorted(found)

    def has_same_day_expiry(self, session_date: dt.date) -> bool:
        """B4, asked directly: does this underlying list THIS date at all?

        Deliberately NOT derived from expirations(): one exact-date query is a
        smaller, cheaper and more reliable answer than parsing a window of OCC
        keys, and B4 is a blocking gate that runs every symbol every day.
        """
        return bool(self._snapshots({"expiration_date": session_date.isoformat()}))

    def fetch_chain(
        self, *, session_date: dt.date, now: dt.datetime, spot: float
    ) -> list[OptionQuote]:
        snaps = self._snapshots({"expiration_date": session_date.isoformat()})
        rows: list[dict[str, Any]] = []
        for occ, snap in snaps.items():
            if not isinstance(snap, dict):
                continue
            # CALLS only (see the Protocol note): the strategy buys calls.
            letters = "".join(ch for ch in occ if ch.isalpha())
            if not letters.endswith("C"):
                continue
            quote = snap.get("latestQuote") or {}
            strike = _strike_from_occ(occ)
            if strike is None:
                continue
            rows.append(
                {
                    "strike": strike,
                    "bid": quote.get("bp"),
                    "ask": quote.get("ap"),
                    "volume": (snap.get("dailyBar") or {}).get("v"),
                    "openInterest": snap.get("openInterest"),
                    # No impliedVolatility on purpose: ADR-002. Even when a
                    # vendor supplies one it is theirs, not ours.
                    "impliedVolatility": None,
                }
            )
        return quotes_from_chain_rows(
            rows,
            underlying=self.symbol,
            expiry=session_date,
            right="C",
            now=now,
            spot=spot,
            max_strikes_around_atm=self.cfg.max_strikes_around_atm,
        )

    # ------------------------------------------------------------------ bars
    def fetch_bars(
        self, *, now: dt.datetime, days: int | None = None, interval_min: int | None = None
    ) -> list[Bar]:
        days = self.cfg.bars_fetch_days if days is None else days
        interval_min = self.cfg.bar_interval_min if interval_min is None else interval_min
        start = (to_et(now) - dt.timedelta(days=days)).date().isoformat()
        query = urllib.parse.urlencode(
            {
                "timeframe": f"{interval_min}Min",
                "start": start,
                "feed": EQUITY_FEED,
                "limit": "10000",
                "adjustment": "raw",  # never back-apply future splits (LA-auto-adjust)
            }
        )
        body = self._get(f"/v2/stocks/{self.symbol}/bars?{query}")
        raw = body.get("bars") or []
        if not isinstance(raw, list):
            raise AlpacaBadResponse("alpaca bars payload is not a list")
        rows: list[tuple[dt.datetime, float, float, float, float, float]] = []
        for b in raw:
            if not isinstance(b, dict) or "t" not in b:
                continue
            try:
                ts = dt.datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00"))
                rows.append(
                    (
                        to_et(ts),  # Alpaca stamps bar OPEN time, like yfinance
                        float(b["o"]),
                        float(b["h"]),
                        float(b["l"]),
                        float(b["c"]),
                        float(b.get("v") or 0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue  # one bad row costs that row, never the fetch
        return bars_from_rows(rows, now=now, interval_min=interval_min)

    # ------------------------------------------------------------------ view
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
        meta = registry.declared_meta("alpaca", self.symbol)
        registry.assert_meta_is_honest(meta)
        return MarketView(
            now=now,
            session_date=session_date,
            bars=session_bars,
            prior_sessions=prior_sessions,
            chain=chain,
            underlying_last=spot,
            meta=meta,
        )


def _strike_from_occ(occ: str) -> float | None:
    """...C00743000 -> 743.0 (last 8 digits are strike x 1000)."""
    digits = "".join(ch for ch in occ if ch.isdigit())
    if len(digits) < 14:
        return None
    try:
        return int(digits[-8:]) / 1000.0
    except ValueError:
        return None
