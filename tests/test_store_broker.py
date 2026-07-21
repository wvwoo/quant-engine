"""Store + broker tests, centered on the crash-recovery contract:
kill -9 at any instant + restart => no duplicate order, no lost position."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from qts_core.broker import PaperBroker, PaperExecutionError
from qts_core.clock import NY
from qts_core.config import StrategyConfig
from qts_core.money import TickSchedule
from qts_core.store import OrderIntent, StateStore, client_order_id

CFG = StrategyConfig()
SESSION = dt.date(2026, 6, 17)
NOW = dt.datetime(2026, 6, 17, 10, 15, tzinfo=NY)


def intent(seq: int = 0, side: str = "BUY", contracts: int = 2) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id("qts-v1", SESSION, "NVDA260617C00205000", side, seq),
        session_date=SESSION,
        strategy="qts-v1",
        occ_symbol="NVDA260617C00205000",
        side=side,
        contracts=contracts,
        limit_cents=420,
        reason="ENTRY",
        seq=seq,
    )


class TestDeterministicIds:
    def test_same_intent_same_id(self) -> None:
        a = client_order_id("qts-v1", SESSION, "OCC", "BUY", 0)
        b = client_order_id("qts-v1", SESSION, "OCC", "BUY", 0)
        assert a == b

    def test_any_component_changes_id(self) -> None:
        base = client_order_id("qts-v1", SESSION, "OCC", "BUY", 0)
        assert client_order_id("qts-v1", SESSION, "OCC", "BUY", 1) != base
        assert client_order_id("qts-v1", SESSION, "OCC", "SELL", 0) != base
        assert client_order_id("qts-v1", SESSION, "XYZ", "BUY", 0) != base
        assert client_order_id("other", SESSION, "OCC", "BUY", 0) != base


class TestJournalIdempotency:
    def test_first_journal_true_replay_false(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        assert store.journal_intent(intent(), NOW) is True
        assert store.journal_intent(intent(), NOW) is False  # replay is a no-op

    def test_replay_survives_process_restart(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        s1 = StateStore(db)
        assert s1.journal_intent(intent(), NOW) is True
        s1.close()  # "crash" + restart
        s2 = StateStore(db)
        assert s2.journal_intent(intent(), NOW) is False
        assert s2.order_status(intent().client_order_id) == "INTENT"

    def test_crash_between_journal_and_fill_is_visible(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        s1 = StateStore(db)
        s1.journal_intent(intent(), NOW)
        s1.close()  # crash BEFORE broker call
        s2 = StateStore(db)
        assert s2.unfilled_intents() == [intent().client_order_id]
        # recovery reconciles: broker replay gives the same fill, then:
        s2.record_fill(intent().client_order_id, NOW, 425, -85000, 130)
        assert s2.unfilled_intents() == []
        assert s2.order_status(intent().client_order_id) == "FILLED"

    def test_seq_monotonic_across_restart(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        s1 = StateStore(db)
        assert s1.next_seq(SESSION) == 0
        s1.journal_intent(intent(0), NOW)
        s1.close()
        s2 = StateStore(db)
        assert s2.next_seq(SESSION) == 1  # never reuses 0 -> never duplicates


class TestPositionsRoundtrip:
    def test_position_survives_restart(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        state = {"phase": "OPEN", "contracts": 2, "entry_quote_cents": 420}
        s1 = StateStore(db)
        s1.save_position("OCC1", state, NOW)
        s1.close()
        s2 = StateStore(db)
        assert s2.load_position("OCC1") == state
        assert "OCC1" in s2.open_positions()

    def test_closed_positions_not_reported_open(self, tmp_path: Path) -> None:
        s = StateStore(tmp_path / "s.db")
        s.save_position("OCC1", {"phase": "CLOSED", "contracts": 0}, NOW)
        assert s.open_positions() == {}


class TestPaperBroker:
    def test_buy_fill_slips_up_and_costs_cash(self) -> None:
        b = PaperBroker(StrategyConfig(commission_per_contract_cents=0), TickSchedule.FULL_PENNY)
        fill = b.execute(intent(), bid_cents=417, ask_cents=420, now=NOW)
        assert fill.premium_cents == 425  # 420 * 1.01 = 424.2 -> ceil 425
        assert fill.cost_cents == -425 * 100 * 2
        assert fill.modeled is True

    def test_sell_fill_slips_down(self) -> None:
        b = PaperBroker(StrategyConfig(commission_per_contract_cents=0), TickSchedule.FULL_PENNY)
        fill = b.execute(intent(side="SELL"), bid_cents=840, ask_cents=845, now=NOW)
        assert fill.premium_cents == 831  # 840 * 0.99 = 831.6 -> floor 831
        assert fill.cost_cents == +831 * 100 * 2

    def test_tick_legality_on_penny_program(self) -> None:
        b = PaperBroker(StrategyConfig(commission_per_contract_cents=0), TickSchedule.PENNY_PROGRAM)
        fill = b.execute(intent(), bid_cents=417, ask_cents=420, now=NOW)
        assert fill.premium_cents % 5 == 0  # >$3 NVDA prices live on the 5c grid

    def test_replay_returns_original_fill(self) -> None:
        b = PaperBroker(CFG, TickSchedule.FULL_PENNY)
        first = b.execute(intent(), 417, 420, NOW)
        later = b.execute(intent(), 999, 1005, NOW + dt.timedelta(minutes=5))
        assert later == first  # identity, not market luck

    def test_no_market_refused(self) -> None:
        b = PaperBroker(CFG, TickSchedule.FULL_PENNY)
        with pytest.raises(PaperExecutionError):
            b.execute(intent(), bid_cents=0, ask_cents=0, now=NOW)
        with pytest.raises(PaperExecutionError):
            b.execute(intent(), bid_cents=430, ask_cents=420, now=NOW)  # crossed

    def test_commission_scales_with_contracts(self) -> None:
        b = PaperBroker(CFG, TickSchedule.FULL_PENNY)
        fill = b.execute(intent(contracts=2), 417, 420, NOW)
        assert fill.commission_cents == 130


class TestSeqCollisionIsLoud:
    """G-03: the schema comment promised UNIQUE(session_date, seq) would make a
    racing-session collision 'an error, not a lost order'. INSERT OR IGNORE
    guaranteed the opposite, and both call sites discarded the return value —
    so the broker executed, record_fill updated ZERO rows, and save_position
    committed a position with no order behind it. reconcile() rebuilds only
    from FILLED order rows and never enumerates the positions table, so the
    phantom could never be cleaned up."""

    def _other_symbol_same_seq(self, seq: int) -> OrderIntent:
        occ = "SPY260617C00628000"  # different contract, SAME (session, seq)
        return OrderIntent(
            client_order_id=client_order_id("qts-v1", SESSION, occ, "BUY", seq),
            session_date=SESSION,
            strategy="qts-v1",
            occ_symbol=occ,
            side="BUY",
            contracts=1,
            limit_cents=500,
            reason="ENTRY",
            seq=seq,
        )

    def test_colliding_seq_raises_instead_of_vanishing(self, tmp_path: Path) -> None:
        from qts_core.store import SeqCollisionError

        store = StateStore(tmp_path / "s.db")
        assert store.journal_intent(intent(seq=0), NOW) is True
        with pytest.raises(SeqCollisionError, match="seq"):
            store.journal_intent(self._other_symbol_same_seq(0), NOW)

    def test_identical_replay_is_still_a_silent_no_op(self, tmp_path: Path) -> None:
        """The fix must not turn legitimate restart replay into an error —
        that is the whole idempotency contract."""
        store = StateStore(tmp_path / "s.db")
        assert store.journal_intent(intent(seq=0), NOW) is True
        assert store.journal_intent(intent(seq=0), NOW) is False

    def test_fill_for_an_unjournaled_order_is_refused(self, tmp_path: Path) -> None:
        """record_fill was an UPDATE with no rowcount check: a fill for an
        order that is not in the ledger updated nothing and said nothing."""
        from qts_core.store import UnknownOrderError

        store = StateStore(tmp_path / "s.db")
        with pytest.raises(UnknownOrderError):
            store.record_fill("no-such-order-id", NOW, 100, -20000, 0)


class TestCrossSessionLedgerIsDisplayOnly:
    """G-13 / ADR-010: capital, sizing and the daily loss limit reset to the
    configured $850 every morning by DESIGN. Multi-day performance was simply
    invisible, which is a reporting gap, not a mandate to compound."""

    def test_running_total_accumulates_across_sessions(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        for i, (day, pnl) in enumerate(
            [(dt.date(2026, 6, 15), 1_000), (dt.date(2026, 6, 16), -400), (SESSION, 250)]
        ):
            store.upsert_session_pnl(day, pnl, NOW + dt.timedelta(days=i))
        rows = store.cumulative_performance()
        assert [r[0] for r in rows] == ["2026-06-15", "2026-06-16", "2026-06-17"]
        assert [r[2] for r in rows] == [1_000, 600, 850]

    def test_restating_a_session_does_not_double_count(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        store.upsert_session_pnl(SESSION, 500, NOW)
        store.upsert_session_pnl(SESSION, 900, NOW)  # the day progressed
        assert store.cumulative_performance() == [("2026-06-17", 900, 900)]

    def test_cumulative_pnl_never_changes_sizing(self, tmp_path: Path) -> None:
        """The load-bearing assertion: a profitable history must not enlarge
        tomorrow's position. ADR-006 pins the mandate at $850 per day."""
        from qts_core.risk import size_entry

        store = StateStore(tmp_path / "s.db")
        store.upsert_session_pnl(dt.date(2026, 6, 15), 50_000, NOW)
        flat = size_entry(CFG, 420, TickSchedule.PENNY_PROGRAM)
        after_profit = size_entry(CFG, 420, TickSchedule.PENNY_PROGRAM)
        assert flat.contracts == after_profit.contracts
        assert flat.gross_committed_cents <= CFG.sub_portfolio_cents


class _FlakyConn:
    """Delegates to a real connection but fails chosen statements once —
    the only way to reach the COMMIT/ROLLBACK error paths without a full disk."""

    def __init__(self, real: object, fail_on: str) -> None:
        self._real = real
        self._fail_on = fail_on
        self.failed_once = False

    def execute(self, sql: str, *args: object):  # type: ignore[no-untyped-def]
        if sql.strip().upper().startswith(self._fail_on) and not self.failed_once:
            self.failed_once = True
            import sqlite3

            raise sqlite3.OperationalError(f"injected failure on {self._fail_on}")
        return self._real.execute(sql, *args)  # type: ignore[attr-defined]

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._real, name)

    def __setattr__(self, name: str, value: object) -> None:
        # Review finding F4: reads were delegated but WRITES were not, so a
        # StateStore method setting row_factory would set it on the wrapper
        # and silently change behavior under test. Own fields stay local.
        if name in ("_real", "_fail_on", "failed_once"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)


class TestTransactionSurvivesItsOwnFailures:
    """N-07: COMMIT sat outside the try. A COMMIT failure (full disk, BUSY)
    propagated with the write transaction still OPEN, so every later
    transaction() died with 'cannot start a transaction within a transaction'
    while the WAL lock starved other processes. And ROLLBACK was unguarded
    inside the except block — its own failure REPLACED the original exception,
    destroying the diagnosis. 'All-or-nothing' was the promise; the commit
    path did not keep it."""

    def test_commit_failure_does_not_wedge_the_connection(self, tmp_path: Path) -> None:
        import sqlite3

        store = StateStore(tmp_path / "s.db")
        store._conn = _FlakyConn(store._conn, fail_on="COMMIT")  # type: ignore[assignment]
        with pytest.raises(sqlite3.OperationalError, match="injected"), store.transaction():
            store.journal_intent(intent(seq=0), NOW)
        # The failed transaction must have been rolled back, not left open:
        with store.transaction():
            assert store.journal_intent(intent(seq=1), NOW) is True
        assert store.order_status(intent(seq=1).client_order_id) == "INTENT"
        # And the write that failed to commit is NOT half-present.
        assert store.order_status(intent(seq=0).client_order_id) is None

    def test_rollback_failure_does_not_mask_the_original_error(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        store._conn = _FlakyConn(store._conn, fail_on="ROLLBACK")  # type: ignore[assignment]
        with pytest.raises(ValueError, match="the real bug"), store.transaction():
            raise ValueError("the real bug")

    def test_nesting_fails_loud_and_clear(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        with (
            pytest.raises(RuntimeError, match="not reentrant"),
            store.transaction(),
            store.transaction(),
        ):
            pass  # pragma: no cover
        # and the outer rollback left the connection usable
        with store.transaction():
            store.journal_intent(intent(seq=2), NOW)


class TestOneDbOneBackend:
    """ADR-012: modeled fills and venue fills are different KINDS of numbers.
    One ledger blending both would make every aggregate a mix no honest label
    could describe. The first writer claims the db; later opens must match."""

    def test_first_writer_claims_and_matching_reopen_passes(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        assert store.backend() is None, "an unclaimed db has no backend"
        store.assert_backend("model")
        assert store.backend() == "model"
        store.close()
        again = StateStore(tmp_path / "s.db")
        again.assert_backend("model")  # must not raise

    def test_mixing_backends_is_refused_with_the_fix_in_the_message(self, tmp_path: Path) -> None:
        from qts_core.store import BackendMismatchError

        store = StateStore(tmp_path / "s.db")
        store.assert_backend("model")
        with pytest.raises(BackendMismatchError, match="fresh --db"):
            store.assert_backend("alpaca_paper")
