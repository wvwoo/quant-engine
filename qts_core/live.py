"""Live paper-trading step runner (CLI).

    .venv/bin/python -m qts_core.live --symbols SPY [--db qts_v8/state/paper.db]

Performs ONE full paper step against live (delayed) yfinance data: builds a
MarketView, runs the checklist, journals/fills through the PaperBroker if
approved, persists state, and prints the reasoned decision. Loop it with cron
or a shell loop for a continuous session; the state store makes any number of
restarts safe.

This is paper trading only. require_paper_mode() is called at the TOP of
main(), before any network I/O — it is not an import-time gate, and relying on
PaperSession's constructor was not a boot gate at all: a run where every symbol
fails the B4 availability check never constructs a session, so the check was
reachable-only-sometimes (G-02). There is no live-order path in this codebase.
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
from qts_core.config import StrategyConfig, require_paper_mode
from qts_core.paper import PaperSession
from qts_core.providers.yfinance_source import YFinanceSource
from qts_core.report import render_session_report
from qts_core.store import StateStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One live paper step (delayed data).")
    parser.add_argument(
        "--symbols",
        default=None,
        help="comma-separated; defaults to the configured universe",
    )
    parser.add_argument("--db", default="qts_v8/state/paper.db")
    parser.add_argument("--report", default=None, help="write session report to this path")
    args = parser.parse_args(argv)

    cfg = StrategyConfig()
    require_paper_mode(cfg)  # BOOT gate: before the clock, the network, the db
    clock = TradingClock.system()
    now = clock.now_utc()
    session_date = clock.session_date()

    if not is_trading_day(session_date):
        print(f"[halt] {session_date} is not an XNYS session — nothing to do")
        return 0

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else list(cfg.tickers)
    )
    force_flat = effective_force_flat_et(
        session_date, cfg.force_flat_et, dt.timedelta(minutes=cfg.force_flat_close_buffer_min)
    )
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = StateStore(args.db)

    for symbol in symbols:
        print(f"--- {symbol} ---")
        source = YFinanceSource(symbol)
        # B4: 0DTE availability is CHECKED per symbol per day, never assumed.
        # NVDA lists Mon/Wed/Fri only; SPY/QQQ list dailies (measured 2026-07-21).
        if not source.has_same_day_expiry(session_date):
            print(f"[halt] {symbol}: no 0DTE expiry listed for {session_date} (B4 gate)")
            continue
        try:
            view = source.build_view(now=now, session_date=session_date)
        except Exception as exc:  # provider hiccup must not kill the whole run
            print(f"[error] {symbol}: {type(exc).__name__}: {exc}")
            continue
        print(
            f"[view] {symbol} bars={len(view.bars)} priors={len(view.prior_sessions)} "
            f"chain={len(view.chain)} spot={view.underlying_last}"
        )
        session = PaperSession(
            store,
            PaperBroker(cfg, cfg.tick_schedule_for(symbol)),
            cfg,
            session_date=session_date,
            session_open_et=session_open_et(session_date),
            force_flat_at=force_flat,
            symbol=symbol,
        )
        result = session.step(view)

        if result.halted:
            print(f"[halt] {symbol}: {result.halted}")
        if result.decision is not None:
            print(f"[decision] {symbol} approved={result.decision.approved}")
            for c in result.decision.checks:
                mark = "PASS" if c.passed else "FAIL"
                print(f"  {mark:4} {c.name:20} {c.value}  ({c.reason})")
        for fill in result.fills:
            print(
                f"[fill/MODELED] {symbol} {fill.client_order_id[:8]}… "
                f"premium={fill.premium_cents}c cash={fill.cost_cents}c"
            )
        if result.position is not None:
            p = result.position
            print(
                f"[position] {p.occ_symbol} phase={p.phase.value} "
                f"contracts={p.contracts} basis={p.cost_basis_per_contract_cents}c"
            )

    if args.report:
        Path(args.report).write_text(render_session_report(store, cfg, session_date))
        print(f"[report] written to {args.report}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
