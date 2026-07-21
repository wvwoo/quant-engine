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
from qts_core.store import StateStore
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


class TestRailSurvivesProviderFailure:
    """N-02: exit management lived ONLY inside session.step(view), and main()
    skipped straight past it whenever the provider raised or the B4 gate went
    false. The rail was therefore conditional on the exact data feed most
    likely to be broken at the moment it matters — and yfinance throttling is
    documented as expected, not exotic."""

    def _holding(self, db: Path) -> None:
        """Put a real open position in the ledger via the normal entry path."""
        from qts_core.broker import PaperBroker
        from qts_core.paper import PaperSession

        s = PaperSession(
            StateStore(db),
            PaperBroker(CFG, CFG.tick_schedule_for("NVDA")),
            CFG,
            session_date=SESSION,
            session_open_et=dt.datetime(2026, 6, 17, 9, 30, tzinfo=NY),
            force_flat_at=dt.datetime(2026, 6, 17, 15, 30, tzinfo=NY),
        )
        r = s.step(make_view(NOW))
        assert r.position is not None and r.position.contracts > 0
        s.store.close()

    def _late_clock(self) -> type[FakeClock]:
        class LateClock(FakeClock):
            def now_utc(self) -> dt.datetime:
                return dt.datetime(2026, 6, 17, 15, 35, tzinfo=NY).astimezone(dt.UTC)

        return LateClock

    def test_provider_exception_does_not_strand_an_open_position(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "p.db"
        self._holding(db)
        monkeypatch.setattr(live, "TradingClock", self._late_clock())
        monkeypatch.setattr(live, "YFinanceSource", lambda s: FakeSource(s, raises=True))
        monkeypatch.setattr(live, "StrategyConfig", lambda: CFG)
        live.main(["--db", str(db), "--symbols", "NVDA"])
        store = StateStore(db)
        sells = [o for o in store.orders_for_session(SESSION) if o["side"] == "SELL"]
        assert len(sells) == 1, "the position was left unmanaged when the provider failed"
        assert sells[0]["reason"] == "FORCE_FLAT_UNMARKED"
        assert store.open_positions() == {}

    def test_b4_gate_going_false_does_not_strand_an_open_position(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db = tmp_path / "p.db"
        self._holding(db)
        monkeypatch.setattr(live, "TradingClock", self._late_clock())
        monkeypatch.setattr(live, "YFinanceSource", lambda s: FakeSource(s, has_expiry=False))
        monkeypatch.setattr(live, "StrategyConfig", lambda: CFG)
        live.main(["--db", str(db), "--symbols", "NVDA"])
        store = StateStore(db)
        sells = [o for o in store.orders_for_session(SESSION) if o["side"] == "SELL"]
        assert len(sells) == 1
        assert sells[0]["reason"] == "FORCE_FLAT_UNMARKED"

    def test_before_force_flat_a_feed_failure_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The rail fires on time, not early: a feed failure at 10:15 must not
        liquidate a healthy position."""
        db = tmp_path / "p.db"
        self._holding(db)
        _wire(monkeypatch, lambda s: FakeSource(s, raises=True))
        live.main(["--db", str(db), "--symbols", "NVDA"])
        store = StateStore(db)
        assert [o for o in store.orders_for_session(SESSION) if o["side"] == "SELL"] == []
        assert store.open_positions() != {}


class TestStepFailureIsContained:
    """N-06: the provider call was guarded, but the step that owns position
    state was not — so one symbol's sqlite error killed the whole pass, skipped
    every remaining symbol, and left the report silently stale on disk."""

    def test_one_symbol_step_failure_does_not_skip_the_rest(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import qts_core.paper as paper_mod

        real_step = paper_mod.PaperSession.step
        seen: list[str] = []

        def flaky_step(self, view):  # type: ignore[no-untyped-def]
            symbol = view.meta.get("symbol", "")
            seen.append(symbol)
            if symbol == "QQQ":
                raise RuntimeError("database is locked")
            return real_step(self, view)

        monkeypatch.setattr(paper_mod.PaperSession, "step", flaky_step)
        _wire(monkeypatch, lambda s: FakeSource(s))
        report = tmp_path / "r.md"
        rc = live.main(
            ["--db", str(tmp_path / "p.db"), "--symbols", "QQQ,NVDA", "--report", str(report)]
        )
        assert rc == 0
        assert "NVDA" in seen, "a step failure on QQQ skipped every later symbol"
        assert "[error] QQQ" in capsys.readouterr().out
        assert report.exists(), "the report must still be written after a contained failure"


class TestUnknownSymbolIsContained:
    """The N-08 fail-loud KeyError must obey the N-06 containment rule: one
    unconfigured symbol costs THAT symbol, never the pass. Without this,
    --symbols AAPL,SPY died on AAPL before SPY ever ran — and an open SPY
    position went unmanaged, which is the exact hazard N-02 closed."""

    def test_unknown_symbol_skips_but_the_rest_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _wire(monkeypatch, lambda s: FakeSource(s))
        rc = live.main(["--db", str(tmp_path / "p.db"), "--symbols", "AAPL,NVDA"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[error] AAPL" in out
        assert "tick schedule" in out
        assert "--- NVDA ---" in out, "an unconfigured symbol killed the whole pass"
        assert "[decision] NVDA" in out


class TestStrandedWarningSurvivesWeekends:
    """Review finding F4: the N-03 warning's comment said 'every run' but the
    non-trading-day early return skipped it — the one day an owner reviews
    state at leisure was the one day the warning went silent."""

    def test_warning_prints_on_a_non_trading_day(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "p.db"
        store = StateStore(db)
        stale = "NVDA260616C00205000"
        store.save_position(
            stale,
            {
                "symbol": "NVDA",
                "occ_symbol": stale,
                "phase": "OPEN",
                "contracts": 2,
                "entry_quote_cents": 420,
                "cost_basis_per_contract_cents": 42400,
                "peak_premium_cents": 420,
                "realized_pnl_cents": 0,
            },
            dt.datetime(2026, 6, 16, 15, 0, tzinfo=NY),
        )
        store.close()

        class SaturdayClock(FakeClock):
            def session_date(self) -> dt.date:
                return dt.date(2026, 6, 20)

        monkeypatch.setattr(live, "TradingClock", SaturdayClock)
        monkeypatch.setattr(live, "StrategyConfig", lambda: CFG)
        rc = live.main(["--db", str(db)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "expired UNSETTLED" in out
        assert stale in out

    def test_no_db_is_not_invented_on_a_weekend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class SaturdayClock(FakeClock):
            def session_date(self) -> dt.date:
                return dt.date(2026, 6, 20)

        monkeypatch.setattr(live, "TradingClock", SaturdayClock)
        monkeypatch.setattr(live, "StrategyConfig", lambda: CFG)
        db = tmp_path / "never" / "p.db"
        assert live.main(["--db", str(db)]) == 0
        assert not db.exists(), "a warning pass must not create state"


class TestConfigGapDoesNotStrandItsOwnPosition:
    """Review F2 (confirmed at HEAD): the KeyError containment `continue`d
    BEFORE the session was built, so the unconfigured symbol's OWN open
    position lost its exits AND its force-flat — a config problem stranding a
    position, which ADR-008 forbids. The unmarked force-flat needs no tick
    schedule at all, so a strand-free handling was always available."""

    def _open_position_for(self, db: Path, symbol: str, occ: str) -> None:
        store = StateStore(db)
        store.save_position(
            occ,
            {
                "symbol": symbol,
                "occ_symbol": occ,
                "phase": "OPEN",
                "contracts": 2,
                "entry_quote_cents": 420,
                "cost_basis_per_contract_cents": 42400,
                "peak_premium_cents": 420,
                "realized_pnl_cents": 0,
            },
            dt.datetime(2026, 6, 17, 10, 30, tzinfo=NY),
        )
        store.close()

    def test_unconfigured_symbol_still_force_flats_after_the_bell(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "p.db"
        occ = "AAPL260617C00200000"  # today's expiry, position opened earlier
        self._open_position_for(db, "AAPL", occ)

        class LateClock(FakeClock):
            def now_utc(self) -> dt.datetime:
                return dt.datetime(2026, 6, 17, 15, 35, tzinfo=NY).astimezone(dt.UTC)

        monkeypatch.setattr(live, "TradingClock", LateClock)
        monkeypatch.setattr(live, "YFinanceSource", lambda s: FakeSource(s))
        monkeypatch.setattr(live, "StrategyConfig", lambda: CFG)
        rc = live.main(["--db", str(db), "--symbols", "AAPL"])
        assert rc == 0
        store = StateStore(db)
        sells = [o for o in store.orders_for_session(SESSION) if o["side"] == "SELL"]
        assert len(sells) == 1, "the config gap stranded the symbol's own position"
        assert sells[0]["reason"] == "FORCE_FLAT_UNMARKED"
        assert store.open_positions(as_of=SESSION) == {}

    def test_before_the_bell_the_position_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db = tmp_path / "p.db"
        occ = "AAPL260617C00200000"
        self._open_position_for(db, "AAPL", occ)
        _wire(monkeypatch, lambda s: FakeSource(s))  # clock at 10:15
        live.main(["--db", str(db), "--symbols", "AAPL"])
        store = StateStore(db)
        assert [o for o in store.orders_for_session(SESSION) if o["side"] == "SELL"] == []


class TestAlpacaBridgeWiring:
    """ADR-012 wiring: the bridge is opt-in per run (--broker), fails closed
    without keys, and the one-db-one-backend guard fires before any network."""

    def test_missing_keys_halt_cleanly_before_any_network(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def exploding_source(symbol: str) -> FakeSource:  # pragma: no cover - never runs
            raise AssertionError("provider reached without broker credentials")

        _wire(monkeypatch, exploding_source)
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
        rc = live.main(["--db", str(tmp_path / "p.db"), "--broker", "alpaca_paper"])
        assert rc == 2
        assert "PAPER account keys" in capsys.readouterr().out

    def test_backend_mixing_is_refused_at_boot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "p.db"
        claimed = StateStore(db)
        claimed.assert_backend("alpaca_paper")
        claimed.close()
        _wire(monkeypatch, lambda s: FakeSource(s))
        rc = live.main(["--db", str(db)])  # default backend: model
        assert rc == 2
        assert "fresh --db" in capsys.readouterr().out

    def test_default_run_claims_the_db_as_model(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db = tmp_path / "p.db"
        _wire(monkeypatch, lambda s: FakeSource(s))
        assert live.main(["--db", str(db), "--symbols", "NVDA"]) == 0
        assert StateStore(db).backend() == "model"
