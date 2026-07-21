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

from qts_core.config import PROVENANCE, StrategyConfig
from qts_core.money import fmt
from qts_core.store import StateStore


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
        add("| Time | Symbol | Verdict | Failed checks |")
        add("| :--- | :--- | :--- | :--- |")
        for ts, symbol, ok, blob in decisions:
            payload = json.loads(blob)
            failed = ", ".join(c["name"] for c in payload["checks"] if not c["passed"]) or "—"
            verdict = "🟢 APPROVED" if ok else "🔴 VETOED"
            add(f"| {ts[11:16]} | {symbol} | {verdict} | {failed} |")
        add("")

    if approved:
        payload = json.loads(approved[0][3])
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
