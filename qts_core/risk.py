"""Risk engine: sizing, the exit state machine, and the safety rails the spec
does not have.

ADR-004 (single lawful cost basis): risk LEVELS are computed off the QUOTED
entry premium exactly as the report states them ($4.20 -> 3.15 / 8.40 / 4.62),
but P&L and every realized percentage are accounted off the ACTUAL fill cost
basis. Both numbers are carried explicitly; nothing is conflated.

Exit ladder (report, IRONCLAD section):
  OPEN   --premium <= stop(-25% of quote)--------------> exit all (STOP)
  OPEN   --premium >= target(+100% of quote)-----------> sell 1 (TRANCHE1), -> RUNNER
  RUNNER --premium <= max(floor(+10%), peak*(1-12%))---> exit rest (TRAIL)
  any    --now >= force_flat---------------------------> exit all (FORCE_FLAT)  [SAFETY]

Finding runner-stop-unreachable is embodied here: the trail from the 8.40 peak
(739) dominates the 462 floor from the first instant of RUNNER phase; the
floor only matters if the entry quote structure changes. Both are kept because
SPEC states both; max() decides.

Intrabar honesty: evaluate() takes a premium MARK, not an OHLC bar. The
backtester feeds marks in a documented conservative order (stop checked before
target within a bar); the paper loop feeds real quotes. This module never
guesses a path.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from enum import Enum

from qts_core.config import StrategyConfig
from qts_core.money import (
    CONTRACT_MULTIPLIER,
    Cents,
    TickSchedule,
    pct_level_cents,
    round_to_tick,
    size_position,
)


class Phase(Enum):
    OPEN = "OPEN"
    RUNNER = "RUNNER"
    CLOSED = "CLOSED"


class ExitReason(Enum):
    STOP = "STOP"
    TRANCHE1 = "TRANCHE1"
    TRAIL = "TRAIL"
    FORCE_FLAT = "FORCE_FLAT"
    # Force-flat with NO usable mark: the contract left the chain or has no
    # market. Distinct from FORCE_FLAT because it is booked at worst-case
    # zero proceeds, and must never be read as a real market fill (G-01).
    FORCE_FLAT_UNMARKED = "FORCE_FLAT_UNMARKED"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"


@dataclass(frozen=True, slots=True)
class SizedOrder:
    contracts: int
    buffered_cost_per_contract_cents: Cents
    gross_committed_cents: Cents
    residual_cents: Cents


@dataclass(frozen=True, slots=True)
class ExitOrder:
    reason: ExitReason
    contracts: int
    trigger_premium_cents: int  # tick-legal premium the order is placed at


@dataclass(frozen=True, slots=True)
class PositionState:
    """Immutable position snapshot; evaluate() returns (new_state, orders)."""

    symbol: str
    occ_symbol: str
    phase: Phase
    contracts: int
    entry_quote_cents: int  # QUOTED ask at decision time (levels derive from this)
    cost_basis_per_contract_cents: int  # ACTUAL cash paid incl. slippage (P&L derives)
    peak_premium_cents: int
    realized_pnl_cents: int = 0

    # -- levels (SPEC, off the quote — ADR-004) ---------------------------
    def stop_level(self, cfg: StrategyConfig) -> Cents:
        return pct_level_cents(self.entry_quote_cents, cfg.stop_loss_bp)

    def tranche1_level(self, cfg: StrategyConfig) -> Cents:
        return pct_level_cents(self.entry_quote_cents, cfg.tranche1_gain_bp)

    def runner_floor(self, cfg: StrategyConfig) -> Cents:
        return pct_level_cents(self.entry_quote_cents, cfg.runner_floor_bp)

    def trail_level(self, cfg: StrategyConfig) -> Cents:
        return pct_level_cents(self.peak_premium_cents, -cfg.trailing_bp)


def size_entry(
    cfg: StrategyConfig, ask_cents: int, schedule: TickSchedule, capital_cents: int | None = None
) -> SizedOrder:
    """Size an entry against EXECUTABLE cost on the symbol's tick grid.

    ``capital_cents`` lets the caller shrink the mandate after realized losses
    (finding QTS-4: sizing off a constant sub-portfolio commits cash the day
    no longer has).
    """
    n, unit, gross, residual = size_position(
        cfg.sub_portfolio_cents if capital_cents is None else capital_cents,
        ask_cents,
        cfg.sizing_slippage_buffer_bp,
        cfg.commission_per_contract_cents,
        schedule,
    )
    return SizedOrder(n, unit, gross, residual)


def open_position(
    *,
    symbol: str,
    occ_symbol: str,
    contracts: int,
    entry_quote_cents: int,
    fill_cost_per_contract_cents: int,
) -> PositionState:
    if contracts <= 0:
        raise ValueError("cannot open a position with zero contracts")
    return PositionState(
        symbol=symbol,
        occ_symbol=occ_symbol,
        phase=Phase.OPEN,
        contracts=contracts,
        entry_quote_cents=entry_quote_cents,
        cost_basis_per_contract_cents=fill_cost_per_contract_cents,
        peak_premium_cents=entry_quote_cents,
    )


def evaluate(
    state: PositionState,
    premium_mark_cents: int,
    now: dt.datetime,
    force_flat_at: dt.datetime,
    cfg: StrategyConfig,
    tick: TickSchedule,
) -> tuple[PositionState, list[ExitOrder]]:
    """One mark -> (next state, exit orders). Pure; no I/O, no clock reads.

    Precedence: FORCE_FLAT > STOP > TRANCHE1 (OPEN) / TRAIL (RUNNER).
    Peak updates BEFORE trail comparison (a new high can't trigger its own trail).
    """
    if state.phase is Phase.CLOSED or state.contracts == 0:
        return state, []

    def leg(reason: ExitReason, contracts: int, level_cents: int) -> ExitOrder:
        return ExitOrder(reason, contracts, round_to_tick(level_cents, tick, "floor"))

    if now >= force_flat_at:
        order = leg(ExitReason.FORCE_FLAT, state.contracts, premium_mark_cents)
        return replace(state, phase=Phase.CLOSED, contracts=0), [order]

    new_peak = max(state.peak_premium_cents, premium_mark_cents)
    state = replace(state, peak_premium_cents=new_peak)

    if state.phase is Phase.OPEN:
        if premium_mark_cents <= state.stop_level(cfg):
            order = leg(ExitReason.STOP, state.contracts, state.stop_level(cfg))
            return replace(state, phase=Phase.CLOSED, contracts=0), [order]
        if premium_mark_cents >= state.tranche1_level(cfg):
            sell = min(cfg.tranche1_contracts, state.contracts)
            remaining = state.contracts - sell
            orders = [leg(ExitReason.TRANCHE1, sell, state.tranche1_level(cfg))]
            if remaining == 0:
                return replace(state, phase=Phase.CLOSED, contracts=0), orders
            return replace(state, phase=Phase.RUNNER, contracts=remaining), orders
        return state, []

    # RUNNER
    floor = state.runner_floor(cfg)
    trail = state.trail_level(cfg)
    trigger = max(floor, trail)
    if premium_mark_cents <= trigger:
        order = leg(ExitReason.TRAIL, state.contracts, trigger)
        return replace(state, phase=Phase.CLOSED, contracts=0), [order]
    return state, []


def realized_pnl_cents(
    state: PositionState,
    exit_premium_cents: int,
    contracts: int,
    commission_per_contract_cents: int,
) -> int:
    """P&L for an exit leg off the ACTUAL basis (ADR-004), commissions included."""
    proceeds = exit_premium_cents * CONTRACT_MULTIPLIER * contracts
    cost = state.cost_basis_per_contract_cents * contracts
    return proceeds - cost - commission_per_contract_cents * contracts


def daily_loss_breached(realized_today_cents: int, cfg: StrategyConfig) -> bool:
    """SAFETY (ADR-008): absent from the spec. Blocks re-entry after the day's
    realized losses cross the limit — a single -25% stop would otherwise allow
    an unbounded retry loop all day."""
    return -realized_today_cents >= cfg.daily_loss_limit_cents
