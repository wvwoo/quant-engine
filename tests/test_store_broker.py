"""Store + broker tests, centered on the crash-recovery contract:
kill -9 at any instant + restart => no duplicate order, no lost position."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from qts_core.clock import NY
from qts_core.config import StrategyConfig
from qts_core.broker import PaperBroker, PaperExecutionError
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
