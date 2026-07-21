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
from collections.abc import Callable
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


def _guarded[T](symbol: str, fn: Callable[..., T], *args: object) -> T | None:
    """Run one symbol's work; a failure costs that symbol, never the pass."""
    try:
        return fn(*args)
    except Exception as exc:
        print(f"[error] {symbol}: {type(exc).__name__}: {exc}")
        return None


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
        # Review finding F4: the [WARN] below claimed "every run" while this
        # early return skipped it on non-trading days — exactly when an owner
        # doing weekend housekeeping is most likely to look. Warn here too if
        # the db already exists (never create one just to say it is empty).
        if Path(args.db).exists():
            weekend_store = StateStore(args.db)
            try:
                for occ in sorted(weekend_store.expired_positions(as_of=session_date)):
                    print(f"[WARN] {occ}: expired UNSETTLED — owner action required.")
            finally:
                weekend_store.close()
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

    # N-03: a position that expired without a closing order is invisible to
    # realized P&L (which sums SELL legs). The operator must SEE it, every
    # run, until it is resolved — not discover it in a disagreeing equity row.
    stranded = store.expired_positions(as_of=session_date)
    for occ in sorted(stranded):
        state = stranded[occ]
        print(
            f"[WARN] {occ}: expired UNSETTLED — "
            f"{state.get('contracts')} contract(s), basis not in realized P&L. "
            "Owner action required."
        )

    for symbol in symbols:
        print(f"--- {symbol} ---")
        # tick_schedule_for fails loud on unknown symbols (N-08) — but a
        # config gap on ONE symbol must cost that symbol, never the pass
        # (N-06): the next symbol may be holding a position that needs its
        # exits managed.
        try:
            tick = cfg.tick_schedule_for(symbol)
        except KeyError as exc:
            print(f"[error] {symbol}: {exc.args[0]}")
            continue
        # The session is built BEFORE the provider is consulted, because the
        # safety rail must not depend on the data feed (N-02): a throttled or
        # unavailable provider is exactly when an open position most needs
        # flattening, and the old loop `continue`d past step() in that case.
        session = PaperSession(
            store,
            PaperBroker(cfg, tick),
            cfg,
            session_date=session_date,
            session_open_et=session_open_et(session_date),
            force_flat_at=force_flat,
            symbol=symbol,
        )
        source = YFinanceSource(symbol)
        view = None
        try:
            # B4: 0DTE availability is CHECKED per symbol per day, never assumed.
            # NVDA lists Mon/Wed/Fri only; SPY/QQQ list dailies (measured 2026-07-21).
            if source.has_same_day_expiry(session_date):
                view = source.build_view(now=now, session_date=session_date)
            else:
                print(f"[halt] {symbol}: no 0DTE expiry listed for {session_date} (B4 gate)")
        except Exception as exc:  # provider hiccup must not kill the whole run
            print(f"[error] {symbol}: {type(exc).__name__}: {exc}")

        if view is None:
            # No usable view. Entry is impossible either way, but an EXIT may
            # still be overdue — and it needs only the clock and the ledger.
            forced = _guarded(symbol, session.force_flat_if_due, now)
            if forced is not None and forced.fills:
                print(
                    f"[force-flat/UNMARKED] {symbol}: liquidated at worst-case "
                    "zero proceeds — no market data was available"
                )
            continue

        print(
            f"[view] {symbol} bars={len(view.bars)} priors={len(view.prior_sessions)} "
            f"chain={len(view.chain)} spot={view.underlying_last}"
        )
        # The step owns position state, so a failure here is strictly more
        # dangerous than a provider failure — and it was the one path with no
        # guard at all (N-06). Contain it per symbol, never lose the pass.
        result = _guarded(symbol, session.step, view)
        if result is None:
            continue

        if result.data_age_min is not None:
            flag = "" if result.data_age_min <= cfg.max_bar_age_min else "  <-- OVER LIMIT"
            gate = "on" if cfg.staleness_gate_enabled else "off"
            print(
                f"[data] {symbol} newest bar {result.data_age_min:.1f} min old "
                f"(limit {cfg.max_bar_age_min}m, gate {gate}){flag}"
            )
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
