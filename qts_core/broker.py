"""Broker abstraction. PaperBroker is the ONLY implementation.

There is deliberately NO live broker class in this codebase (plan red line):
``require_paper_mode`` refuses live even with an owner acknowledgement. The
Broker protocol exists so a future owner-directed integration has a seam —
not so this system can ever place a real order.

Paper fill model (MODELED, and labeled as such in every report):
  BUY  fills at ask * (1 + slippage_bp/10000), tick-ceiled  (pay up)
  SELL fills at bid * (1 - slippage_bp/10000), tick-floored (give up)
Commission per contract per side from config. The slippage default mirrors
the report's own 1% buffer (ADR-005 sizing quantity).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

from qts_core.config import StrategyConfig
from qts_core.money import BP, CONTRACT_MULTIPLIER, TickSchedule, round_to_tick
from qts_core.store import OrderIntent


@dataclass(frozen=True, slots=True)
class Fill:
    client_order_id: str
    premium_cents: int  # per-share fill price after modeled slippage
    cost_cents: int  # signed cash flow: negative=cash out (BUY), positive=cash in (SELL)
    commission_cents: int
    filled_at: dt.datetime
    modeled: bool  # ALWAYS True for PaperBroker — surfaces in every report


class Broker(Protocol):
    def execute(
        self, intent: OrderIntent, bid_cents: int, ask_cents: int, now: dt.datetime
    ) -> Fill: ...

    def execute_unmarked_exit(self, intent: OrderIntent, now: dt.datetime) -> Fill: ...


class PaperExecutionError(RuntimeError):
    pass


class PaperBroker:
    def __init__(self, cfg: StrategyConfig, tick: TickSchedule) -> None:
        self._cfg = cfg
        self._tick = tick
        self._fills: dict[str, Fill] = {}  # idempotency at the broker seam too

    def execute(
        self, intent: OrderIntent, bid_cents: int, ask_cents: int, now: dt.datetime
    ) -> Fill:
        # Replay of an already-executed intent returns the ORIGINAL fill —
        # restart-safe by identity, not by luck.
        prior = self._fills.get(intent.client_order_id)
        if prior is not None:
            return prior
        if bid_cents <= 0 or ask_cents <= 0 or ask_cents < bid_cents:
            raise PaperExecutionError(
                f"no market for {intent.occ_symbol}: bid={bid_cents} ask={ask_cents}"
            )
        slip = self._cfg.sizing_slippage_buffer_bp
        if intent.side == "BUY":
            raw = ask_cents * (BP + slip)
            premium = round_to_tick(-(-raw // BP), self._tick, "ceil")
            cash = -premium * CONTRACT_MULTIPLIER * intent.contracts
        else:
            raw = bid_cents * (BP - slip)
            premium = round_to_tick(raw // BP, self._tick, "floor")
            cash = premium * CONTRACT_MULTIPLIER * intent.contracts
        commission = self._cfg.commission_per_contract_cents * intent.contracts
        fill = Fill(
            client_order_id=intent.client_order_id,
            premium_cents=int(premium),
            cost_cents=int(cash),
            commission_cents=commission,
            filled_at=now,
            modeled=True,
        )
        self._fills[intent.client_order_id] = fill
        return fill

    def execute_unmarked_exit(self, intent: OrderIntent, now: dt.datetime) -> Fill:
        """Liquidate a position we cannot price — WORST CASE, by construction.

        ``execute`` refuses a bid<=0 book, which is correct for a discretionary
        order and catastrophic for the force-flat rail: the one moment the rail
        must fire is the moment the contract stops being quotable (G-01). This
        path exists only for that rail. Proceeds are ZERO — we do not own data
        to price an untradeable contract, and zero can only understate the
        result, never flatter it. Commission is still charged, because assuming
        it away would be the optimistic direction.
        """
        prior = self._fills.get(intent.client_order_id)
        if prior is not None:
            return prior
        fill = Fill(
            client_order_id=intent.client_order_id,
            premium_cents=0,
            cost_cents=0,
            commission_cents=self._cfg.commission_per_contract_cents * intent.contracts,
            filled_at=now,
            modeled=True,
        )
        self._fills[intent.client_order_id] = fill
        return fill
