"""Trade-report generator: the institutional report's own format, generated
from real session state — with honesty banners the original lacked.

Every figure that passed through the paper fill model or the BS premium model
carries the MODELED tag (ADR-003/007). Deterministic rendering: fixed float
formats, sorted iteration, no wall-clock reads. No SQL is built here — all
storage access goes through StateStore's parameterized queries.
"""

from __future__ import annotations

import datetime as dt
import json

from qts_core.clock import to_et
from qts_core.config import PROVENANCE, StrategyConfig
from qts_core.money import fmt
from qts_core.store import StateStore


def _et_hhmm(ts: str) -> str:
    """Stored ISO stamp -> HH:MM in EXCHANGE time.

    This document is headed ET throughout, but the stamps are written from
    ``TradingClock.now_utc()`` in production, so slicing the raw string printed
    UTC — a 10:14 ET decision rendered as 14:14 (G-08). The golden fixtures
    stamp in NY, where the slice happened to be correct, so the regression gate
    was structurally unable to see this. Convert explicitly instead.
    """
    return to_et(dt.datetime.fromisoformat(ts)).strftime("%H:%M")


def _banner(cfg: StrategyConfig) -> str:
    prov = PROVENANCE["commission_per_contract_cents"][0].value
    return (
        "> **PAPER TRADING — MODELED FILLS.** No real order was placed. Fills are\n"
        f"> simulated at quote +/- {cfg.sizing_slippage_buffer_bp}bp slippage with "
        f"{cfg.commission_per_contract_cents}c/contract commission\n"
        f"> (config provenance: {prov}). Nothing here is investment advice or a\n"
        "> promise of returns.\n"
    )


def render_session_report(store: StateStore, cfg: StrategyConfig, session_date: dt.date) -> str:
    orders = store.orders_for_session(session_date)
    decisions = store.decisions_for_session(session_date)
    equity = store.equity_series(session_date)
    realized = store.realized_pnl_today(session_date)

    lines: list[str] = []
    add = lines.append
    add("# 📊 QTS PAPER TRADING REPORT")
    add("")
    add(_banner(cfg))
    add("## 🌐 TELEMETRY & ROUTING METADATA")
    add(f"* **Session Date:** `{session_date.isoformat()}`")
    add("* **Engine:** `qts-core v0.1.0 / paper`")
    add(f"* **Decisions Evaluated:** `{len(decisions)}`")
    add(f"* **Orders (journaled/filled):** `{len(orders)}`")
    add("")

    approved = [d for d in decisions if d[2] == 1]
    if decisions:
        add("## 🔍 ENTRY CHECKLIST AUDIT")
        add("")
        add("| Time (ET) | Symbol | Verdict | Failed checks |")
        add("| :--- | :--- | :--- | :--- |")
        for ts, symbol, ok, blob in decisions:
            payload = json.loads(blob)
            failed = ", ".join(c["name"] for c in payload["checks"] if not c["passed"]) or "—"
            verdict = "🟢 APPROVED" if ok else "🔴 VETOED"
            add(f"| {_et_hhmm(ts)} | {symbol} | {verdict} | {failed} |")
        add("")

    if approved:
        payload = json.loads(approved[-1][3])  # heading says LAST (finding RPT-*)
        add("### Last approved decision — full checklist")
        add("")
        add("| Check | Value | Threshold | Status |")
        add("| :--- | :--- | :--- | :--- |")
        for c in payload["checks"]:
            status = "🟢 PASS" if c["passed"] else "🔴 FAIL"
            add(f"| **{c['name']}** | {c['value']} | {c['threshold']} | {status} |")
        sel = payload.get("selection")
        if sel:
            add("")
            add(
                f"Selected: `{sel['occ']}` — delta {sel['delta']:.4f} "
                f"(IV {sel['iv']:.2%}, source: {sel['iv_source']}), "
                f"mid {fmt(sel['mid_cents'])}, spread {fmt(sel['spread_cents'])}"
            )
        add("")

    if orders:
        add("## 🧮 ORDER LEDGER (MODELED FILLS)")
        add("")
        add("| Seq | Side | Reason | Contracts | Limit | Fill | Cash Δ / Realized | Status |")
        add("| :-- | :-- | :-- | :-- | :-- | :-- | :-- | :-- |")
        for o in orders:
            fill_p = fmt(o["fill_premium_cents"]) if o["fill_premium_cents"] is not None else "—"
            cash = fmt(o["fill_cost_cents"]) if o["fill_cost_cents"] is not None else "—"
            add(
                f"| {o['seq']} | {o['side']} | {o['reason']} | {o['contracts']} "
                f"| {fmt(o['limit_cents'])} | {fill_p} | {cash} | {o['status']} |"
            )
        add("")

    add("## 🛡️ RISK PARAMETERS IN FORCE")
    add("")
    add("| Parameter | Value | Provenance |")
    add("| :--- | :--- | :--- |")
    rows = [
        ("sub_portfolio", fmt(cfg.sub_portfolio_cents), "sub_portfolio_cents"),
        ("stop_loss", f"{cfg.stop_loss_bp / 100:.0f}% of entry quote", "stop_loss_bp"),
        (
            "tranche1",
            f"+{cfg.tranche1_gain_bp / 100:.0f}% (sell {cfg.tranche1_contracts})",
            "tranche1_gain_bp",
        ),
        ("trailing", f"{cfg.trailing_bp / 100:.0f}% from peak", "trailing_bp"),
        ("runner_floor", f"+{cfg.runner_floor_bp / 100:.0f}% of entry quote", "runner_floor_bp"),
        ("force_flat", cfg.force_flat_et.strftime("%H:%M ET"), "force_flat_et"),
        ("daily_loss_limit", fmt(cfg.daily_loss_limit_cents), "daily_loss_limit_cents"),
    ]
    for name, value, key in rows:
        add(f"| **{name}** | {value} | {PROVENANCE[key][0].value} |")
    add("")

    expired = store.expired_positions(as_of=session_date)
    if expired:
        # N-03: "reported, never silently settled" was a promise with zero
        # callers. We do not own assignment data, so nothing is fabricated —
        # but realized P&L sums SELL legs only, so an unsold position's basis
        # is absent from every P&L figure below. Say so, with the number.
        add("## ⚠️ EXPIRED / UNSETTLED POSITIONS")
        add("")
        add("| OCC | Contracts | Basis committed |")
        add("| :-- | :-- | :-- |")
        stranded = 0
        for occ in sorted(expired):
            state = expired[occ]
            contracts = int(str(state.get("contracts", 0)))
            basis = int(str(state.get("cost_basis_per_contract_cents", 0)))
            stranded += basis * contracts
            add(f"| `{occ}` | {contracts} | {fmt(basis * contracts)} |")
        add("")
        add(
            f"> **{fmt(stranded)} of committed basis is NOT reflected in the realized "
            "P&L below.** These 0DTE rows expired without a closing order; pricing an "
            "assignment needs data this system does not own, so they are reported — "
            "never silently settled. Resolving them is an owner action."
        )
        add("")

    add("## 📈 SESSION P&L (PAPER, MODELED)")
    add("")
    add(f"* **Realized P&L today:** **{fmt(realized)}**")
    if equity:
        last = equity[-1]
        add(f"* **Cash:** {fmt(last[1])} | **Open position value (mark):** {fmt(last[2])}")
        add(f"* **Equity snapshots:** {len(equity)}")
    add("")
    add("---")
    add(
        "*Assumptions: paper fills at quote±"
        f"{cfg.sizing_slippage_buffer_bp}bp, commission "
        f"{cfg.commission_per_contract_cents}c/contract/side, marks at option mid. "
        "Cherry-picking of sessions is prohibited; this report covers one full "
        "session as recorded in the state store.*"
    )
    add("")
    return "\n".join(lines)
