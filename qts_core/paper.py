"""The paper-trading session loop: decide -> journal -> fill -> persist.

Resumable by construction: every step writes through StateStore before acting,
client order ids are deterministic, and PaperBroker replays by identity — so
killing the process at any instant and restarting continues the SAME session
without a duplicate order or a lost position (tested by simulated kill -9).

This is Agent 0 (the orchestrator the PRD forgot): it owns the kill switch
(daily loss limit + force-flat) and sequences every other module. It holds NO
trading logic of its own — signals and risk stay pure and separately tested.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from qts_core.broker import Broker, Fill
from qts_core.config import StrategyConfig, require_paper_mode
from qts_core.models import MarketView
from qts_core.money import CONTRACT_MULTIPLIER
from qts_core.risk import (
    Phase,
    PositionState,
    evaluate,
    open_position,
    size_entry,
)
from qts_core.signals.checklist import EntryDecision, evaluate_entry
from qts_core.store import OrderIntent, StateStore, client_order_id

STRATEGY_ID = "qts-v1"


@dataclass(frozen=True, slots=True)
class StepResult:
    decision: EntryDecision | None
    fills: tuple[Fill, ...]
    position: PositionState | None
    halted: str | None  # non-None => session halted (reason)


def _pos_to_json(p: PositionState) -> dict[str, object]:
    return {
        "symbol": p.symbol,
        "occ_symbol": p.occ_symbol,
        "phase": p.phase.value,
        "contracts": p.contracts,
        "entry_quote_cents": p.entry_quote_cents,
        "cost_basis_per_contract_cents": p.cost_basis_per_contract_cents,
        "peak_premium_cents": p.peak_premium_cents,
        "realized_pnl_cents": p.realized_pnl_cents,
    }


def _pos_from_json(d: dict[str, object]) -> PositionState:
    return PositionState(
        symbol=str(d["symbol"]),
        occ_symbol=str(d["occ_symbol"]),
        phase=Phase(str(d["phase"])),
        contracts=int(d["contracts"]),  # type: ignore[call-overload]
        entry_quote_cents=int(d["entry_quote_cents"]),  # type: ignore[call-overload]
        cost_basis_per_contract_cents=int(d["cost_basis_per_contract_cents"]),  # type: ignore[call-overload]
        peak_premium_cents=int(d["peak_premium_cents"]),  # type: ignore[call-overload]
        realized_pnl_cents=int(d["realized_pnl_cents"]),  # type: ignore[call-overload]
    )


def _decision_blob(d: EntryDecision) -> str:
    return json.dumps(
        {
            "now_et": d.now_et,
            "symbol": d.symbol,
            "approved": d.approved,
            "checks": [dataclasses.asdict(c) for c in d.checks],
            "selection": (
                {
                    "occ": d.selection.quote.occ_symbol,
                    "delta": round(d.selection.delta, 6),
                    "iv": round(d.selection.iv, 6),
                    "iv_source": d.selection.iv_source,
                    "mid_cents": d.selection.mid_cents,
                    "spread_cents": d.selection.spread_cents,
                }
                if d.selection
                else None
            ),
        },
        sort_keys=True,
    )


class PaperSession:
    """One symbol, one session, resumable. All wall-clock via the view's now."""

    def __init__(
        self,
        store: StateStore,
        broker: Broker,
        cfg: StrategyConfig,
        *,
        session_date: dt.date,
        session_open_et: dt.datetime,
        force_flat_at: dt.datetime,
    ) -> None:
        require_paper_mode(cfg)  # boot gate — every constructor call, no exceptions
        self.store = store
        self.broker = broker
        self.cfg = cfg
        self.session_date = session_date
        self.session_open_et = session_open_et
        self.force_flat_at = force_flat_at

    # ------------------------------------------------------------ recovery
    def recover_position(self, occ_symbol: str) -> PositionState | None:
        blob = self.store.load_position(occ_symbol)
        return None if blob is None else _pos_from_json(blob)

    def any_open_position(self) -> PositionState | None:
        for blob in self.store.open_positions().values():
            return _pos_from_json(blob)
        return None

    # ------------------------------------------------------------ one step
    def step(self, view: MarketView) -> StepResult:
        """Advance the session by one market snapshot."""
        now = view.now
        realized_today = self.store.realized_pnl_today(self.session_date)

        pos = self.any_open_position()
        if pos is not None:
            return self._manage_exits(pos, view)

        # -- flat: entry path, guarded by the safety rails ----------------
        if -realized_today >= self.cfg.daily_loss_limit_cents:
            return StepResult(None, (), None, halted="DAILY_LOSS_LIMIT")
        if now >= self.force_flat_at:
            return StepResult(None, (), None, halted="AFTER_FORCE_FLAT")

        decision = evaluate_entry(view, self.cfg, self.session_open_et)
        self.store.log_decision(
            now, self.session_date, decision.symbol, decision.approved, _decision_blob(decision)
        )
        if not decision.approved or decision.selection is None:
            return StepResult(decision, (), None, None)

        sel = decision.selection
        sized = size_entry(self.cfg, sel.quote.ask_cents)
        if sized.contracts == 0:
            return StepResult(decision, (), None, None)

        seq = self.store.next_seq(self.session_date)
        coid = client_order_id(STRATEGY_ID, self.session_date, sel.quote.occ_symbol, "BUY", seq)
        intent = OrderIntent(
            client_order_id=coid,
            session_date=self.session_date,
            strategy=STRATEGY_ID,
            occ_symbol=sel.quote.occ_symbol,
            side="BUY",
            contracts=sized.contracts,
            limit_cents=sel.quote.ask_cents,
            reason="ENTRY",
            seq=seq,
        )
        # Journal BEFORE side effect; a replayed step is a no-op + reconcile.
        self.store.journal_intent(intent, now)
        fill = self.broker.execute(intent, sel.quote.bid_cents, sel.quote.ask_cents, now)
        # For BUY legs fill_cost_cents stores the signed cash flow.
        self.store.record_fill(
            coid, now, fill.premium_cents, fill.cost_cents, fill.commission_cents
        )

        pos = open_position(
            symbol=sel.quote.underlying,
            occ_symbol=sel.quote.occ_symbol,
            contracts=sized.contracts,
            entry_quote_cents=sel.quote.ask_cents,
            fill_cost_per_contract_cents=fill.premium_cents * CONTRACT_MULTIPLIER
            + self.cfg.commission_per_contract_cents,
        )
        self.store.save_position(pos.occ_symbol, _pos_to_json(pos), now)
        self._snapshot(view, pos)
        return StepResult(decision, (fill,), pos, None)

    def _manage_exits(self, pos: PositionState, view: MarketView) -> StepResult:
        now = view.now
        quote = next((q for q in view.chain if q.occ_symbol == pos.occ_symbol), None)
        if quote is None or quote.mid_cents is None:
            # No usable mark: state unchanged; report the data gap loudly.
            return StepResult(None, (), pos, halted=None)
        tick = self.cfg.tick_schedule_for(pos.symbol)
        new_state, exit_orders = evaluate(
            pos, quote.mid_cents, now, self.force_flat_at, self.cfg, tick
        )
        fills: list[Fill] = []
        for order in exit_orders:
            seq = self.store.next_seq(self.session_date)
            coid = client_order_id(
                STRATEGY_ID, self.session_date, pos.occ_symbol, f"SELL-{order.reason.value}", seq
            )
            intent = OrderIntent(
                client_order_id=coid,
                session_date=self.session_date,
                strategy=STRATEGY_ID,
                occ_symbol=pos.occ_symbol,
                side="SELL",
                contracts=order.contracts,
                limit_cents=order.trigger_premium_cents,
                reason=order.reason.value,
                seq=seq,
            )
            self.store.journal_intent(intent, now)
            fill = self.broker.execute(intent, quote.bid_cents, quote.ask_cents, now)
            realized = (
                fill.premium_cents * CONTRACT_MULTIPLIER * order.contracts
                - pos.cost_basis_per_contract_cents * order.contracts
                - fill.commission_cents
            )
            # For SELL legs fill_cost_cents stores the signed REALIZED P&L.
            self.store.record_fill(coid, now, fill.premium_cents, realized, fill.commission_cents)
            fills.append(fill)
            new_state = dataclasses.replace(
                new_state, realized_pnl_cents=new_state.realized_pnl_cents + realized
            )
        self.store.save_position(pos.occ_symbol, _pos_to_json(new_state), now)
        self._snapshot(view, new_state)
        halt = None
        if -self.store.realized_pnl_today(self.session_date) >= self.cfg.daily_loss_limit_cents:
            halt = "DAILY_LOSS_LIMIT"
        return StepResult(None, tuple(fills), new_state, halt)

    def _snapshot(self, view: MarketView, pos: PositionState | None) -> None:
        realized = self.store.realized_pnl_today(self.session_date)
        open_value = 0
        if pos is not None and pos.contracts > 0:
            mark = next((q.mid_cents for q in view.chain if q.occ_symbol == pos.occ_symbol), None)
            if mark is not None:
                open_value = mark * CONTRACT_MULTIPLIER * pos.contracts
        cash = self.cfg.sub_portfolio_cents + realized
        if pos is not None and pos.contracts > 0:
            cash -= pos.cost_basis_per_contract_cents * pos.contracts
        self.store.snapshot_equity(view.now, self.session_date, cash, open_value, realized)


def load_fixture_session(path: str | Path) -> list[MarketView]:
    """Deserialize a recorded session (list of MarketView snapshots) from JSON.

    Fixtures are deterministic and network-free: the golden gate replays them
    byte-identically. Format: {"snapshots": [{now, session_date, bars: [...],
    prior_sessions: [[...]], chain: [...], underlying_last, meta}]}.
    """
    import qts_core.models as m

    raw = json.loads(Path(path).read_text())

    def bar(b: dict[str, object]) -> m.Bar:
        return m.Bar(
            ts_close=dt.datetime.fromisoformat(str(b["ts_close"])),
            open=float(b["open"]),  # type: ignore[arg-type]
            high=float(b["high"]),  # type: ignore[arg-type]
            low=float(b["low"]),  # type: ignore[arg-type]
            close=float(b["close"]),  # type: ignore[arg-type]
            volume=int(b["volume"]),  # type: ignore[call-overload]
        )

    def quote(q: dict[str, object]) -> m.OptionQuote:
        return m.OptionQuote(
            underlying=str(q["underlying"]),
            expiry=dt.date.fromisoformat(str(q["expiry"])),
            strike_cents=int(q["strike_cents"]),  # type: ignore[call-overload]
            right="C" if str(q["right"]) == "C" else "P",
            bid_cents=int(q["bid_cents"]),  # type: ignore[call-overload]
            ask_cents=int(q["ask_cents"]),  # type: ignore[call-overload]
            volume=int(q["volume"]),  # type: ignore[call-overload]
            open_interest=int(q["open_interest"]),  # type: ignore[call-overload]
            received_at=dt.datetime.fromisoformat(str(q["received_at"])),
            iv_hint=(float(q["iv_hint"]) if q.get("iv_hint") is not None else None),  # type: ignore[arg-type]
        )

    views: list[MarketView] = []
    for snap in raw["snapshots"]:
        views.append(
            m.MarketView(
                now=dt.datetime.fromisoformat(snap["now"]),
                session_date=dt.date.fromisoformat(snap["session_date"]),
                bars=tuple(bar(b) for b in snap["bars"]),
                prior_sessions=tuple(
                    tuple(bar(b) for b in sess) for sess in snap.get("prior_sessions", [])
                ),
                chain=tuple(quote(q) for q in snap.get("chain", [])),
                underlying_last=snap.get("underlying_last"),
                meta=dict(snap.get("meta", {})),
            )
        )
    return views
