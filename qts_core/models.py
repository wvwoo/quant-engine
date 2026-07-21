"""Frozen market-data models and the look-ahead firewall.

``MarketView`` is the ONLY thing the strategy is allowed to see. Its
constructor rejects any bar or quote stamped after 'now' — so look-ahead is a
construction error, not a code-review hope (findings LA-*).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

from qts_core.clock import require_aware, to_et


class LookaheadError(RuntimeError):
    """Data from the future reached the strategy boundary."""


class DataQualityError(ValueError):
    """Market data failed a sanity gate (NaN, zero/crossed quote, disorder)."""


@dataclass(frozen=True, slots=True)
class Bar:
    """One completed OHLCV bar. ``ts_close`` is the bar's CLOSE time.

    Stolen semantic (nautilus/LEAN): a bar does not exist until it closes, and
    its identity timestamp is the close — never the open. A bar whose close is
    in the future is a look-ahead leak by definition.
    """

    ts_close: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self) -> None:
        require_aware(self.ts_close)
        for name in ("open", "high", "low", "close"):
            v: float = getattr(self, name)
            if v != v or v <= 0:
                raise DataQualityError(f"bar {name} invalid: {v!r} @ {self.ts_close}")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise DataQualityError(f"bar OHLC disordered @ {self.ts_close}")
        if self.volume < 0:
            raise DataQualityError(f"bar volume negative @ {self.ts_close}")


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """One option quote. Money in integer cents; zero/crossed handled explicitly.

    A bid=0/ask=0 quote is NO MARKET — the legacy scanner displayed it as a
    $0.00 spread, reading the WORST liquidity as the best (finding
    zero-bid-ask-reads-as-perfect-liquidity). ``is_quotable`` is the gate.
    """

    underlying: str
    expiry: dt.date
    strike_cents: int
    right: Literal["C", "P"]
    bid_cents: int
    ask_cents: int
    volume: int
    open_interest: int
    received_at: dt.datetime
    iv_hint: float | None = None  # provider IV; advisory only (ADR-002)

    def __post_init__(self) -> None:
        require_aware(self.received_at)
        if self.bid_cents < 0 or self.ask_cents < 0:
            raise DataQualityError(f"negative quote {self.bid_cents}/{self.ask_cents}")

    @property
    def is_quotable(self) -> bool:
        return self.bid_cents > 0 and self.ask_cents > 0 and self.ask_cents >= self.bid_cents

    @property
    def mid_cents(self) -> int | None:
        if not self.is_quotable:
            return None
        return (self.bid_cents + self.ask_cents) // 2

    @property
    def spread_cents(self) -> int | None:
        if not self.is_quotable:
            return None
        return self.ask_cents - self.bid_cents

    def spread_bp_of_mid(self) -> int | None:
        """Spread as basis points of mid, rounded up (conservative)."""
        mid = self.mid_cents
        spread = self.spread_cents
        if mid is None or spread is None or mid == 0:
            return None
        return -(-spread * 10_000 // mid)

    @property
    def occ_symbol(self) -> str:
        """OCC option symbol, e.g. SPY260721C00628000."""
        strike_millis = self.strike_cents * 10  # cents -> 1/1000 dollar units
        return f"{self.underlying.upper():s}{self.expiry:%y%m%d}{self.right}{strike_millis:08d}"


@dataclass(frozen=True, slots=True)
class MarketView:
    """Frozen, validated snapshot handed to the strategy. Nothing newer than
    ``now`` can exist inside it — enforced at construction."""

    now: dt.datetime
    session_date: dt.date
    bars: tuple[Bar, ...]  # completed session bars so far, ascending
    prior_sessions: tuple[tuple[Bar, ...], ...] = ()  # for RVOL baseline / HV
    chain: tuple[OptionQuote, ...] = ()
    underlying_last: float | None = None
    meta: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_aware(self.now)
        prev: dt.datetime | None = None
        for b in self.bars:
            if b.ts_close > self.now:
                raise LookaheadError(f"bar closing {b.ts_close} is in the future of now={self.now}")
            if prev is not None and b.ts_close <= prev:
                raise DataQualityError("session bars not strictly ascending")
            prev = b.ts_close
        for q in self.chain:
            if q.received_at > self.now:
                raise LookaheadError(
                    f"quote received {q.received_at} is in the future of now={self.now}"
                )
        for sess in self.prior_sessions:
            for b in sess:
                if to_et(b.ts_close).date() >= self.session_date:
                    raise LookaheadError(
                        f"prior-session bar {b.ts_close} is not strictly before "
                        f"session {self.session_date}"
                    )
