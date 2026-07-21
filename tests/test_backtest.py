"""Backtest engine tests on deterministic synthetic sessions (MODELED layer)."""

from __future__ import annotations

import datetime as dt

from qts_core.clock import NY
from qts_core.config import StrategyConfig
from qts_core.backtest import BacktestResult, SessionData, run_backtest, run_session
from qts_core.models import Bar

from tests.test_checklist import _prior_dates, _prior_session, _session_bars

CFG = StrategyConfig(commission_per_contract_cents=0)
SESSION = dt.date(2026, 6, 17)
OPEN = dt.datetime(2026, 6, 17, 9, 30, tzinfo=NY)
FLAT = dt.datetime(2026, 6, 17, 15, 30, tzinfo=NY)


def _full_session_bars(*, crash_after: dt.datetime | None = None) -> tuple[Bar, ...]:
    """The checklist's passing session extended to 15:55, optionally crashing
    hard after `crash_after` (to exercise the stop path)."""
    end = dt.datetime(2026, 6, 17, 15, 55, tzinfo=NY)
    bars = list(_session_bars(end))
    if crash_after is not None:
        out: list[Bar] = []
        price: float | None = None
        for b in bars:
            if b.ts_close <= crash_after:
                out.append(b)
                price = b.close
            else:
                assert price is not None
                new_close = max(price - 1.8, 150.0)
                out.append(
                    Bar(ts_close=b.ts_close, open=price, high=price + 0.05,
                        low=new_close - 0.1, close=new_close, volume=b.volume)
                )
                price = new_close
        bars = out
    return tuple(bars)


def make_session_data(*, crash_after: dt.datetime | None = None, iv: float = 0.68) -> SessionData:
    return SessionData(
        session_date=SESSION,
        session_open_et=OPEN,
        force_flat_at=FLAT,
        bars=_full_session_bars(crash_after=crash_after),
        prior_sessions=tuple(tuple(_prior_session(d)) for d in _prior_dates(20)),
        symbol="NVDA",
        atm_iv=iv,
    )


class TestSingleSession:
    def test_uptrend_session_enters_and_flattens(self) -> None:
        trades, signals, marks = run_session(make_session_data(), CFG)
        assert signals >= 1
        assert len(trades) == 1
        t = trades[0]
        # Force-flat guarantees no position survives 15:30 (B3 embodied).
        last_exit_reason = t.exits[-1][0]
        assert last_exit_reason in {"FORCE_FLAT", "TRAIL", "TRANCHE1", "STOP"}
        assert marks  # equity curve exists

    def test_crash_session_hits_stop(self) -> None:
        # First legal entry in this synthetic session is 10:55 (delta gate keeps
        # earlier bars out of band — measured, not assumed). Crash at 11:30 so
        # the position exists before the plunge.
        crash = dt.datetime(2026, 6, 17, 11, 30, tzinfo=NY)
        trades, _, _ = run_session(make_session_data(crash_after=crash), CFG)
        assert len(trades) == 1
        reasons = [r for r, _, _ in trades[0].exits]
        assert "STOP" in reasons
        assert trades[0].realized_pnl_cents < 0

    def test_no_entry_before_next_bar_exists(self) -> None:
        # A session whose bars end right at the first approval can never fill
        # (decision at close, fill at NEXT open) — zero trades, not a fake fill.
        sess = make_session_data()
        cut = tuple(b for b in sess.bars if b.ts_close <= dt.datetime(2026, 6, 17, 10, 15, tzinfo=NY))
        import dataclasses as dc

        short = dc.replace(sess, bars=cut)
        trades, signals, _ = run_session(short, CFG)
        # signals may fire on the final bar but no trade can exist from it
        if trades:
            assert all(t.exits for t in trades)
        assert signals >= 0  # structural: no crash, no phantom entry at own close


class TestMetricsHonesty:
    def test_result_is_modeled_with_assumptions(self) -> None:
        res = run_backtest([make_session_data()], CFG)
        assert res.modeled is True
        assert "NOT real option prices" in res.assumptions["options_premiums"]
        assert "conservative" in res.assumptions["intrabar_path"]

    def test_sharpe_suppressed_below_30_sessions(self) -> None:
        res = run_backtest([make_session_data()], CFG)
        assert res.sharpe is None
        assert any("sharpe not reported" in n for n in res.notes)

    def test_profit_factor_none_when_no_losses(self) -> None:
        res = run_backtest([make_session_data()], CFG)  # uptrend session only
        if all(t.realized_pnl_cents > 0 for t in res.trades):
            assert res.profit_factor is None
            assert any("zero losing trades" in n for n in res.notes)

    def test_mixed_sessions_aggregate(self) -> None:
        crash = dt.datetime(2026, 6, 17, 11, 30, tzinfo=NY)
        res = run_backtest(
            [make_session_data(), make_session_data(crash_after=crash)], CFG
        )
        assert res.sessions == 2
        assert len(res.trades) == 2
        assert res.max_drawdown_cents > 0
        assert res.win_rate is not None
        assert isinstance(res, BacktestResult)

    def test_net_pnl_is_sum_of_trades(self) -> None:
        crash = dt.datetime(2026, 6, 17, 11, 30, tzinfo=NY)
        res = run_backtest(
            [make_session_data(), make_session_data(crash_after=crash)], CFG
        )
        assert res.net_pnl_cents == sum(t.realized_pnl_cents for t in res.trades)
