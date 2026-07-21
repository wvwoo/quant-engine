"""run_backtest CLI tests.

qts_core/run_backtest.py measured 0.0% coverage — and a naive grep hid it: the
`run_backtest` FUNCTION lives in qts_core/backtest.py and IS tested, so
`grep run_backtest tests/` reads as covered while the 193-line MODULE of the
same name has never been executed. Bars are synthetic and injected; no wire.
"""

from __future__ import annotations

import datetime as dt

import pytest

from qts_core import run_backtest as rb
from qts_core.clock import NY
from qts_core.config import LiveTradingBlocked, StrategyConfig
from qts_core.models import Bar

CFG = StrategyConfig(commission_per_contract_cents=0)
# Three consecutive real XNYS sessions (Mon/Tue/Wed).
DAYS = [dt.date(2026, 6, 15), dt.date(2026, 6, 16), dt.date(2026, 6, 17)]
NOW = dt.datetime(2026, 6, 17, 16, 0, tzinfo=NY).astimezone(dt.UTC)


def _session_bars(day: dt.date, *, rising: bool) -> list[Bar]:
    """5-minute bars 09:35..11:30 ET, monotone or flat but always valid OHLC."""
    out: list[Bar] = []
    base = 200.0
    for i in range(24):
        close_t = dt.datetime.combine(day, dt.time(9, 35), tzinfo=NY) + dt.timedelta(minutes=5 * i)
        drift = 0.25 * i if rising else 0.0
        o = base + drift
        c = o + (0.2 if rising else 0.0)
        out.append(
            Bar(
                ts_close=close_t,
                open=o,
                high=max(o, c) + 0.1,
                low=min(o, c) - 0.1,
                close=c,
                volume=1_000 + 50 * i,
            )
        )
    return out


class FakeSource:
    def __init__(self, symbol: str, bars: list[Bar] | None = None) -> None:
        self.symbol = symbol
        self._bars = bars

    def fetch_bars(self, *, now: dt.datetime, days: int = 10, interval_min: int = 5) -> list[Bar]:
        if self._bars is not None:
            return self._bars
        out: list[Bar] = []
        for i, day in enumerate(DAYS):
            out.extend(_session_bars(day, rising=(i == len(DAYS) - 1)))
        return out


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rb, "YFinanceSource", FakeSource)

    class FrozenClock:
        @classmethod
        def system(cls) -> FrozenClock:
            return cls()

        def now_utc(self) -> dt.datetime:
            return NOW

    monkeypatch.setattr(rb, "TradingClock", FrozenClock)


class TestBuildSessions:
    def test_groups_bars_into_sessions_oldest_first(self, wired: None) -> None:
        sessions = rb.build_sessions("SPY", 30, CFG, NOW)
        # The first day has no prior baseline, so RVOL is undefined and it is
        # correctly dropped rather than silently scored against nothing.
        assert [s.session_date for s in sessions] == DAYS[1:]
        assert sessions[0].prior_sessions, "a session without priors cannot compute RVOL"

    def test_priors_are_capped_by_the_configured_lookback(self, wired: None) -> None:
        sessions = rb.build_sessions("SPY", 30, CFG, NOW)
        for s in sessions:
            assert len(s.prior_sessions) <= CFG.rvol_lookback_days

    def test_force_flat_is_calendar_aware(self, wired: None) -> None:
        for s in rb.build_sessions("SPY", 30, CFG, NOW):
            assert s.force_flat_at <= dt.datetime.combine(
                s.session_date, CFG.force_flat_et, tzinfo=NY
            )

    def test_no_usable_sessions_is_reported_not_crashed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(rb, "YFinanceSource", lambda s: FakeSource(s, bars=[]))
        rc = rb.main(["--symbol", "SPY", "--days", "5"])
        assert rc == 1
        assert "no usable sessions" in capsys.readouterr().out


class TestDiagnose:
    def test_diagnose_explains_which_gate_binds(
        self, wired: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Zero signals is a RESULT; --diagnose is what makes it an explained
        one rather than a silent $0.00."""
        rc = rb.main(["--symbol", "SPY", "--days", "30", "--diagnose"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "DIAGNOSIS" in out
        assert "fail rate per gate" in out
        assert "rvol" in out
        assert "BACKTEST ARTIFACT" in out, "the synthetic-chain caveat must stay visible"


class TestMainOutput:
    def test_every_number_is_preceded_by_its_assumptions(
        self, wired: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = rb.main(["--symbol", "SPY", "--days", "30"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "MODELED options P&L" in out
        assert "ASSUMPTIONS" in out
        assert out.index("ASSUMPTIONS") < out.index("net P&L"), (
            "assumptions must precede the numbers they qualify"
        )
        assert "not a forecast" in out

    def test_sharpe_is_withheld_below_thirty_sessions(
        self, wired: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rb.main(["--symbol", "SPY", "--days", "30"])
        out = capsys.readouterr().out
        assert "Sharpe             : not reported" in out

    def test_paper_mode_gate_runs_before_any_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def exploding(symbol: str) -> FakeSource:  # pragma: no cover - never runs
            raise AssertionError("provider was built before the paper-mode gate")

        monkeypatch.setattr(rb, "YFinanceSource", exploding)
        monkeypatch.setattr(rb, "StrategyConfig", lambda: StrategyConfig(live_trading=True))
        monkeypatch.delenv("QTS_LIVE_TRADING_OWNER_ACK", raising=False)
        with pytest.raises(LiveTradingBlocked):
            rb.main(["--symbol", "SPY"])
