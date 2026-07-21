"""The entry checklist: every filter from the reference report, as one
reasoned decision.

Output mirrors the report's own tables: each check carries its name, the
measured value, the threshold, PASS/FAIL, and a human reason. A missing
input is a FAIL with reason — never a silent skip (the legacy scanner's
broad-except is the anti-pattern this replaces).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from qts_core.clock import in_time_window, session_close_et, to_et
from qts_core.config import StrategyConfig
from qts_core.models import MarketView, OptionQuote
from qts_core.pricing import analyze_contract
from qts_core.signals import indicators as ind


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    value: str  # deterministic string rendering (golden-stable)
    threshold: str
    reason: str


@dataclass(frozen=True, slots=True)
class ContractSelection:
    quote: OptionQuote
    delta: float
    iv: float
    iv_source: str
    mid_cents: int
    spread_cents: int
    spread_bp: int


@dataclass(frozen=True, slots=True)
class EntryDecision:
    session_date: dt.date
    now_et: str
    symbol: str
    checks: tuple[CheckResult, ...]
    approved: bool
    selection: ContractSelection | None
    veto_reasons: tuple[str, ...]


def _fmt(x: float | None, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _time_to_expiry_years(now_et: dt.datetime, expiry: dt.date) -> float:
    """Years to the ACTUAL session close, not a hardcoded 16:00.

    On a half day the market closes 13:00; assuming 16:00 makes T roughly
    twice its true value, inflating every modeled premium and quietly
    defeating the IV and delta gates (finding F1).
    """
    close = session_close_et(expiry)
    seconds = max((close - now_et).total_seconds(), 60.0)
    return seconds / (365.0 * 24.0 * 3600.0)


def select_contract(
    view: MarketView, cfg: StrategyConfig, *, right: str = "C"
) -> tuple[ContractSelection | None, list[str]]:
    """Pick the tradeable same-day contract in the delta band, or explain why not.

    Gates per contract: quotable market, same-day expiry (B4: never assumed —
    absence is a reported reason), spread ceilings (ADR-005: both the absolute
    SPEC $0.05 and the 3.5% reject gate), IV ceiling, delta band. Among
    survivors: nearest |delta - 0.5|.
    """
    reasons: list[str] = []
    if view.underlying_last is None:
        return None, ["no underlying price in view"]
    same_day = [q for q in view.chain if q.right == right and q.expiry == view.session_date]
    if not same_day:
        return None, [f"no same-day (0DTE) {right} expiry listed for {view.session_date}"]
    now_et = to_et(view.now)
    candidates: list[ContractSelection] = []
    for q in same_day:
        mid = q.mid_cents
        spread = q.spread_cents
        spread_bp = q.spread_bp_of_mid()
        if mid is None or spread is None or spread_bp is None:
            reasons.append(f"{q.occ_symbol}: no market (bid={q.bid_cents} ask={q.ask_cents})")
            continue
        if spread > cfg.spread_abs_max_cents:
            reasons.append(f"{q.occ_symbol}: spread {spread}c > {cfg.spread_abs_max_cents}c")
            continue
        if spread_bp > cfg.spread_reject_max_bp:
            reasons.append(f"{q.occ_symbol}: spread {spread_bp}bp > {cfg.spread_reject_max_bp}bp")
            continue
        analytics = analyze_contract(
            mid_price=mid / 100.0,
            spot=view.underlying_last,
            strike=q.strike_cents / 100.0,
            t_years=_time_to_expiry_years(now_et, q.expiry),
            rate=cfg.risk_free_rate,
            call=(right == "C"),
            iv_hint=q.iv_hint,
        )
        if analytics.iv is None or analytics.delta is None:
            reasons.append(f"{q.occ_symbol}: IV unavailable from mid or hint")
            continue
        if analytics.iv >= cfg.iv_max:
            reasons.append(f"{q.occ_symbol}: IV {analytics.iv:.2%} >= {cfg.iv_max:.0%}")
            continue
        if not (cfg.delta_min <= analytics.delta <= cfg.delta_max):
            reasons.append(
                f"{q.occ_symbol}: delta {analytics.delta:.4f} outside "
                f"[{cfg.delta_min}, {cfg.delta_max}]"
            )
            continue
        candidates.append(
            ContractSelection(
                quote=q,
                delta=analytics.delta,
                iv=analytics.iv,
                iv_source=analytics.iv_source,
                mid_cents=mid,
                spread_cents=spread,
                spread_bp=spread_bp,
            )
        )
    if not candidates:
        return None, reasons
    best = min(candidates, key=lambda c: (abs(c.delta - 0.5), c.quote.strike_cents))
    return best, reasons


def evaluate_entry(
    view: MarketView, cfg: StrategyConfig, session_open_et: dt.datetime
) -> EntryDecision:
    """Run the full checklist. approved iff EVERY check passes (conjunction —
    the report's own gate: 'All Core Triggers Validated')."""
    now_et = to_et(view.now)
    checks: list[CheckResult] = []

    in_window = in_time_window(view.now, cfg.entry_window_start, cfg.entry_window_end)
    checks.append(
        CheckResult(
            "trading_window",
            in_window,
            now_et.strftime("%H:%M ET"),
            f"{cfg.entry_window_start:%H:%M}-{cfg.entry_window_end:%H:%M} ET",
            "inside entry window" if in_window else "outside entry window",
        )
    )

    # RSI/MACD warm-up: a 26+9-bar MACD on 5m bars cannot exist by 10:15 from
    # today's bars alone (9 bars). The indicator series therefore continues
    # across the most recent prior session, overnight gap included — standard
    # intraday construction, flagged ASSUMPTION (the report is silent).
    # VWAP and ORB remain strictly session-local by definition.
    session_closes = [b.close for b in view.bars]
    warmup = [b.close for b in view.prior_sessions[-1]] if view.prior_sessions else []
    closes = warmup + session_closes
    last_close = session_closes[-1] if session_closes else None

    orb = ind.orb_high(view.bars, session_open_et, cfg.orb_minutes, view.now)
    orb_ok = orb is not None and last_close is not None and last_close > orb
    checks.append(
        CheckResult(
            "orb_breakout",
            orb_ok,
            f"close {_fmt(last_close)} vs ORB high {_fmt(orb)}",
            "close > ORB high",
            "breakout above opening range"
            if orb_ok
            else ("opening range incomplete" if orb is None else "no breakout"),
        )
    )

    vw = ind.vwap(view.bars)
    vwap_ok = vw is not None and last_close is not None and last_close > vw
    checks.append(
        CheckResult(
            "vwap_alignment",
            vwap_ok,
            f"close {_fmt(last_close)} vs VWAP {_fmt(vw)}",
            "close > session VWAP",
            "bullish expansion above VWAP" if vwap_ok else "not above VWAP",
        )
    )

    rv = ind.rvol(view.bars, view.prior_sessions, view.now, cfg.rvol_lookback_days, cfg.rvol_mode)
    rvol_ok = rv is not None and rv >= cfg.rvol_min
    checks.append(
        CheckResult(
            "rvol",
            rvol_ok,
            _fmt(rv),
            f">= {cfg.rvol_min:.1f} ({cfg.rvol_mode}, {cfg.rvol_lookback_days}d)",
            "volume expansion confirmed"
            if rvol_ok
            else ("no prior-session baseline" if rv is None else "insufficient relative volume"),
        )
    )

    r = ind.rsi(closes, cfg.rsi_period)
    rsi_ok = r is not None and r < cfg.rsi_overbought
    checks.append(
        CheckResult(
            "rsi_non_overbought",
            rsi_ok,
            _fmt(r, 1),
            f"< {cfg.rsi_overbought:.0f} (Wilder-{cfg.rsi_period}, ASSUMPTION)",
            "momentum not overbought"
            if rsi_ok
            else ("insufficient bars for RSI" if r is None else "overbought"),
        )
    )

    hist = ind.macd_histogram(closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    macd_ok = hist is not None and hist > 0
    checks.append(
        CheckResult(
            "macd_histogram",
            macd_ok,
            _fmt(hist, 4),
            f"> 0 ({cfg.macd_fast}/{cfg.macd_slow}/{cfg.macd_signal}, ASSUMPTION)",
            "MACD histogram positive"
            if macd_ok
            else ("insufficient bars for MACD" if hist is None else "histogram non-positive"),
        )
    )

    selection, veto = select_contract(view, cfg)
    checks.append(
        CheckResult(
            "contract_gates",
            selection is not None,
            selection.quote.occ_symbol if selection else "none",
            f"0DTE {cfg.delta_min}<=delta<={cfg.delta_max}, IV<{cfg.iv_max:.0%}, "
            f"spread<={cfg.spread_abs_max_cents}c & <={cfg.spread_reject_max_bp}bp",
            (
                f"delta {selection.delta:.4f} ({selection.iv_source} IV "
                f"{selection.iv:.2%}), spread {selection.spread_cents}c"
                if selection
                else "; ".join(veto) or "no candidates"
            ),
        )
    )

    approved = all(c.passed for c in checks)
    return EntryDecision(
        session_date=view.session_date,
        now_et=now_et.strftime("%Y-%m-%d %H:%M ET"),
        symbol=selection.quote.underlying if selection else (view.meta.get("symbol", "?")),
        checks=tuple(checks),
        approved=approved,
        selection=selection,
        veto_reasons=tuple(veto),
    )
