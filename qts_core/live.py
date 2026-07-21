"""Live paper-trading step runner (CLI).

    .venv/bin/python -m qts_core.live --symbol SPY [--db qts_v8/state/paper.db]

Performs ONE full paper step against live (delayed) yfinance data: builds a
MarketView, runs the checklist, journals/fills through the PaperBroker if
approved, persists state, and prints the reasoned decision. Loop it with cron
or a shell loop for a continuous session; the state store makes any number of
restarts safe.

This is paper trading only. require_paper_mode() runs at import of the
session; there is no live-order path in this codebase.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from qts_core.broker import PaperBroker
from qts_core.clock import (
    TradingClock,
    effective_force_flat_et,
    is_trading_day,
    session_open_et,
)
from qts_core.config import StrategyConfig
from qts_core.paper import PaperSession
from qts_core.providers.yfinance_source import YFinanceSource
from qts_core.report import render_session_report
from qts_core.store import StateStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One live paper step (delayed data).")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--db", default="qts_v8/state/paper.db")
    parser.add_argument("--report", default=None, help="write session report to this path")
    args = parser.parse_args(argv)

    cfg = StrategyConfig()
    clock = TradingClock.system()
    now = clock.now_utc()
    session_date = clock.session_date()

    if not is_trading_day(session_date):
        print(f"[halt] {session_date} is not an XNYS session — nothing to do")
        return 0

    source = YFinanceSource(args.symbol)
    if not source.has_same_day_expiry(session_date):
        print(
            f"[halt] {args.symbol} lists NO 0DTE expiry for {session_date} (B4 gate) — "
            "no decision is possible today for this symbol"
        )
        return 0

    view = source.build_view(now=now, session_date=session_date)
    print(
        f"[view] {args.symbol} {session_date} bars={len(view.bars)} "
        f"priors={len(view.prior_sessions)} chain={len(view.chain)} "
        f"spot={view.underlying_last}"
    )

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = StateStore(args.db)
    session = PaperSession(
        store,
        PaperBroker(cfg, cfg.tick_schedule_for(args.symbol)),
        cfg,
        session_date=session_date,
        session_open_et=session_open_et(session_date),
        force_flat_at=effective_force_flat_et(
            session_date, cfg.force_flat_et, dt.timedelta(minutes=cfg.force_flat_close_buffer_min)
        ),
    )
    result = session.step(view)

    if result.halted:
        print(f"[halt] session halted: {result.halted}")
    if result.decision is not None:
        print(f"[decision] approved={result.decision.approved}")
        for c in result.decision.checks:
            mark = "PASS" if c.passed else "FAIL"
            print(f"  {mark:4} {c.name:20} {c.value}  ({c.reason})")
    for fill in result.fills:
        print(
            f"[fill/MODELED] {fill.client_order_id[:8]}… premium={fill.premium_cents}c "
            f"cash={fill.cost_cents}c commission={fill.commission_cents}c"
        )
    if result.position is not None:
        p = result.position
        print(
            f"[position] {p.occ_symbol} phase={p.phase.value} contracts={p.contracts} "
            f"basis={p.cost_basis_per_contract_cents}c/contract"
        )

    if args.report:
        Path(args.report).write_text(render_session_report(store, cfg, session_date))
        print(f"[report] written to {args.report}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
