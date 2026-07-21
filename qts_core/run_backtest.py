"""Runnable backtest CLI over REAL historical underlying bars.

    .venv/bin/python -m qts_core.run_backtest --symbol SPY --days 30

Layer 1 (signals) runs on real 5-minute bars from the provider. Layer 2
(option premiums and therefore all P&L) is MODELED via Black-Scholes — B2 is
open, so no real historical option premiums exist to test against (ADR-003 /
ADR-007). Every printed figure is labelled accordingly; none of it is a
prediction of profit.

yfinance caps intraday history at ~60 days, so --days is clamped.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import sys

from qts_core.artifacts import write_artifacts
from qts_core.backtest import SessionData, run_backtest
from qts_core.clock import (
    TradingClock,
    effective_force_flat_et,
    is_trading_day,
    session_open_et,
    to_et,
)
from qts_core.config import StrategyConfig, require_paper_mode
from qts_core.money import fmt
from qts_core.providers.yfinance_source import YFinanceSource


def build_sessions(
    symbol: str, days: int, cfg: StrategyConfig, now: dt.datetime
) -> list[SessionData]:
    """Group real intraday bars into per-session SessionData, oldest first."""
    source = YFinanceSource(symbol)
    bars = source.fetch_bars(
        now=now, days=min(days, cfg.max_intraday_days), interval_min=cfg.bar_interval_min
    )
    by_day: dict[dt.date, list] = {}
    for b in bars:
        by_day.setdefault(to_et(b.ts_close).date(), []).append(b)

    ordered = sorted(d for d in by_day if is_trading_day(d))
    # An IN-PROGRESS session must not be scored (N-05). Running at 10:45 used
    # to append today as a full SessionData, so an approved entry was closed at
    # the last available bar and recorded as a completed trade under the
    # END_OF_DATA reason — which nothing printed. A session counts only once
    # its bars reach the force-flat instant, i.e. once the strategy's own day
    # is over. This needs no notion of "today" and so cannot drift.
    complete: list[dt.date] = []
    for day in ordered:
        flat_at = effective_force_flat_et(
            day, cfg.force_flat_et, dt.timedelta(minutes=cfg.force_flat_close_buffer_min)
        )
        if by_day[day] and by_day[day][-1].ts_close >= flat_at:
            complete.append(day)
    dropped = [d for d in ordered if d not in complete]
    if dropped:
        print(f"[note] excluded {len(dropped)} incomplete session(s): {dropped[-1]} (in progress)")
    ordered = complete

    sessions: list[SessionData] = []
    for i, day in enumerate(ordered):
        priors = tuple(tuple(by_day[d]) for d in ordered[:i][-cfg.rvol_lookback_days :])
        if not priors:
            continue  # first day has no baseline: RVOL would be undefined
        sessions.append(
            SessionData(
                session_date=day,
                session_open_et=session_open_et(day),
                force_flat_at=effective_force_flat_et(
                    day,
                    cfg.force_flat_et,
                    dt.timedelta(minutes=cfg.force_flat_close_buffer_min),
                ),
                bars=tuple(by_day[day]),
                prior_sessions=priors,
                symbol=symbol,
                atm_iv=cfg.backtest_atm_iv,  # ASSUMPTION; printed above the numbers
            )
        )
    return sessions


def _diagnose(sessions: list[SessionData], cfg: StrategyConfig) -> int:
    """Why did (or didn't) the checklist fire? Zero signals is a RESULT, and a
    result deserves an explanation rather than a silent $0.00."""
    from collections import Counter

    from qts_core.backtest import _synthetic_chain
    from qts_core.models import MarketView
    from qts_core.signals.checklist import evaluate_entry

    fail: Counter[str] = Counter()
    passes: Counter[int] = Counter()
    total = 0
    best: tuple[int, str, str, list[str]] | None = None
    for sess in sessions:
        for i, bar in enumerate(sess.bars[:-1]):
            t = to_et(bar.ts_close).time()
            if not (cfg.entry_window_start <= t < cfg.entry_window_end):
                continue
            view = MarketView(
                now=bar.ts_close,
                session_date=sess.session_date,
                bars=tuple(sess.bars[: i + 1]),
                prior_sessions=sess.prior_sessions,
                chain=_synthetic_chain(sess, bar.ts_close, bar.close, cfg),
                underlying_last=bar.close,
                meta={"symbol": sess.symbol},
            )
            d = evaluate_entry(view, cfg, sess.session_open_et)
            total += 1
            ok = sum(c.passed for c in d.checks)
            passes[ok] += 1
            failed = [c.name for c in d.checks if not c.passed]
            for name in failed:
                fail[name] += 1
            if best is None or ok > best[0]:
                best = (
                    ok,
                    sess.session_date.isoformat(),
                    to_et(bar.ts_close).strftime("%H:%M"),
                    failed,
                )
    if total == 0:
        print("[halt] no in-window bars to diagnose")
        return 1
    print("=" * 72)
    print(f"DIAGNOSIS — {sessions[0].symbol}: {total} in-window bars, {len(sessions)} sessions")
    print("=" * 72)
    print("fail rate per gate (a gate at ~100% is the binding constraint):")
    for name, count in fail.most_common():
        print(f"  {name:22} {count / total:6.1%}")
    print("\ngates passing simultaneously (ALL must pass to fire):")
    for k in sorted(passes, reverse=True):
        print(f"  {k}/7: {passes[k]:5} bars ({passes[k] / total:5.1%})")
    if best is not None:
        print(
            f"\nclosest approach: {best[0]}/7 on {best[1]} {best[2]} "
            f"— blocked by {', '.join(best[3])}"
        )
    print("=" * 72)
    print("NOTE: contract_gates failures here are partly a BACKTEST ARTIFACT —")
    print("the synthetic chain snaps to a $5 strike grid at one flat IV, so the")
    print("delta band is missed more often than it would be on a real chain.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest on real bars (MODELED options P&L).")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--atm-iv", type=float, default=StrategyConfig().backtest_atm_iv)
    ap.add_argument(
        "--diagnose",
        action="store_true",
        help="explain WHY signals did or did not fire (per-gate fail rates + co-occurrence)",
    )
    args = ap.parse_args(argv)

    cfg = StrategyConfig()
    require_paper_mode(cfg)  # BOOT gate: before the clock, the network, any output
    now = TradingClock.system().now_utc()
    sessions = build_sessions(args.symbol, args.days, cfg, now)
    # Compare against the CONFIGURED default, not a second copy of the literal:
    # the two drifting apart would silently ignore --atm-iv (G-16).
    if args.atm_iv != cfg.backtest_atm_iv:
        sessions = [dataclasses.replace(s, atm_iv=args.atm_iv) for s in sessions]
    if not sessions:
        print(f"[halt] no usable sessions for {args.symbol} in the last {args.days} days")
        return 1

    if args.diagnose:
        return _diagnose(sessions, cfg)

    res = run_backtest(sessions, cfg)

    print("=" * 72)
    print(f"BACKTEST — {args.symbol}   sessions={res.sessions}   (MODELED options P&L)")
    print("=" * 72)
    print("ASSUMPTIONS (every one of these changes the numbers):")
    for k, v in sorted(res.assumptions.items()):
        print(f"  - {k}: {v}")
    print(f"  - atm_iv: flat {args.atm_iv:.0%} across all sessions (ASSUMPTION)")
    print(
        f"  - period: last {args.days} calendar days ending {to_et(now).date()} "
        "(NOT cherry-picked; the window is whatever the provider returns)"
    )
    print("-" * 72)
    print(f"  signals fired      : {res.signal_count}")
    print(f"  trades             : {len(res.trades)}")
    print(f"  net P&L (MODELED)  : {fmt(res.net_pnl_cents)}")
    print(f"  win rate           : {'n/a' if res.win_rate is None else f'{res.win_rate:.1%}'}")
    pf = res.profit_factor
    print(f"  profit factor      : {'n/a (see notes)' if pf is None else f'{pf:.2f}'}")
    print(f"  max drawdown       : {fmt(res.max_drawdown_cents)}")
    sharpe = "not reported (see notes)" if res.sharpe is None else f"{res.sharpe:.2f}"
    print(f"  Sharpe             : {sharpe}")
    if res.notes:
        print("-" * 72)
        for n in res.notes:
            print(f"  NOTE: {n}")
    print("=" * 72)
    print("These are MODELED results on synthetic option premiums, not a record")
    print("of trades and not a forecast. Real option prices will differ.")

    if cfg.backtest_artifacts:
        json_path, md_path = write_artifacts(
            res, symbol=args.symbol, days=args.days, atm_iv=args.atm_iv, generated_at=now
        )
        print(f"[artifacts] {json_path}")
        print(f"[artifacts] {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
