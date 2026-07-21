"""run_backtest CLI tests.

qts_core/run_backtest.py measured 0.0% coverage — and a naive grep hid it: the
`run_backtest` FUNCTION lives in qts_core/backtest.py and IS tested, so
`grep run_backtest tests/` reads as covered while the 193-line MODULE of the
same name has never been executed. Bars are synthetic and injected; no wire.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path

import pytest

from qts_core import run_backtest as rb
from qts_core.clock import NY
from qts_core.config import LiveTradingBlocked, StrategyConfig
from qts_core.models import Bar

CFG = StrategyConfig(commission_per_contract_cents=0)
# Three consecutive real XNYS sessions (Mon/Tue/Wed).
DAYS = [dt.date(2026, 6, 15), dt.date(2026, 6, 16), dt.date(2026, 6, 17)]
NOW = dt.datetime(2026, 6, 17, 16, 0, tzinfo=NY).astimezone(dt.UTC)


def _session_bars(day: dt.date, *, rising: bool, bars: int = 73) -> list[Bar]:
    """5-minute bars from 09:35 ET. The default run reaches 15:35, i.e. past
    force-flat, so the session counts as COMPLETE — build_sessions drops
    in-progress days on purpose (N-05)."""
    out: list[Bar] = []
    base = 200.0
    for i in range(bars):
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


class TestIncompleteSessionsAreExcluded:
    """N-05: running mid-session appended TODAY as a full SessionData. An entry
    approved at 10:05 was then force-closed at the last available bar and
    recorded as a completed trade under the END_OF_DATA reason — which nothing
    printed, so a partial day entered the statistics silently while the CLI
    claimed the window was "NOT cherry-picked"."""

    def test_in_progress_session_is_dropped_and_announced(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rows: list[Bar] = []
        for day in DAYS[:2]:
            rows.extend(_session_bars(day, rising=False))
        # today: only the morning has happened
        rows.extend(_session_bars(DAYS[2], rising=True, bars=24))
        monkeypatch.setattr(rb, "YFinanceSource", lambda s: FakeSource(s, bars=rows))
        sessions = rb.build_sessions("SPY", 30, CFG, NOW)
        assert DAYS[2] not in [s.session_date for s in sessions]
        assert "excluded 1 incomplete session" in capsys.readouterr().out


class TestArtifacts:
    """G-12: results existed only on stdout, so the owner could not review a
    run once the terminal scrolled."""

    def test_no_artifacts_are_written_while_the_flag_is_off(
        self, wired: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QTS_BACKTEST_DIR", str(tmp_path / "arts"))
        rb.main(["--symbol", "SPY", "--days", "30"])
        assert not (tmp_path / "arts").exists()

    def test_enabled_flag_writes_tagged_json_and_markdown(
        self, wired: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QTS_BACKTEST_DIR", str(tmp_path / "arts"))
        monkeypatch.setattr(
            rb, "StrategyConfig", lambda: dataclasses.replace(CFG, backtest_artifacts=True)
        )
        rb.main(["--symbol", "SPY", "--days", "30"])
        out = tmp_path / "arts"
        payloads = sorted(out.glob("*.json"))
        markdowns = sorted(out.glob("*.md"))
        assert len(payloads) == 1 and len(markdowns) == 1

        data = json.loads(payloads[0].read_text())
        assert data["modeled"] is True
        assert "B2" in data["barrier_open"]
        assert "MODELED" in data["modeled_note"]
        # the tag must travel WITH the numbers, not just sit in a header
        assert "net_pnl_cents_MODELED" in data["results"]
        assert data["results"]["modeled"] is True

        md = markdowns[0].read_text()
        assert "MODELED" in md.split("\n")[2], "the banner must precede every figure"
        assert "not a forecast" in md
        assert "| MODELED |" in md

    def test_artifact_name_is_deterministic(self) -> None:
        from qts_core.artifacts import artifact_stem

        stamp = dt.datetime(2026, 6, 17, 16, 5, tzinfo=NY)
        assert artifact_stem("spy", 30, stamp) == artifact_stem("SPY", 30, stamp)
        assert artifact_stem("SPY", 30, stamp) == "SPY-30d-20260617T160500"


class TestArtifactDirIsCwdIndependent:
    """Found by RUNNING it: the API was served by a process whose cwd was not
    the repo root, so /api/backtest reported "no saved backtest" with complete
    confidence while the file sat on disk. A confident false negative reads as
    a fact, which is worse than an error."""

    def test_default_dir_does_not_move_with_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qts_core.artifacts import default_dir

        monkeypatch.delenv("QTS_BACKTEST_DIR", raising=False)
        here = default_dir()
        monkeypatch.chdir(tmp_path)
        assert default_dir() == here, "artifact path drifted with the process cwd"
        assert here.is_absolute()

    def test_env_override_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qts_core.artifacts import default_dir

        monkeypatch.setenv("QTS_BACKTEST_DIR", str(tmp_path / "elsewhere"))
        assert default_dir() == tmp_path / "elsewhere"

    def test_written_artifact_is_found_from_a_different_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qts_core.artifacts import latest_payload, write_artifacts
        from qts_core.backtest import BacktestResult

        monkeypatch.setenv("QTS_BACKTEST_DIR", str(tmp_path / "arts"))
        res = BacktestResult(
            modeled=True,
            assumptions={},
            sessions=1,
            signal_count=0,
            trades=(),
            net_pnl_cents=0,
            win_rate=None,
            profit_factor=None,
            max_drawdown_cents=0,
            sharpe=None,
        )
        write_artifacts(
            res,
            symbol="SPY",
            days=30,
            atm_iv=0.20,
            generated_at=dt.datetime(2026, 6, 17, 16, 0, tzinfo=NY),
        )
        monkeypatch.chdir(tmp_path / "arts")  # serve from somewhere else entirely
        found = latest_payload()
        assert found is not None, "a saved artifact was reported as missing"
        assert found["symbol"] == "SPY"
