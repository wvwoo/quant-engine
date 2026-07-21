"""Risk engine tests — the report's IRONCLAD ladder, exactly, plus the safety
rails it lacks. Property-style sweeps guard the sizing/stop equations."""

from __future__ import annotations

import datetime as dt

import pytest

from qts_core.clock import NY
from qts_core.config import StrategyConfig
from qts_core.money import CONTRACT_MULTIPLIER, TickSchedule
from qts_core.risk import (
    ExitReason,
    Phase,
    PositionState,
    daily_loss_breached,
    evaluate,
    open_position,
    realized_pnl_cents,
    size_entry,
)

CFG = StrategyConfig()
TICK = TickSchedule.PENNY_PROGRAM  # NVDA
FLAT_AT = dt.datetime(2026, 6, 17, 15, 30, tzinfo=NY)


def t(hh: int, mm: int) -> dt.datetime:
    return dt.datetime(2026, 6, 17, hh, mm, tzinfo=NY)


def nvda_position(contracts: int = 2) -> PositionState:
    # Entry quote $4.20; actual fill basis $424.20/contract (report's slipped cost).
    return open_position(
        symbol="NVDA",
        occ_symbol="NVDA260617C00205000",
        contracts=contracts,
        entry_quote_cents=420,
        fill_cost_per_contract_cents=42420,
    )


class TestSizing:
    def test_report_scenario_at_execution_prices(self) -> None:
        # Commission-free (the report's own assumption): still 2 contracts,
        # but priced at the tick-legal 425/share -> 42500/contract, so the
        # residual is honestly $0.00 rather than the report's $1.60 (which
        # came from rounding at contract granularity). See QTS-R2.
        cfg = StrategyConfig(commission_per_contract_cents=0)
        s = size_entry(cfg, 420, TickSchedule.PENNY_PROGRAM)
        assert (s.contracts, s.buffered_cost_per_contract_cents) == (2, 42500)
        assert (s.gross_committed_cents, s.residual_cents) == (85000, 0)

    def test_default_commission_drops_to_one_contract(self) -> None:
        # THE BUG: the old estimate said 2 contracts / $849.70 while the broker
        # would charge $851.30 against an $850 mandate. Honest sizing takes 1.
        s = size_entry(CFG, 420, TickSchedule.PENNY_PROGRAM)
        assert s.contracts == 1
        assert s.gross_committed_cents == 42565

    def test_capital_shrinks_after_realized_loss(self) -> None:
        # QTS-4: sizing must respect what the day still has.
        full = size_entry(CFG, 200, TickSchedule.FULL_PENNY)
        after_loss = size_entry(CFG, 200, TickSchedule.FULL_PENNY, capital_cents=40000)
        assert after_loss.contracts < full.contracts

    def test_sizing_never_overspends_property(self) -> None:
        # Sweep premiums AND tick grids: gross never exceeds capital, and one
        # more contract always breaks the budget (maximality).
        for ask in range(50, 4000, 7):
            for tick in TickSchedule:
                s = size_entry(CFG, ask, tick)
                assert s.gross_committed_cents <= CFG.sub_portfolio_cents
                if s.contracts:
                    unit = s.buffered_cost_per_contract_cents + CFG.commission_per_contract_cents
                    assert (s.contracts + 1) * unit > CFG.sub_portfolio_cents


class TestExitLadder:
    def test_stop_exits_all(self) -> None:
        st, orders = evaluate(nvda_position(), 315, t(10, 30), FLAT_AT, CFG, TICK)
        assert [o.reason for o in orders] == [ExitReason.STOP]
        assert orders[0].contracts == 2
        assert st.phase is Phase.CLOSED

    def test_stop_is_boundary_inclusive(self) -> None:
        _, at = evaluate(nvda_position(), 315, t(10, 30), FLAT_AT, CFG, TICK)
        _, above = evaluate(nvda_position(), 316, t(10, 30), FLAT_AT, CFG, TICK)
        assert at and not above

    def test_tranche1_sells_one_and_transitions(self) -> None:
        st, orders = evaluate(nvda_position(), 840, t(10, 45), FLAT_AT, CFG, TICK)
        assert [o.reason for o in orders] == [ExitReason.TRANCHE1]
        assert orders[0].contracts == 1
        assert st.phase is Phase.RUNNER
        assert st.contracts == 1
        assert st.peak_premium_cents == 840

    def test_tranche1_on_single_contract_closes(self) -> None:
        st, orders = evaluate(nvda_position(1), 840, t(10, 45), FLAT_AT, CFG, TICK)
        assert st.phase is Phase.CLOSED
        assert orders[0].contracts == 1

    def test_trail_dominates_floor_immediately(self) -> None:
        # Finding runner-stop-unreachable, now executable: after tranche-1 at
        # 840, trail = 840*0.88 = 739 (floor-rounded) > floor 462. A mark at
        # 700 must exit on TRAIL at trigger 739 — the 462 floor is dead.
        st, _ = evaluate(nvda_position(), 840, t(10, 45), FLAT_AT, CFG, TICK)
        st2, orders = evaluate(st, 700, t(10, 50), FLAT_AT, CFG, TICK)
        assert [o.reason for o in orders] == [ExitReason.TRAIL]
        assert orders[0].trigger_premium_cents == 735  # 739 tick-floored to 5c grid
        assert st2.phase is Phase.CLOSED

    def test_peak_ratchets_and_trail_follows(self) -> None:
        st, _ = evaluate(nvda_position(), 840, t(10, 45), FLAT_AT, CFG, TICK)
        st, orders = evaluate(st, 1000, t(10, 50), FLAT_AT, CFG, TICK)  # new peak
        assert not orders
        assert st.peak_premium_cents == 1000
        # trail now 880; 875 must trigger
        _st2, orders = evaluate(st, 875, t(10, 55), FLAT_AT, CFG, TICK)
        assert [o.reason for o in orders] == [ExitReason.TRAIL]

    def test_new_peak_does_not_self_trigger(self) -> None:
        st, _ = evaluate(nvda_position(), 840, t(10, 45), FLAT_AT, CFG, TICK)
        st, orders = evaluate(st, 2000, t(10, 50), FLAT_AT, CFG, TICK)
        assert not orders
        assert st.phase is Phase.RUNNER

    def test_quiet_mark_no_orders(self) -> None:
        st, orders = evaluate(nvda_position(), 500, t(10, 30), FLAT_AT, CFG, TICK)
        assert not orders
        assert st.phase is Phase.OPEN
        assert st.peak_premium_cents == 500


class TestForceFlat:
    def test_force_flat_overrides_everything(self) -> None:
        # SAFETY B3: at/after force-flat every phase exits, even at a profit.
        st, orders = evaluate(nvda_position(), 840, FLAT_AT, FLAT_AT, CFG, TICK)
        assert [o.reason for o in orders] == [ExitReason.FORCE_FLAT]
        assert orders[0].contracts == 2
        assert st.phase is Phase.CLOSED

    def test_runner_also_force_flats(self) -> None:
        st, _ = evaluate(nvda_position(), 840, t(10, 45), FLAT_AT, CFG, TICK)
        st2, orders = evaluate(st, 900, t(15, 31), FLAT_AT, CFG, TICK)
        assert [o.reason for o in orders] == [ExitReason.FORCE_FLAT]
        assert st2.phase is Phase.CLOSED

    def test_closed_position_stays_closed(self) -> None:
        st, _ = evaluate(nvda_position(), 315, t(10, 30), FLAT_AT, CFG, TICK)
        st2, orders = evaluate(st, 100, t(10, 35), FLAT_AT, CFG, TICK)
        assert not orders
        assert st2 == st


class TestPnlAccounting:
    def test_stop_loss_realized_off_actual_basis(self) -> None:
        # ADR-004: -25% of quote realizes 26/101 = -25.74% of the slipped basis.
        pnl = realized_pnl_cents(nvda_position(), 315, 2, 0)
        assert pnl == 2 * (31500 - 42420) == -21840
        assert pnl * 101 == -26 * 2 * 42420  # exact fraction, no floats

    def test_tranche1_pnl(self) -> None:
        pnl = realized_pnl_cents(nvda_position(), 840, 1, 0)
        assert pnl == 84000 - 42420 == 41580

    def test_commission_reduces_pnl(self) -> None:
        with_fee = realized_pnl_cents(nvda_position(), 840, 1, 65)
        assert with_fee == 41580 - 65

    def test_contract_multiplier_present(self) -> None:
        # The report's own formula dropped the 100x; the engine must not.
        pnl = realized_pnl_cents(nvda_position(1), 421, 1, 0)
        assert pnl == 421 * CONTRACT_MULTIPLIER - 42420


class TestDailyLossLimit:
    def test_not_breached_under_limit(self) -> None:
        assert not daily_loss_breached(-21_000, CFG)

    def test_breached_at_limit(self) -> None:
        assert daily_loss_breached(-21_250, CFG)

    def test_profits_never_breach(self) -> None:
        assert not daily_loss_breached(+50_000, CFG)


class TestLevelSweep:
    """Property sweep: for any entry quote, level ordering must hold and the
    stop must never round UP through the trigger (conservative floor)."""

    @pytest.mark.parametrize("quote", range(100, 3000, 37))
    def test_level_ordering(self, quote: int) -> None:
        pos = open_position(
            symbol="X",
            occ_symbol="X",
            contracts=2,
            entry_quote_cents=quote,
            fill_cost_per_contract_cents=quote * 101,
        )
        stop = pos.stop_level(CFG)
        floor = pos.runner_floor(CFG)
        target = pos.tranche1_level(CFG)
        assert stop < quote < floor < target
        assert stop == quote * 75 // 100
        assert target == quote * 2
