"""Persisted backtest results — JSON for machines, Markdown for the owner.

A stdout backtest dies with the terminal, so the owner could not review a run
after it scrolled (G-12). But a FILE outlives its caveats: it can be forwarded,
screenshotted, or quoted back months later as if it were a track record. That
asymmetry is the whole reason the MODELED tag and the B2 caveat are written
into EVERY artifact — header, per-field, and in the JSON itself — rather than
printed once at the top and forgotten.

Nothing here is a profit projection. B2 (no real historical option premiums,
ADR-007) is open, so every P&L figure is Black-Scholes on synthetic premiums.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path

from qts_core.backtest import BacktestResult
from qts_core.clock import to_et
from qts_core.money import fmt

DEFAULT_DIR = Path("qts_v8/state/backtests")

MODELED_BANNER = (
    "MODELED — options premiums are Black-Scholes on synthetic quotes, NOT real "
    "historical option prices (B2 open, ADR-003/ADR-007). Not a forecast, not a "
    "record of trades, not investment advice."
)


def artifact_stem(symbol: str, days: int, generated_at: dt.datetime) -> str:
    """Deterministic name: same run, same file. No wall-clock read in here."""
    stamp = to_et(generated_at).strftime("%Y%m%dT%H%M%S")
    return f"{symbol.upper()}-{days}d-{stamp}"


def to_payload(
    res: BacktestResult, *, symbol: str, days: int, atm_iv: float, generated_at: dt.datetime
) -> dict[str, object]:
    """JSON-safe result. Every numeric block carries the tag with it, so no
    consumer can slice a number away from its caveat."""
    return {
        "schema": "qts-backtest/1",
        "modeled": True,
        "modeled_note": MODELED_BANNER,
        "barrier_open": "B2: no real historical option premiums are available",
        "symbol": symbol.upper(),
        "days_requested": days,
        "generated_at_et": to_et(generated_at).isoformat(),
        "atm_iv": atm_iv,
        "assumptions": dict(res.assumptions),
        "notes": list(res.notes),
        "results": {
            "modeled": True,
            "sessions": res.sessions,
            "signal_count": res.signal_count,
            "trades": len(res.trades),
            "net_pnl_cents_MODELED": res.net_pnl_cents,
            "win_rate": res.win_rate,
            "profit_factor": res.profit_factor,
            "max_drawdown_cents_MODELED": res.max_drawdown_cents,
            "sharpe": res.sharpe,
        },
        "trades": [
            {**dataclasses.asdict(t), "session_date": t.session_date.isoformat()}
            for t in res.trades
        ],
    }


def to_markdown(
    res: BacktestResult, *, symbol: str, days: int, atm_iv: float, generated_at: dt.datetime
) -> str:
    """Owner-facing rendering. Deterministic: sorted iteration, fixed formats.

    Renders from the typed result, not from the JSON payload, so the two
    representations cannot drift into disagreeing about the same run.
    """
    lines: list[str] = []
    add = lines.append
    add(f"# Backtest — {symbol.upper()} ({days}d requested)")
    add("")
    add(f"> **{MODELED_BANNER}**")
    add("")
    add(f"* **Generated (ET):** `{to_et(generated_at).isoformat()}`")
    add(f"* **Sessions scored:** `{res.sessions}`")
    add(f"* **Flat ATM IV:** `{atm_iv:.0%}`")
    add("")
    add("## Assumptions (every one of these changes the numbers)")
    add("")
    for k, v in sorted(res.assumptions.items()):
        add(f"* **{k}:** {v}")
    add("")
    add("## Results (MODELED)")
    add("")
    add("| Metric | Value | Tag |")
    add("| :--- | ---: | :--- |")
    add(f"| signals fired | {res.signal_count} | measured on REAL bars |")
    add(f"| trades | {len(res.trades)} | MODELED |")
    add(f"| net P&L | {fmt(res.net_pnl_cents)} | MODELED |")
    wr = "n/a" if res.win_rate is None else f"{res.win_rate:.1%}"
    add(f"| win rate | {wr} | MODELED |")
    pf = "n/a" if res.profit_factor is None else f"{res.profit_factor:.2f}"
    add(f"| profit factor | {pf} | MODELED |")
    add(f"| max drawdown | {fmt(res.max_drawdown_cents)} | MODELED |")
    sh = "not reported" if res.sharpe is None else f"{res.sharpe:.2f}"
    add(f"| Sharpe | {sh} | MODELED |")
    add("")
    if res.notes:
        add("## Notes")
        add("")
        for n in res.notes:
            add(f"* {n}")
        add("")
    add("---")
    add("")
    add(
        "*Layer 1 (signals) ran on REAL 5-minute bars and is verifiable. Layer 2 "
        "(every P&L figure above) is MODELED: it is not a forecast, not a record "
        "of trades, and not investment advice. Zero signals is a RESULT, not a "
        "failure to report.*"
    )
    add("")
    return "\n".join(lines)


def write_artifacts(
    res: BacktestResult,
    *,
    symbol: str,
    days: int,
    atm_iv: float,
    generated_at: dt.datetime,
    directory: Path | str = DEFAULT_DIR,
) -> tuple[Path, Path]:
    """Write <stem>.json and <stem>.md. Returns both paths."""
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    payload = to_payload(res, symbol=symbol, days=days, atm_iv=atm_iv, generated_at=generated_at)
    stem = artifact_stem(symbol, days, generated_at)
    json_path = out / f"{stem}.json"
    md_path = out / f"{stem}.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    md_path.write_text(
        to_markdown(res, symbol=symbol, days=days, atm_iv=atm_iv, generated_at=generated_at)
    )
    return json_path, md_path


def latest_payload(directory: Path | str = DEFAULT_DIR) -> dict[str, object] | None:
    """Most recent saved result, or None. Ordering is by the deterministic
    filename stamp, never by filesystem mtime."""
    out = Path(directory)
    if not out.exists():
        return None
    files = sorted(out.glob("*.json"))
    if not files:
        return None
    try:
        loaded: dict[str, object] = json.loads(files[-1].read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return loaded
