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
from typing import Any

from qts_core.broker import Broker, Fill
from qts_core.clock import NY, require_aware
from qts_core.config import StrategyConfig, require_paper_mode
from qts_core.models import MarketView
from qts_core.money import CONTRACT_MULTIPLIER
from qts_core.risk import (
    ExitReason,
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


def _expiry_from_occ(occ: str) -> dt.date | None:
    """Parse the YYMMDD block out of an OCC symbol (root + 6 date + R + 8)."""
    if len(occ) < 15:
        return None
    body = occ[:-9]  # strip right + 8-digit strike
    ymd = body[-6:]
    try:
        return dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError:
        return None


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
        symbol: str | None = None,
    ) -> None:
        require_paper_mode(cfg)  # boot gate — every constructor call, no exceptions
        self.store = store
        self.broker = broker
        self.cfg = cfg
        self.session_date = session_date
        self.session_open_et = session_open_et
        self.force_flat_at = force_flat_at
        self.symbol = symbol
        # Recover at construction: a caller that only inspects state (never
        # steps) must still see the truth the ledger records.
        self.reconcile()

    # ------------------------------------------------------------ recovery
    def recover_position(self, occ_symbol: str) -> PositionState | None:
        blob = self.store.load_position(occ_symbol)
        return None if blob is None else _pos_from_json(blob)

    def any_open_position(self) -> PositionState | None:
        """Open position for THIS session only.

        A 0DTE contract from an earlier session has expired; carrying it
        forward as tradeable would mark a worthless (or assigned) contract as
        live forever (finding QTS-2). Stale rows are quarantined instead.
        """
        for occ, blob in self.store.open_positions().items():
            if not occ.startswith("__"):
                expiry = _expiry_from_occ(occ)
                if expiry is not None and expiry < self.session_date:
                    continue
            return _pos_from_json(blob)
        return None

    def expired_positions(self) -> list[str]:
        """Open rows whose 0DTE expiry has already passed — reported, never
        silently settled: we do not own the data to price an assignment."""
        out = []
        for occ in self.store.open_positions():
            expiry = _expiry_from_occ(occ)
            if expiry is not None and expiry < self.session_date:
                out.append(occ)
        return sorted(out)

    # ------------------------------------------------------------ reconciliation
    def reconcile(self, now: dt.datetime | None = None) -> None:
        """Rebuild positions from the ORDER LEDGER — the single source of truth.

        A crash between ``record_fill`` and ``save_position`` leaves a FILLED
        order with no matching position row. Without this, restart sees itself
        as flat, ``next_seq`` has already advanced past the filled order, and
        the replayed intent gets a NEW client_order_id — a genuine duplicate
        order (found by 3 independent reviewers: QTS-1/QTS-2/QTS-R1).

        Folding the ledger makes ``positions`` a derived cache: whatever the
        crash timing, the rebuilt state matches the fills that actually
        happened.
        """
        rows = self.store.orders_for_session(self.session_date)
        # An INTENT with no fill is a rolled-back attempt: the fill and the
        # position commit together, so if the fill is absent it never happened.
        # Retire it so it cannot be mistaken for an in-flight order.
        orphans = [r["client_order_id"] for r in rows if r["status"] == "INTENT"]
        if orphans:
            self.store.reject_intents(orphans)
            rows = self.store.orders_for_session(self.session_date)
        if now is None:
            # Timestamp is only the positions row's updated_at; the newest
            # ledger entry is the honest stamp for a reconstruction.
            stamps = [str(r["filled_at"] or r["created_at"]) for r in rows]
            now = (
                dt.datetime.fromisoformat(max(stamps))
                if stamps
                else dt.datetime.combine(self.session_date, dt.time(0, 0), tzinfo=NY)
            )
        by_symbol: dict[str, list[Any]] = {}
        for row in rows:
            if row["status"] != "FILLED":
                continue
            by_symbol.setdefault(row["occ_symbol"], []).append(row)

        for occ, fills in by_symbol.items():
            rebuilt = self._fold_fills(occ, sorted(fills, key=lambda r: int(r["seq"])))
            if rebuilt is None:
                continue
            stored = self.store.load_position(occ)
            if stored is not None:
                # Never lower a persisted peak: the trail must not be loosened
                # by a reconstruction that only sees fill prices.
                stored_peak = stored.get("peak_premium_cents", 0)
                peak = stored_peak if isinstance(stored_peak, int) else 0
                rebuilt = dataclasses.replace(
                    rebuilt,
                    peak_premium_cents=max(rebuilt.peak_premium_cents, peak),
                )
                if _pos_to_json(rebuilt) == stored:
                    continue  # already consistent
            self.store.save_position(occ, _pos_to_json(rebuilt), now)

    def _fold_fills(self, occ: str, fills: list[Any]) -> PositionState | None:
        """Deterministic fold of one symbol's FILLED legs into a position."""
        state: PositionState | None = None
        for row in fills:
            contracts = int(row["contracts"])
            premium = int(row["fill_premium_cents"])
            if row["side"] == "BUY":
                state = open_position(
                    symbol=occ[:-15] or occ,  # OCC: root + 6 date + 1 right + 8 strike
                    occ_symbol=occ,
                    contracts=contracts,
                    entry_quote_cents=int(row["limit_cents"]),
                    fill_cost_per_contract_cents=premium * CONTRACT_MULTIPLIER
                    + self.cfg.commission_per_contract_cents,
                )
                continue
            if state is None:
                continue  # a SELL with no recorded BUY: nothing coherent to rebuild
            remaining = max(state.contracts - contracts, 0)
            realized = state.realized_pnl_cents + int(row["fill_cost_cents"] or 0)
            phase = Phase.CLOSED if remaining == 0 else Phase.RUNNER
            state = dataclasses.replace(
                state,
                contracts=remaining,
                phase=phase,
                peak_premium_cents=max(state.peak_premium_cents, premium),
                realized_pnl_cents=realized,
            )
        return state

    # ------------------------------------------------------------ one step
    def step(self, view: MarketView) -> StepResult:
        """Advance the session by one market snapshot."""
        now = view.now
        # Recovery BEFORE any decision: never act on a stale view of ourselves.
        self.reconcile(now)
        realized_today = self.store.realized_pnl_today(self.session_date)

        pos = self.any_open_position()
        if pos is not None:
            if self.symbol is not None and pos.symbol != self.symbol:
                # Capital is a SHARED, concentrated sub-portfolio: while any
                # symbol holds a position, no other symbol may open one. The
                # holder's own view manages its exits.
                return StepResult(None, (), None, halted=f"CAPITAL_COMMITTED:{pos.symbol}")
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
        # QTS-4: size against what the day still has, not a constant mandate.
        capital = min(self.cfg.sub_portfolio_cents, self.cfg.sub_portfolio_cents + realized_today)
        sized = size_entry(
            self.cfg,
            sel.quote.ask_cents,
            self.cfg.tick_schedule_for(sel.quote.underlying),
            capital,
        )
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
        pos = open_position(
            symbol=sel.quote.underlying,
            occ_symbol=sel.quote.occ_symbol,
            contracts=sized.contracts,
            entry_quote_cents=sel.quote.ask_cents,
            fill_cost_per_contract_cents=fill.premium_cents * CONTRACT_MULTIPLIER
            + self.cfg.commission_per_contract_cents,
        )
        # Fill + position in ONE transaction: a crash between them can no
        # longer leave a FILLED order without its position (QTS-2).
        with self.store.transaction():
            # For BUY legs fill_cost_cents stores the signed cash flow.
            self.store.record_fill(
                coid, now, fill.premium_cents, fill.cost_cents, fill.commission_cents
            )
            self.store.save_position(pos.occ_symbol, _pos_to_json(pos), now)
        self._snapshot(view.now, view.chain, pos)
        return StepResult(decision, (fill,), pos, None)

    def _manage_exits(self, pos: PositionState, view: MarketView) -> StepResult:
        now = view.now
        quote = next((q for q in view.chain if q.occ_symbol == pos.occ_symbol), None)
        if quote is None or quote.mid_cents is None:
            # No usable mark. Before force-flat there is nothing to act on, but
            # the gap is REPORTED (halted), not swallowed.
            #
            # After force-flat it is the opposite: the rail MUST still fire.
            # This early return used to cover both cases, so FORCE_FLAT — the
            # only guard against carrying a 0DTE contract past the close
            # (ADR-008 / B3) — was skipped in exactly the situation it exists
            # for: a contract that stopped being quotable near the close (G-01).
            if now < self.force_flat_at:
                return StepResult(None, (), pos, halted="NO_MARK")
            return self._force_flat_unmarked(pos, now, view.chain)
        tick = self.cfg.tick_schedule_for(pos.symbol)
        new_state, exit_orders = evaluate(
            pos, quote.mid_cents, now, self.force_flat_at, self.cfg, tick
        )
        fills: list[Fill] = []
        pending: list[tuple[str, int, int, int]] = []  # (coid, premium, realized, commission)
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
            pending.append((coid, fill.premium_cents, realized, fill.commission_cents))
            fills.append(fill)
            new_state = dataclasses.replace(
                new_state, realized_pnl_cents=new_state.realized_pnl_cents + realized
            )
        # Every exit fill AND the resulting position state commit together: a
        # crash can no longer sell the position twice on restart (QTS-R1).
        with self.store.transaction():
            for coid_, premium_, realized_, commission_ in pending:
                self.store.record_fill(coid_, now, premium_, realized_, commission_)
            self.store.save_position(pos.occ_symbol, _pos_to_json(new_state), now)
        self._snapshot(view.now, view.chain, new_state)
        halt = None
        if -self.store.realized_pnl_today(self.session_date) >= self.cfg.daily_loss_limit_cents:
            halt = "DAILY_LOSS_LIMIT"
        return StepResult(None, tuple(fills), new_state, halt)

    def force_flat_if_due(self, now: dt.datetime) -> StepResult | None:
        """Liquidate a held position using ONLY the clock and the ledger (N-02).

        Exit management used to live exclusively inside ``step(view)``, and
        live.py skips straight past ``step`` whenever the provider raises or
        the B4 availability gate flips false. That made the safety rail
        conditional on the one data feed most likely to be broken at the moment
        it matters — and yfinance throttling is documented as expected, not
        exotic. This path needs no market data at all.

        Returns None when there is nothing to do (flat, another symbol's
        position, or simply not force-flat time yet).
        """
        require_aware(now)
        self.reconcile(now)
        pos = self.any_open_position()
        if pos is None:
            return None
        if self.symbol is not None and pos.symbol != self.symbol:
            return None  # the holder's own pass owns its exits
        if now < self.force_flat_at:
            return None
        return self._force_flat_unmarked(pos, now, ())

    def _force_flat_unmarked(
        self, pos: PositionState, now: dt.datetime, chain: tuple[Any, ...]
    ) -> StepResult:
        """Force-flat a position that cannot be priced (G-01 / N-02).

        Booked at worst-case ZERO proceeds through a DISTINCT reason, so the
        ledger never lets it be mistaken for a real market fill. Carrying the
        contract instead would leave a 0DTE position past the close, which is
        the specific hazard ADR-008 added the rail to prevent.
        """
        reason = ExitReason.FORCE_FLAT_UNMARKED.value
        seq = self.store.next_seq(self.session_date)
        coid = client_order_id(
            STRATEGY_ID, self.session_date, pos.occ_symbol, f"SELL-{reason}", seq
        )
        intent = OrderIntent(
            client_order_id=coid,
            session_date=self.session_date,
            strategy=STRATEGY_ID,
            occ_symbol=pos.occ_symbol,
            side="SELL",
            contracts=pos.contracts,
            limit_cents=0,  # no market to limit against; the fill is worst-case
            reason=reason,
            seq=seq,
        )
        self.store.journal_intent(intent, now)
        fill = self.broker.execute_unmarked_exit(intent, now)
        realized = (
            fill.premium_cents * CONTRACT_MULTIPLIER * pos.contracts
            - pos.cost_basis_per_contract_cents * pos.contracts
            - fill.commission_cents
        )
        new_state = dataclasses.replace(
            pos,
            phase=Phase.CLOSED,
            contracts=0,
            realized_pnl_cents=pos.realized_pnl_cents + realized,
        )
        with self.store.transaction():
            self.store.record_fill(coid, now, fill.premium_cents, realized, fill.commission_cents)
            self.store.save_position(pos.occ_symbol, _pos_to_json(new_state), now)
        self._snapshot(now, chain, new_state)
        return StepResult(None, (fill,), new_state, halted=reason)

    def _snapshot(
        self,
        now: dt.datetime,
        chain: tuple[Any, ...],
        pos: PositionState | None,
    ) -> None:
        """Record equity. Takes the chain, not a whole view, so the paths that
        have no market data (force_flat_if_due) can still snapshot."""
        realized = self.store.realized_pnl_today(self.session_date)
        open_value = 0
        if pos is not None and pos.contracts > 0:
            mark = next((q.mid_cents for q in chain if q.occ_symbol == pos.occ_symbol), None)
            if mark is not None:
                open_value = mark * CONTRACT_MULTIPLIER * pos.contracts
        cash = self.cfg.sub_portfolio_cents + realized
        if pos is not None and pos.contracts > 0:
            cash -= pos.cost_basis_per_contract_cents * pos.contracts
        self.store.snapshot_equity(now, self.session_date, cash, open_value, realized)


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
