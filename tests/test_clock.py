from __future__ import annotations

import datetime as dt

import pytest

from qts_core.clock import (
    NY,
    UTC,
    NaiveDatetimeError,
    TradingClock,
    effective_force_flat_et,
    in_time_window,
    is_trading_day,
    require_aware,
    session_close_et,
    session_open_et,
    to_et,
)


def et(y: int, m: int, d: int, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=NY)


class TestAwareness:
    def test_naive_rejected(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            require_aware(dt.datetime(2026, 7, 21, 10, 0))  # noqa: DTZ001

    def test_aware_passes(self) -> None:
        d = et(2026, 7, 21, 10, 0)
        assert require_aware(d) is d

    def test_riyadh_to_et_offset_is_7h_in_summer(self) -> None:
        riyadh = ZoneInfoRiyadh = dt.timezone(dt.timedelta(hours=3))
        machine = dt.datetime(2026, 7, 21, 17, 15, tzinfo=ZoneInfoRiyadh)
        assert to_et(machine) == et(2026, 7, 21, 10, 15)
        del riyadh


class TestClock:
    def test_injected_now(self) -> None:
        frozen = dt.datetime(2026, 6, 17, 14, 15, tzinfo=UTC)  # 10:15 ET (EDT)
        clk = TradingClock(lambda: frozen)
        assert clk.now_et() == et(2026, 6, 17, 10, 15)
        assert clk.session_date() == dt.date(2026, 6, 17)

    def test_session_date_is_exchange_local_not_machine_local(self) -> None:
        # 01:00 Riyadh on the 22nd == 18:00 ET on the 21st. The legacy scanner
        # got this wrong for ~7h every day (finding TZ-local-now).
        riyadh = dt.timezone(dt.timedelta(hours=3))
        clk = TradingClock(lambda: dt.datetime(2026, 7, 22, 1, 0, tzinfo=riyadh))
        assert clk.session_date() == dt.date(2026, 7, 21)


class TestCalendar:
    def test_weekday_is_session(self) -> None:
        assert is_trading_day(dt.date(2026, 6, 17))  # the report's Wednesday

    def test_weekend_is_not(self) -> None:
        assert not is_trading_day(dt.date(2026, 6, 20))  # Saturday

    def test_july_4_observed_holiday(self) -> None:
        # 2026-07-04 is a Saturday; observed Friday 2026-07-03 is closed.
        assert not is_trading_day(dt.date(2026, 7, 3))

    def test_full_day_close_is_1600(self) -> None:
        assert session_close_et(dt.date(2026, 6, 17)) == et(2026, 6, 17, 16, 0)

    def test_half_day_close_is_1300(self) -> None:
        # Black Friday 2026-11-27 is a shortened session.
        assert session_close_et(dt.date(2026, 11, 27)) == et(2026, 11, 27, 13, 0)

    def test_open_is_0930(self) -> None:
        assert session_open_et(dt.date(2026, 6, 17)) == et(2026, 6, 17, 9, 30)


class TestWindows:
    START, END = dt.time(9, 46), dt.time(11, 30)

    def test_report_execution_time_inside(self) -> None:
        assert in_time_window(et(2026, 6, 17, 10, 15), self.START, self.END)

    def test_orb_period_outside(self) -> None:
        assert not in_time_window(et(2026, 6, 17, 9, 45), self.START, self.END)

    def test_start_inclusive_end_exclusive(self) -> None:
        assert in_time_window(et(2026, 6, 17, 9, 46), self.START, self.END)
        assert not in_time_window(et(2026, 6, 17, 11, 30), self.START, self.END)

    def test_window_evaluated_in_exchange_time(self) -> None:
        # 17:15 Riyadh == 10:15 ET -> inside, even though 17:15 > 11:30.
        riyadh = dt.timezone(dt.timedelta(hours=3))
        machine = dt.datetime(2026, 6, 17, 17, 15, tzinfo=riyadh)
        assert in_time_window(machine, self.START, self.END)


class TestForceFlat:
    CONFIGURED = dt.time(15, 30)
    BUFFER = dt.timedelta(minutes=30)

    def test_full_day_uses_configured(self) -> None:
        ff = effective_force_flat_et(dt.date(2026, 6, 17), self.CONFIGURED, self.BUFFER)
        assert ff == et(2026, 6, 17, 15, 30)

    def test_half_day_pulls_forward(self) -> None:
        # Half-day closes 13:00: configured 15:30 would fire after the close.
        ff = effective_force_flat_et(dt.date(2026, 11, 27), self.CONFIGURED, self.BUFFER)
        assert ff == et(2026, 11, 27, 12, 30)
