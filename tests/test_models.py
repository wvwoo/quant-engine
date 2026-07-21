from __future__ import annotations

import datetime as dt

import pytest

from qts_core.clock import NY
from qts_core.models import Bar, DataQualityError, LookaheadError, MarketView, OptionQuote


def et(hh: int, mm: int, day: int = 17) -> dt.datetime:
    return dt.datetime(2026, 6, day, hh, mm, tzinfo=NY)


def mk_bar(hh: int, mm: int, *, day: int = 17, close: float = 204.0, volume: int = 1000) -> Bar:
    return Bar(
        ts_close=et(hh, mm, day),
        open=close - 0.5,
        high=close + 0.6,
        low=close - 0.8,
        close=close,
        volume=volume,
    )


def mk_quote(bid: int, ask: int, *, received: dt.datetime | None = None) -> OptionQuote:
    return OptionQuote(
        underlying="NVDA",
        expiry=dt.date(2026, 6, 17),
        strike_cents=20500,
        right="C",
        bid_cents=bid,
        ask_cents=ask,
        volume=100,
        open_interest=500,
        received_at=received or et(10, 15),
    )


class TestBar:
    def test_naive_ts_rejected(self) -> None:
        with pytest.raises(Exception, match="naive"):
            Bar(dt.datetime(2026, 6, 17, 10, 0), 1, 2, 0.5, 1.5, 10)  # noqa: DTZ001

    def test_nan_price_rejected(self) -> None:
        with pytest.raises(DataQualityError):
            mk_bar(10, 0, close=float("nan"))

    def test_disordered_ohlc_rejected(self) -> None:
        with pytest.raises(DataQualityError):
            Bar(et(10, 0), open=205.0, high=204.0, low=203.0, close=204.5, volume=10)


class TestQuoteGates:
    def test_zero_bid_ask_is_not_quotable(self) -> None:
        # Worst liquidity must NOT read as best (legacy scanner bug).
        q = mk_quote(0, 0)
        assert not q.is_quotable
        assert q.mid_cents is None
        assert q.spread_cents is None
        assert q.spread_bp_of_mid() is None

    def test_crossed_market_not_quotable(self) -> None:
        assert not mk_quote(430, 420).is_quotable

    def test_report_quote_spread(self) -> None:
        q = mk_quote(417, 420)  # $0.03 spread as in the report's liquidity row
        assert q.is_quotable
        assert q.spread_cents == 3
        assert q.mid_cents == 418

    def test_spread_bp_rounds_up(self) -> None:
        q = mk_quote(417, 420)
        # 3/418 = 71.77bp -> ceil 72
        assert q.spread_bp_of_mid() == 72

    def test_occ_symbol(self) -> None:
        assert mk_quote(417, 420).occ_symbol == "NVDA260617C00205000"


class TestMarketViewFirewall:
    def test_future_bar_rejected(self) -> None:
        with pytest.raises(LookaheadError):
            MarketView(
                now=et(10, 0),
                session_date=dt.date(2026, 6, 17),
                bars=(mk_bar(10, 5),),
            )

    def test_future_quote_rejected(self) -> None:
        with pytest.raises(LookaheadError):
            MarketView(
                now=et(10, 0),
                session_date=dt.date(2026, 6, 17),
                bars=(),
                chain=(mk_quote(417, 420, received=et(10, 1)),),
            )

    def test_prior_session_bar_from_today_rejected(self) -> None:
        # RVOL baseline must be built strictly from PRIOR sessions
        # (finding LA-rvol-completed-day).
        with pytest.raises(LookaheadError):
            MarketView(
                now=et(10, 0),
                session_date=dt.date(2026, 6, 17),
                bars=(),
                prior_sessions=((mk_bar(9, 35),),),
            )

    def test_unsorted_bars_rejected(self) -> None:
        with pytest.raises(DataQualityError):
            MarketView(
                now=et(11, 0),
                session_date=dt.date(2026, 6, 17),
                bars=(mk_bar(10, 0), mk_bar(9, 55)),
            )

    def test_valid_view_constructs(self) -> None:
        v = MarketView(
            now=et(10, 15),
            session_date=dt.date(2026, 6, 17),
            bars=(mk_bar(9, 35), mk_bar(9, 40)),
            prior_sessions=((mk_bar(15, 55, day=16),),),
            chain=(mk_quote(417, 420),),
            underlying_last=204.79,
        )
        assert len(v.bars) == 2
