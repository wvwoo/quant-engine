"""live.main() — the CLI that nothing exercised.

qts_core/live.py measured 0.0% coverage: no test file imported it, so the B4
halt, the provider-hiccup guard, the report write and the paper-mode gate were
asserted only by their own docstrings — one of which (G-02) was false.

Network stays off (pytest-socket, --disable-socket in pyproject): the provider
and the clock are injected, never reached.
"""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
from collections.abc import Callable
from pathlib import Path

import pytest

from qts_core import live
from qts_core.clock import NY
from qts_core.config import LiveTradingBlocked, StrategyConfig
from qts_core.models import MarketView
from tests.test_checklist import SESSION, make_view

CFG = StrategyConfig(commission_per_contract_cents=0)
NOW = dt.datetime(2026, 6, 17, 10, 15, tzinfo=NY)


class FakeClock:
    """The single source of 'now', frozen. Mirrors TradingClock's surface."""

    @classmethod
    def system(cls) -> FakeClock:
        return cls()

    def now_utc(self) -> dt.datetime:
        return NOW.astimezone(dt.UTC)

    def session_date(self) -> dt.date:
        return SESSION


class FakeSource:
    """Stands in for YFinanceSource: same surface, no network."""

    def __init__(self, symbol: str, has_expiry: bool = True, raises: bool = False) -> None:
        self.symbol = symbol
        self._has_expiry = has_expiry
        self._raises = raises

    def has_same_day_expiry(self, session_date: dt.date) -> bool:
        return self._has_expiry

    def build_view(self, *, now: dt.datetime, session_date: dt.date) -> MarketView:
        if self._raises:
            raise RuntimeError("provider hiccup")
        return dc.replace(make_view(NOW), meta={"symbol": self.symbol})


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[str], object],
    cfg: StrategyConfig = CFG,
) -> None:
    monkeypatch.setattr(live, "TradingClock", FakeClock)
    monkeypatch.setattr(live, "YFinanceSource", factory)
    monkeypatch.setattr(live, "StrategyConfig", lambda: cfg)


class TestPaperModeGate:
    """G-02: live.py claimed 'require_paper_mode() runs at import of the
    session'. It does not — it runs inside PaperSession.__init__, which
    live.main() never reaches if every symbol fails the B4 gate. The gate is
    now called explicitly in main(), BEFORE any network I/O."""

    def test_live_trading_config_is_refused_before_any_io(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def exploding_source(symbol: str) -> FakeSource:  # pragma: no cover - never runs
            raise AssertionError("provider was constructed before the paper-mode gate")

        _wire(monkeypatch, exploding_source, cfg=StrategyConfig(live_trading=True))
        monkeypatch.delenv("QTS_LIVE_TRADING_OWNER_ACK", raising=False)
        with pytest.raises(LiveTradingBlocked):
            live.main(["--db", str(tmp_path / "p.db")])

    def test_gate_runs_even_when_every_symbol_is_halted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The old placement could be skipped entirely: with no 0DTE listed for
        any symbol, no PaperSession was ever constructed."""
        _wire(
            monkeypatch,
            lambda s: FakeSource(s, has_expiry=False),
            cfg=StrategyConfig(live_trading=True),
        )
        monkeypatch.delenv("QTS_LIVE_TRADING_OWNER_ACK", raising=False)
        with pytest.raises(LiveTradingBlocked):
            live.main(["--db", str(tmp_path / "p.db"), "--symbols", "SPY"])


class TestB4Gate:
    def test_symbol_without_same_day_expiry_is_halted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _wire(monkeypatch, lambda s: FakeSource(s, has_expiry=False))
        rc = live.main(["--db", str(tmp_path / "p.db"), "--symbols", "NVDA"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no 0DTE expiry listed" in out
        assert "B4 gate" in out

    def test_non_trading_day_stops_before_the_provider(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class SaturdayClock(FakeClock):
            def session_date(self) -> dt.date:
                return dt.date(2026, 6, 20)  # a Saturday

        monkeypatch.setattr(live, "TradingClock", SaturdayClock)
        monkeypatch.setattr(live, "StrategyConfig", lambda: CFG)
        rc = live.main(["--db", str(tmp_path / "p.db")])
        assert rc == 0
        assert "not an XNYS session" in capsys.readouterr().out


class TestProviderResilience:
    def test_one_symbol_raising_does_not_kill_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def factory(symbol: str) -> FakeSource:
            return FakeSource(symbol, raises=(symbol == "QQQ"))

        _wire(monkeypatch, factory)
        rc = live.main(["--db", str(tmp_path / "p.db"), "--symbols", "QQQ,NVDA"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[error] QQQ" in out
        assert "--- NVDA ---" in out, "a hiccup on one symbol skipped the rest"


class TestStepAndReport:
    def test_step_runs_and_prints_the_reasoned_decision(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _wire(monkeypatch, lambda s: FakeSource(s))
        rc = live.main(["--db", str(tmp_path / "p.db"), "--symbols", "NVDA"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[view] NVDA" in out
        assert "[decision] NVDA" in out
        assert "rvol" in out, "per-gate reasoning must be printed, not just a verdict"

    def test_report_is_written_when_asked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _wire(monkeypatch, lambda s: FakeSource(s))
        report = tmp_path / "r.md"
        rc = live.main(
            ["--db", str(tmp_path / "p.db"), "--symbols", "NVDA", "--report", str(report)]
        )
        assert rc == 0
        text = report.read_text()
        assert "QTS PAPER TRADING REPORT" in text
        assert "MODELED FILLS" in text, "the honesty banner is not optional"

    def test_symbols_defaults_to_the_configured_universe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _wire(monkeypatch, lambda s: FakeSource(s))
        live.main(["--db", str(tmp_path / "p.db")])
        out = capsys.readouterr().out
        for symbol in CFG.tickers:
            assert f"--- {symbol} ---" in out
