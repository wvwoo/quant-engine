"""Pure conversion helpers of the yfinance provider (no network)."""

from __future__ import annotations

import datetime as dt

from qts_core.clock import NY
from qts_core.providers.yfinance_source import bars_from_rows, quotes_from_chain_rows

NOW = dt.datetime(2026, 7, 21, 10, 20, tzinfo=NY)
SESSION = dt.date(2026, 7, 21)


def ts(hh: int, mm: int) -> dt.datetime:
    return dt.datetime(2026, 7, 21, hh, mm, tzinfo=NY)


class TestBarsFromRows:
    def test_in_progress_bar_dropped(self) -> None:
        rows = [
            (ts(10, 10), 100.0, 101.0, 99.5, 100.5, 5000.0),  # closes 10:15 <= now
            (ts(10, 15), 100.5, 101.5, 100.0, 101.0, 4000.0),  # closes 10:20 <= now
            (ts(10, 20), 101.0, 101.2, 100.9, 101.1, 100.0),  # closes 10:25 > now: FORMING
        ]
        bars = bars_from_rows(rows, now=NOW, interval_min=5)
        assert [b.ts_close for b in bars] == [ts(10, 15), ts(10, 20)]

    def test_nan_row_dropped_not_crashed(self) -> None:
        nan = float("nan")
        rows = [
            (ts(10, 0), nan, 101.0, 99.5, 100.5, 5000.0),
            (ts(10, 5), 100.0, 101.0, 99.5, 100.5, 5000.0),
        ]
        bars = bars_from_rows(rows, now=NOW, interval_min=5)
        assert len(bars) == 1

    def test_nan_volume_becomes_zero(self) -> None:
        rows = [(ts(10, 0), 100.0, 101.0, 99.5, 100.5, float("nan"))]
        bars = bars_from_rows(rows, now=NOW, interval_min=5)
        assert bars[0].volume == 0

    def test_disordered_provider_row_skipped(self) -> None:
        rows = [(ts(10, 0), 105.0, 101.0, 99.5, 100.5, 100.0)]  # open > high
        assert bars_from_rows(rows, now=NOW, interval_min=5) == []


class TestQuotesFromChainRows:
    def _row(
        self, strike: float, bid: float, ask: float, iv: float | None = 0.2
    ) -> dict[str, object]:
        return {
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "volume": 100,
            "openInterest": 200,
            "impliedVolatility": iv,
        }

    def test_dead_quote_kept_as_no_market_not_dropped(self) -> None:
        qs = quotes_from_chain_rows(
            [self._row(628.0, 0.0, 0.0)],
            underlying="SPY",
            expiry=SESSION,
            right="C",
            now=NOW,
            spot=628.0,
        )
        assert len(qs) == 1
        assert not qs[0].is_quotable  # reported as no-market, not silently lost

    def test_atm_window_selection(self) -> None:
        rows = [self._row(600.0 + 5 * i, 1.0, 1.05) for i in range(20)]  # 600..695
        qs = quotes_from_chain_rows(
            rows,
            underlying="SPY",
            expiry=SESSION,
            right="C",
            now=NOW,
            spot=628.0,
            max_strikes_around_atm=6,
        )
        assert len(qs) == 6
        strikes = [q.strike_cents // 100 for q in qs]
        assert all(610 <= s <= 645 for s in strikes)  # clustered around spot

    def test_nan_iv_hint_becomes_none(self) -> None:
        qs = quotes_from_chain_rows(
            [self._row(628.0, 1.0, 1.05, iv=float("nan"))],
            underlying="SPY",
            expiry=SESSION,
            right="C",
            now=NOW,
            spot=628.0,
        )
        assert qs[0].iv_hint is None

    def test_absurd_iv_hint_rejected(self) -> None:
        qs = quotes_from_chain_rows(
            [self._row(628.0, 1.0, 1.05, iv=99.0)],
            underlying="SPY",
            expiry=SESSION,
            right="C",
            now=NOW,
            spot=628.0,
        )
        assert qs[0].iv_hint is None
