"""Paper session tests: the full decide->journal->fill->persist loop, the
kill-9 resume contract, and the safety halts."""

from __future__ import annotations

import contextlib
import datetime as dt
from pathlib import Path

from qts_core.broker import PaperBroker
from qts_core.clock import NY
from qts_core.config import StrategyConfig
from qts_core.paper import PaperSession, load_fixture_session
from qts_core.risk import Phase
from qts_core.store import StateStore
from tests.test_checklist import OPEN, SESSION, make_view

CFG = StrategyConfig(commission_per_contract_cents=0)  # report arithmetic mode
FLAT_AT = dt.datetime(2026, 6, 17, 15, 30, tzinfo=NY)


def make_session(db: Path, cfg: StrategyConfig = CFG) -> PaperSession:
    return PaperSession(
        StateStore(db),
        PaperBroker(cfg, cfg.tick_schedule_for("NVDA")),
        cfg,
        session_date=SESSION,
        session_open_et=OPEN,
        force_flat_at=FLAT_AT,
    )


def et(hh: int, mm: int) -> dt.datetime:
    return dt.datetime(2026, 6, 17, hh, mm, tzinfo=NY)


class TestEntryFlow:
    def test_approved_entry_opens_position(self, tmp_path: Path) -> None:
        s = make_session(tmp_path / "s.db")
        result = s.step(make_view())
        assert result.decision is not None and result.decision.approved
        assert len(result.fills) == 1
        assert result.position is not None
        assert result.position.phase is Phase.OPEN
        assert result.position.contracts >= 1
        # journaled + filled in the store
        assert s.store.unfilled_intents() == []
        orders = s.store.orders_for_session(SESSION)
        assert len(orders) == 1
        assert orders[0]["status"] == "FILLED"
        assert orders[0]["side"] == "BUY"

    def test_vetoed_entry_logs_decision_no_order(self, tmp_path: Path) -> None:
        s = make_session(tmp_path / "s.db")
        result = s.step(make_view(breakout=False))
        assert result.decision is not None and not result.decision.approved
        assert result.fills == ()
        assert s.store.orders_for_session(SESSION) == []
        assert len(s.store.decisions_for_session(SESSION)) == 1

    def test_same_view_twice_single_position(self, tmp_path: Path) -> None:
        # Second step sees the open position and manages exits — it must NOT
        # open a second position on the same signal.
        s = make_session(tmp_path / "s.db")
        s.step(make_view())
        r2 = s.step(make_view())
        buys = [o for o in s.store.orders_for_session(SESSION) if o["side"] == "BUY"]
        assert len(buys) == 1
        assert r2.position is not None


class TestKill9Resume:
    def test_restart_after_fill_resumes_same_position(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        s1 = make_session(db)
        r1 = s1.step(make_view())
        assert r1.position is not None
        s1.store.close()  # kill -9

        s2 = make_session(db)  # fresh process, fresh broker
        recovered = s2.any_open_position()
        assert recovered is not None
        assert recovered.occ_symbol == r1.position.occ_symbol
        assert recovered.cost_basis_per_contract_cents == (
            r1.position.cost_basis_per_contract_cents
        )
        # stepping again manages the EXISTING position; no duplicate BUY
        s2.step(make_view())
        buys = [o for o in s2.store.orders_for_session(SESSION) if o["side"] == "BUY"]
        assert len(buys) == 1

    def test_seq_never_reused_across_restart(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        s1 = make_session(db)
        s1.step(make_view())
        s1.store.close()
        s2 = make_session(db)
        assert s2.store.next_seq(SESSION) == 1


class TestExitFlow:
    def _entered(self, tmp_path: Path) -> PaperSession:
        s = make_session(tmp_path / "s.db")
        r = s.step(make_view())
        assert r.position is not None
        return s

    def _view_with_mark(self, mark_cents: int, now: dt.datetime):  # type: ignore[no-untyped-def]
        import dataclasses as dc

        view = make_view(now)
        pos_quote = view.chain[0]
        marked = dc.replace(
            pos_quote, bid_cents=mark_cents - 1, ask_cents=mark_cents + 1, received_at=now
        )
        return dc.replace(view, chain=(marked, *view.chain[1:]))

    def test_stop_exit_realizes_loss(self, tmp_path: Path) -> None:
        s = self._entered(tmp_path)
        pos = s.any_open_position()
        assert pos is not None
        stop = pos.stop_level(CFG)
        r = s.step(self._view_with_mark(stop - 2, et(10, 30)))
        assert r.position is not None and r.position.phase is Phase.CLOSED
        sells = [o for o in s.store.orders_for_session(SESSION) if o["side"] == "SELL"]
        assert len(sells) == 1
        assert sells[0]["reason"] == "STOP"
        assert s.store.realized_pnl_today(SESSION) < 0

    def test_tranche1_then_position_persists_as_runner(self, tmp_path: Path) -> None:
        s = self._entered(tmp_path)
        pos = s.any_open_position()
        assert pos is not None
        target = pos.tranche1_level(CFG)
        r = s.step(self._view_with_mark(target + 5, et(10, 45)))
        assert r.position is not None and r.position.phase is Phase.RUNNER
        assert r.position.contracts == pos.contracts - 1
        # restart mid-runner: phase survives
        s.store.close()
        s2 = make_session(tmp_path / "s.db")
        recovered = s2.any_open_position()
        assert recovered is not None and recovered.phase is Phase.RUNNER

    def test_force_flat_closes_at_any_profit(self, tmp_path: Path) -> None:
        s = self._entered(tmp_path)
        r = s.step(self._view_with_mark(500, et(15, 30)))
        assert r.position is not None and r.position.phase is Phase.CLOSED
        sells = s.store.orders_for_session(SESSION)
        assert any(o["reason"] == "FORCE_FLAT" for o in sells)


class TestSafetyHalts:
    def test_daily_loss_limit_blocks_new_entries(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        s = make_session(db)
        # Inject a big realized loss directly through the ledger.
        from qts_core.store import OrderIntent, client_order_id

        coid = client_order_id("qts-v1", SESSION, "X", "SELL-STOP", 0)
        s.store.journal_intent(
            OrderIntent(coid, SESSION, "qts-v1", "X", "SELL", 1, 100, "STOP", 0), et(10, 0)
        )
        s.store.record_fill(coid, et(10, 0), 100, -CFG.daily_loss_limit_cents, 0)
        r = s.step(make_view())
        assert r.halted == "DAILY_LOSS_LIMIT"
        assert [o for o in s.store.orders_for_session(SESSION) if o["side"] == "BUY"] == []

    def test_after_force_flat_no_new_entries(self, tmp_path: Path) -> None:
        s = make_session(tmp_path / "s.db")
        r = s.step(make_view(et(15, 45)))
        assert r.halted == "AFTER_FORCE_FLAT"


class TestFixtureRoundtrip:
    def test_fixture_serialization_roundtrip(self, tmp_path: Path) -> None:
        import json

        view = make_view()
        blob = {
            "snapshots": [
                {
                    "now": view.now.isoformat(),
                    "session_date": view.session_date.isoformat(),
                    "bars": [
                        {
                            "ts_close": b.ts_close.isoformat(),
                            "open": b.open,
                            "high": b.high,
                            "low": b.low,
                            "close": b.close,
                            "volume": b.volume,
                        }
                        for b in view.bars
                    ],
                    "prior_sessions": [
                        [
                            {
                                "ts_close": b.ts_close.isoformat(),
                                "open": b.open,
                                "high": b.high,
                                "low": b.low,
                                "close": b.close,
                                "volume": b.volume,
                            }
                            for b in sess
                        ]
                        for sess in view.prior_sessions
                    ],
                    "chain": [
                        {
                            "underlying": q.underlying,
                            "expiry": q.expiry.isoformat(),
                            "strike_cents": q.strike_cents,
                            "right": q.right,
                            "bid_cents": q.bid_cents,
                            "ask_cents": q.ask_cents,
                            "volume": q.volume,
                            "open_interest": q.open_interest,
                            "received_at": q.received_at.isoformat(),
                            "iv_hint": q.iv_hint,
                        }
                        for q in view.chain
                    ],
                    "underlying_last": view.underlying_last,
                    "meta": view.meta,
                }
            ]
        }
        p = tmp_path / "fixture.json"
        p.write_text(json.dumps(blob))
        views = load_fixture_session(p)
        assert len(views) == 1
        assert views[0].bars == view.bars
        assert views[0].chain == view.chain


class TestCrashBetweenFillAndSave:
    """The window the original TestKill9Resume did NOT cover.

    step() order is: journal_intent -> broker.execute -> record_fill ->
    save_position. A crash BETWEEN record_fill and save_position leaves a
    FILLED order with no position row. On restart the session sees itself as
    flat, while next_seq has already advanced past the filled order — so the
    replayed intent gets a DIFFERENT client_order_id and becomes a genuine
    second order. Found independently by 3 reviewers (QTS-1/QTS-2/QTS-R1).
    """

    def _crash_after_fill(self, db: Path, view_factory) -> None:  # type: ignore[no-untyped-def]
        """Run one step but die right after record_fill."""
        s = make_session(db)
        original_save = s.store.save_position

        def die(*_a: object, **_k: object) -> None:
            raise KeyboardInterrupt("simulated kill -9 after record_fill")

        s.store.save_position = die  # type: ignore[method-assign]
        with contextlib.suppress(KeyboardInterrupt):
            s.step(view_factory())
        del original_save
        s.store.close()

    def test_entry_not_duplicated_after_crash(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        self._crash_after_fill(db, make_view)

        s2 = make_session(db)
        buys_before = [o for o in s2.store.orders_for_session(SESSION) if o["side"] == "BUY"]
        assert len(buys_before) == 1
        assert buys_before[0]["status"] == "FILLED"

        # Restart + step: the filled entry must be recognised, NOT re-issued.
        s2.step(make_view())
        buys_after = [o for o in s2.store.orders_for_session(SESSION) if o["side"] == "BUY"]
        assert len(buys_after) == 1, "restart issued a duplicate entry order"

        pos = s2.any_open_position()
        assert pos is not None, "filled entry was lost — position not recovered"
        assert pos.contracts == buys_before[0]["contracts"]

    def test_recovered_basis_matches_the_actual_fill(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        self._crash_after_fill(db, make_view)
        s2 = make_session(db)
        order = next(o for o in s2.store.orders_for_session(SESSION) if o["side"] == "BUY")
        pos = s2.any_open_position()
        assert pos is not None
        expected = order["fill_premium_cents"] * 100 + CFG.commission_per_contract_cents
        assert pos.cost_basis_per_contract_cents == expected

    def test_exit_not_duplicated_after_crash(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        s = make_session(db)
        r = s.step(make_view())
        assert r.position is not None
        stop = r.position.stop_level(CFG)

        # crash right after the SELL fill is recorded
        def die(*_a: object, **_k: object) -> None:
            raise KeyboardInterrupt("simulated kill -9 after record_fill")

        exit_view = TestExitFlow()._view_with_mark(stop - 2, et(10, 30))
        s.store.save_position = die  # type: ignore[method-assign]
        with contextlib.suppress(KeyboardInterrupt):
            s.step(exit_view)
        s.store.close()

        s2 = make_session(db)
        s2.step(exit_view)
        sells = [o for o in s2.store.orders_for_session(SESSION) if o["side"] == "SELL"]
        assert len(sells) == 1, "restart issued a duplicate exit order"
        pos = s2.any_open_position()
        assert pos is None, "position should be closed after the recovered STOP"


class TestStalePositionQuarantine:
    """QTS-2: a 0DTE position from an earlier session has EXPIRED."""

    def test_yesterdays_position_is_not_live(self, tmp_path: Path) -> None:
        s = make_session(tmp_path / "s.db")
        stale = {
            "symbol": "NVDA",
            "occ_symbol": "NVDA260616C00205000",
            "phase": "OPEN",
            "contracts": 2,
            "entry_quote_cents": 420,
            "cost_basis_per_contract_cents": 42420,
            "peak_premium_cents": 420,
            "realized_pnl_cents": 0,
        }
        s.store.save_position("NVDA260616C00205000", stale, et(10, 0))
        assert s.any_open_position() is None, "expired contract treated as live"
        assert s.expired_positions() == ["NVDA260616C00205000"]

    def test_todays_position_is_live(self, tmp_path: Path) -> None:
        s = make_session(tmp_path / "s.db")
        r = s.step(make_view())
        assert r.position is not None
        assert s.any_open_position() is not None
        assert s.expired_positions() == []
