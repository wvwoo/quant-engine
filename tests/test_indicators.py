from __future__ import annotations

import datetime as dt

import pytest

from qts_core.clock import NY
from qts_core.models import Bar
from qts_core.signals.indicators import (
    cumulative_volume,
    macd_histogram,
    orb_high,
    rsi,
    rvol,
    vwap,
)


def et(hh: int, mm: int, day: int = 17) -> dt.datetime:
    return dt.datetime(2026, 6, day, hh, mm, tzinfo=NY)


def bar(hh: int, mm: int, *, day: int = 17, o: float, h: float, lo: float, c: float, v: int) -> Bar:
    return Bar(ts_close=et(hh, mm, day), open=o, high=h, low=lo, close=c, volume=v)


OPEN = et(9, 30)


class TestVwap:
    def test_empty_is_none(self) -> None:
        assert vwap([]) is None

    def test_single_bar_is_typical_price(self) -> None:
        b = bar(9, 35, o=100, h=102, lo=99, c=101, v=500)
        assert vwap([b]) == pytest.approx((102 + 99 + 101) / 3)

    def test_volume_weighting(self) -> None:
        b1 = bar(9, 35, o=100, h=100, lo=100, c=100, v=100)  # tp=100
        b2 = bar(9, 40, o=200, h=200, lo=200, c=200, v=300)  # tp=200
        assert vwap([b1, b2]) == pytest.approx((100 * 100 + 200 * 300) / 400)

    def test_zero_volume_session_is_none(self) -> None:
        assert vwap([bar(9, 35, o=1, h=1, lo=1, c=1, v=0)]) is None


class TestOrb:
    B1 = bar(9, 35, o=203.5, h=203.9, lo=203.2, c=203.7, v=100)
    B2 = bar(9, 40, o=203.7, h=204.10, lo=203.5, c=203.9, v=100)  # range high 204.10
    B3 = bar(9, 45, o=203.9, h=204.0, lo=203.6, c=203.8, v=100)
    LATER = bar(10, 0, o=204.0, h=205.5, lo=203.9, c=204.79, v=100)

    def test_incomplete_range_is_none(self) -> None:
        # At 9:44 the 15m range is not finished — acting on it is look-ahead.
        assert orb_high([self.B1, self.B2], OPEN, 15, et(9, 44)) is None

    def test_complete_range_max_high(self) -> None:
        got = orb_high([self.B1, self.B2, self.B3, self.LATER], OPEN, 15, et(10, 15))
        assert got == pytest.approx(204.10)

    def test_later_bars_never_pollute_range(self) -> None:
        # LATER's 205.5 high must not leak into the 9:30-9:45 range.
        with_later = orb_high([self.B1, self.B2, self.B3, self.LATER], OPEN, 15, et(10, 15))
        without = orb_high([self.B1, self.B2, self.B3], OPEN, 15, et(10, 15))
        assert with_later == without

    def test_no_bars_in_range_is_none(self) -> None:
        assert orb_high([self.LATER], OPEN, 15, et(10, 15)) is None


class TestRvol:
    def _prior(self, day: int, vols: list[int]) -> list[Bar]:
        out = []
        t = [(9, 35), (9, 40), (9, 45), (9, 50)]
        for (hh, mm), v in zip(t, vols, strict=True):
            out.append(bar(hh, mm, day=day, o=1, h=1.1, lo=0.9, c=1, v=v))
        return out

    def test_same_elapsed_time_construction(self) -> None:
        # Today by 9:45: 300+300+300 = 900.
        today = [
            bar(9, 35, o=1, h=1.1, lo=0.9, c=1, v=300),
            bar(9, 40, o=1, h=1.1, lo=0.9, c=1, v=300),
            bar(9, 45, o=1, h=1.1, lo=0.9, c=1, v=300),
        ]
        # Prior sessions: cumulative to 9:45 is 100+100+100=300 each; the 9:50
        # bar (400) must be EXCLUDED because it is after today's elapsed time.
        p1 = self._prior(15, [100, 100, 100, 400])
        p2 = self._prior(16, [100, 100, 100, 400])
        got = rvol(today, [p1, p2], et(9, 45))
        assert got == pytest.approx(900 / 300)

    def test_no_priors_is_none(self) -> None:
        assert rvol([], [], et(10, 0)) is None

    def test_zero_baselines_is_none(self) -> None:
        today = [bar(9, 35, o=1, h=1.1, lo=0.9, c=1, v=100)]
        p = [bar(15, 0, day=16, o=1, h=1.1, lo=0.9, c=1, v=500)]  # all after cutoff
        assert rvol(today, [p], et(9, 35)) is None

    def test_cumulative_volume(self) -> None:
        today = [
            bar(9, 35, o=1, h=1.1, lo=0.9, c=1, v=10),
            bar(9, 40, o=1, h=1.1, lo=0.9, c=1, v=20),
        ]
        assert cumulative_volume(today) == 30


class TestRsi:
    def test_insufficient_data_none(self) -> None:
        assert rsi([1.0] * 14, 14) is None  # needs period+1

    def test_all_gains_is_100(self) -> None:
        closes = [float(i) for i in range(1, 17)]
        assert rsi(closes, 14) == 100.0

    def test_all_losses_near_zero(self) -> None:
        closes = [float(i) for i in range(17, 1, -1)]
        assert rsi(closes, 14) == pytest.approx(0.0, abs=1e-9)

    def test_known_wilder_value(self) -> None:
        # Classic textbook series (Wilder's own soybean example digits vary by
        # source; instead assert a structural property): alternating equal
        # gains/losses -> RSI 50.
        closes = [100.0]
        for i in range(30):
            closes.append(closes[-1] + (1.0 if i % 2 == 0 else -1.0))
        got = rsi(closes, 14)
        assert got == pytest.approx(50.0, abs=2.0)

    def test_report_style_moderate_momentum(self) -> None:
        # Mixed steps with upward drift -> RSI in the 50-70 non-overbought zone.
        steps = (0.30, -0.20, 0.20, -0.15)
        closes = [200.0]
        for i in range(40):
            closes.append(closes[-1] + steps[i % 4])
        got = rsi(closes, 14)
        assert got is not None
        assert 50 < got < 70


class TestMacd:
    def test_insufficient_none(self) -> None:
        assert macd_histogram([1.0] * 34, 12, 26, 9) is None

    def test_uptrend_positive_histogram(self) -> None:
        closes = [100 * (1.003**i) for i in range(60)]
        hist = macd_histogram(closes, 12, 26, 9)
        assert hist is not None
        assert hist > 0

    def test_downtrend_negative_histogram(self) -> None:
        # ACCELERATING decline: a decelerating fall (e.g. geometric decay) can
        # legitimately flip the histogram positive — momentum is the derivative.
        closes = [200.0 - 0.01 * i * i for i in range(60)]
        hist = macd_histogram(closes, 12, 26, 9)
        assert hist is not None
        assert hist < 0

    def test_flat_series_zero(self) -> None:
        hist = macd_histogram([100.0] * 60, 12, 26, 9)
        assert hist == pytest.approx(0.0, abs=1e-12)
