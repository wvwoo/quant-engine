"""FastAPI backend serving REAL state to the dashboard.

Every number the dashboard shows comes from the SQLite state store or the
provenance-tagged config — nothing is hard-coded in the page (the three
original dashboards were 100% static mockups; finding dashboards/*).

Run:  QTS_DB=qts_v8/state/paper.db .venv/bin/uvicorn qts_core.api:app --port 8787
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from qts_core.artifacts import latest_payload
from qts_core.clock import TradingClock
from qts_core.config import PROVENANCE, StrategyConfig, require_paper_mode
from qts_core.money import fmt
from qts_core.store import StateStore

app = FastAPI(title="qts-core", version="0.1.0")
_CFG = StrategyConfig()
require_paper_mode(_CFG)  # boot gate: this process can never be a live trader

_DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "dashboard.html"


def _db_path() -> str:
    return os.environ.get("QTS_DB", "qts_v8/state/paper.db")


def _store() -> StateStore | None:
    path = _db_path()
    if not Path(path).exists():
        return None
    return StateStore(path)


def _session_date() -> dt.date:
    """Today's EXCHANGE session date; QTS_SESSION_DATE pins it for demos/tests.

    A hardcoded default silently served a stale session while the live runner
    wrote to today's — the dashboard looked healthy and reported nothing
    (finding QTS-1, live-api reviewer).
    """
    raw = os.environ.get("QTS_SESSION_DATE")
    if raw:
        return dt.date.fromisoformat(raw)
    return TradingClock.system().session_date()


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(_DASHBOARD)


@app.get("/api/overview")
def overview() -> JSONResponse:
    store = _store()
    session = _session_date()
    if store is None:
        return JSONResponse({"connected": False, "reason": f"state db not found at {_db_path()}"})
    try:
        realized = store.realized_pnl_today(session)
        equity = store.equity_series(session)
        last = equity[-1] if equity else None
        cash = last[1] if last else _CFG.sub_portfolio_cents
        open_value = last[2] if last else 0
        return JSONResponse(
            {
                "connected": True,
                "mode": "PAPER (MODELED fills)",
                "session_date": session.isoformat(),
                "sub_portfolio_cents": _CFG.sub_portfolio_cents,
                "cash_cents": cash,
                "open_value_cents": open_value,
                "equity_cents": cash + open_value,
                # On a day the engine never snapshotted, the numbers above are
                # the CONFIGURED mandate, not a measurement. Saying so is the
                # difference between a dashboard and a decoration (G-15).
                "equity_measured": last is not None,
                "realized_pnl_today_cents": realized,
                "realized_pnl_today": fmt(realized),
                "equity_points": [{"ts": ts, "equity_cents": c + ov} for ts, c, ov, _ in equity],
                # Expired rows are REPORTED separately, never mixed into live
                # holdings: the session loop refuses to trade them, so the API
                # must not present them as tradeable either (G-07).
                "open_positions": store.open_positions(as_of=session),
                "expired_positions": store.expired_positions(as_of=session),
            }
        )
    finally:
        store.close()


@app.get("/api/risk/rules")
def risk_rules() -> JSONResponse:
    rules: list[dict[str, Any]] = []
    cfg = _CFG
    entries = [
        ("sub_portfolio", fmt(cfg.sub_portfolio_cents), "sub_portfolio_cents"),
        (
            "sizing_slippage_buffer",
            f"{cfg.sizing_slippage_buffer_bp}bp",
            "sizing_slippage_buffer_bp",
        ),
        ("spread_reject_max", f"{cfg.spread_reject_max_bp}bp", "spread_reject_max_bp"),
        ("spread_abs_max", fmt(cfg.spread_abs_max_cents), "spread_abs_max_cents"),
        ("delta_band", f"{cfg.delta_min}-{cfg.delta_max}", "delta_min"),
        ("iv_max", f"{cfg.iv_max:.0%}", "iv_max"),
        ("rvol_min", f"{cfg.rvol_min}", "rvol_min"),
        ("rsi", f"Wilder-{cfg.rsi_period} < {cfg.rsi_overbought:.0f}", "rsi_period"),
        ("macd", f"{cfg.macd_fast}/{cfg.macd_slow}/{cfg.macd_signal} hist>0", "macd_fast"),
        (
            "entry_window",
            f"{cfg.entry_window_start:%H:%M}-{cfg.entry_window_end:%H:%M} ET",
            "entry_window_start",
        ),
        ("stop_loss", f"{cfg.stop_loss_bp / 100:.0f}%", "stop_loss_bp"),
        (
            "tranche1",
            f"+{cfg.tranche1_gain_bp / 100:.0f}% sell {cfg.tranche1_contracts}",
            "tranche1_gain_bp",
        ),
        ("trailing", f"{cfg.trailing_bp / 100:.0f}%", "trailing_bp"),
        ("force_flat", cfg.force_flat_et.strftime("%H:%M ET"), "force_flat_et"),
        ("daily_loss_limit", fmt(cfg.daily_loss_limit_cents), "daily_loss_limit_cents"),
        ("live_trading", "OFF (owner-gated; no live broker exists)", "live_trading"),
    ]
    for name, value, key in entries:
        tier, citation = PROVENANCE[key]
        rules.append({"id": name, "value": value, "tier": tier.value, "citation": citation})
    return JSONResponse({"version": "qts-core 0.1.0", "rules": rules})


@app.get("/api/execution")
def execution() -> JSONResponse:
    store = _store()
    session = _session_date()
    if store is None:
        return JSONResponse({"connected": False, "orders": []})
    try:
        orders = [
            {
                "seq": o["seq"],
                "side": o["side"],
                "reason": o["reason"],
                "contracts": o["contracts"],
                "limit": fmt(o["limit_cents"]),
                "fill": (
                    fmt(o["fill_premium_cents"]) if o["fill_premium_cents"] is not None else None
                ),
                "status": o["status"],
                "modeled": True,
            }
            for o in store.orders_for_session(session)
        ]
        decisions = [
            {"ts": ts, "symbol": s, "approved": bool(a), "detail": json.loads(blob)}
            for ts, s, a, blob in store.decisions_for_session(session)
        ]
        return JSONResponse({"connected": True, "orders": orders, "decisions": decisions})
    finally:
        store.close()


@app.get("/api/backtest")
def backtest() -> JSONResponse:
    """Latest SAVED backtest, or an explicit absence.

    Serving nothing is the honest answer when nothing has been run; the
    endpoint never synthesises or estimates. Every figure it does serve arrives
    already tagged MODELED by the artifact writer (ADR-003/007).
    """
    payload = latest_payload()
    if payload is None:
        return JSONResponse(
            {
                "available": False,
                "reason": (
                    "no saved backtest. Enable StrategyConfig.backtest_artifacts and run "
                    "python -m qts_core.run_backtest"
                ),
            }
        )
    return JSONResponse({"available": True, **payload})


@app.get("/api/system")
def system() -> JSONResponse:
    store = _store()
    ok = store is not None
    if store is not None:
        store.close()
    return JSONResponse(
        {
            "engine": "qts-core 0.1.0",
            "mode": "PAPER",
            "live_trading": "OFF (no live broker exists in this codebase)",
            "state_db": {"path": _db_path(), "reachable": ok},
            "modules": {
                "providers": "yfinance (free tier, ADR-007)",
                "pricing": "local Black-Scholes (ADR-002)",
                "signals": "7-gate checklist",
                "risk": "IRONCLAD ladder + SAFETY rails",
                "broker": "PaperBroker (modeled fills)",
                "store": "SQLite WAL + fullfsync",
            },
        }
    )
