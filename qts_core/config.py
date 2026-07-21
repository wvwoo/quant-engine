"""Single strategy config with per-field provenance.

Every parameter carries a tier visible in this file (plan Phase 3):

  SPEC       — stated verbatim in institutional_quant_trading_report.md
  DERIVED    — computed from SPEC values (derivation cited)
  ASSUMPTION — the spec is silent/ambiguous; an explicit, flagged choice
  SAFETY     — absent from the spec entirely; added because its absence is a
               safety defect (ADR-008), never silently

No magic numbers may live outside this file. A test asserts every field has a
provenance entry.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field, fields
from enum import Enum

from qts_core.money import TickSchedule


class Tier(Enum):
    SPEC = "SPEC"
    DERIVED = "DERIVED"
    ASSUMPTION = "ASSUMPTION"
    SAFETY = "SAFETY"


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    # --- capital & sizing -------------------------------------------------
    sub_portfolio_cents: int = 85_000  # $850.00
    sizing_slippage_buffer_bp: int = 100  # 1.00% (ADR-005: sizing quantity)
    commission_per_contract_cents: int = 65

    # --- entry window & opening range ------------------------------------
    entry_window_start: dt.time = dt.time(9, 46)
    entry_window_end: dt.time = dt.time(11, 30)
    orb_minutes: int = 15

    # --- contract gates ---------------------------------------------------
    delta_min: float = 0.45
    delta_max: float = 0.55
    iv_max: float = 0.95
    spread_abs_max_cents: int = 5
    spread_reject_max_bp: int = 350  # 3.5% (ADR-005: reject quantity)

    # --- momentum filters -------------------------------------------------
    rvol_min: float = 2.0
    rvol_lookback_days: int = 20
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bar_interval_min: int = 5

    # --- exits ------------------------------------------------------------
    stop_loss_bp: int = -2_500  # -25% of entry premium
    tranche1_gain_bp: int = 10_000  # +100%
    tranche1_contracts: int = 1
    runner_floor_bp: int = 1_000  # breakeven +10%
    trailing_bp: int = 1_200  # 12% from peak

    # --- safety (ADR-008: added, not derived) -----------------------------
    force_flat_et: dt.time = dt.time(15, 30)
    force_flat_close_buffer_min: int = 30
    daily_loss_limit_cents: int = 21_250  # 25% of sub-portfolio
    live_trading: bool = False

    # --- pricing model ----------------------------------------------------
    risk_free_rate: float = 0.045

    # --- universe ---------------------------------------------------------
    tickers: tuple[str, ...] = ("SPY", "QQQ", "NVDA")
    tick_schedules: dict[str, TickSchedule] = field(
        default_factory=lambda: {
            "SPY": TickSchedule.FULL_PENNY,
            "QQQ": TickSchedule.FULL_PENNY,
            "NVDA": TickSchedule.PENNY_PROGRAM,
        }
    )

    def tick_schedule_for(self, symbol: str) -> TickSchedule:
        # Unknown symbols get the coarsest schedule — misclassifying a nickel
        # class as penny produces unfillable prices; the reverse only costs
        # granularity. Fail conservative.
        return self.tick_schedules.get(symbol.upper(), TickSchedule.NICKEL)


PROVENANCE: dict[str, tuple[Tier, str]] = {
    "sub_portfolio_cents": (Tier.SPEC, "report: Concentrated Sub-Portfolio Base = $850.00"),
    "sizing_slippage_buffer_bp": (
        Tier.SPEC,
        "report: C_total = Ask x 1.01; ADR-005 fixes this as the SIZING quantity. "
        "WARNING: 2-contract sizing survives on $0.0079 — see knife-edge test.",
    ),
    "commission_per_contract_cents": (
        Tier.ASSUMPTION,
        "report has NO fee model (finding no-fee-model). $0.65/contract/side is a "
        "typical retail rate. Golden fixtures set 0 to reproduce report arithmetic.",
    ),
    "entry_window_start": (Tier.SPEC, "report: Trading Window 09:46 AM EST"),
    "entry_window_end": (Tier.SPEC, "report: Trading Window 11:30 AM EST"),
    "orb_minutes": (Tier.SPEC, "report: '15m Opening Range Breakout (ORB) High'"),
    "delta_min": (Tier.SPEC, "report: 0.45 <= delta"),
    "delta_max": (Tier.SPEC, "report: delta <= 0.55"),
    "iv_max": (Tier.SPEC, "report: IV < 95%"),
    "spread_abs_max_cents": (Tier.SPEC, "report: Bid/Ask Spread <= $0.05"),
    "spread_reject_max_bp": (
        Tier.SPEC,
        "report: Slippage Spread Risk <= 3.5%; ADR-005 fixes this as the REJECT gate. "
        "The report's 2.38% 'current value' has no defined denominator (finding "
        "slippage-triple-conflict) — we define spread_bp_of_mid.",
    ),
    "rvol_min": (Tier.SPEC, "report: RVOL > 2.0"),
    "rvol_lookback_days": (
        Tier.ASSUMPTION,
        "report says '20-day Moving Average' with no denominator definition (finding "
        "rvol-denominator-undefined). Defined here as: session cumulative volume vs the "
        "mean same-elapsed-time cumulative volume over the prior N sessions.",
    ),
    "rsi_period": (
        Tier.ASSUMPTION,
        "report: 'RSI (5m): 58.5' names the bar interval but no period. Wilder-14 chosen "
        "(finding rsi-no-threshold).",
    ),
    "rsi_overbought": (
        Tier.ASSUMPTION,
        "report: 'Non-Overbought Momentum' with no numeric gate. Classic 70 chosen; "
        "pass condition is rsi < 70 (finding rsi-no-threshold).",
    ),
    "macd_fast": (Tier.ASSUMPTION, "report gives no MACD periods; classic 12/26/9 chosen."),
    "macd_slow": (Tier.ASSUMPTION, "classic 12/26/9 (finding macd-underspecified)."),
    "macd_signal": (Tier.ASSUMPTION, "classic 12/26/9 (finding macd-underspecified)."),
    "bar_interval_min": (Tier.SPEC, "report: 'RSI (5m)' — 5-minute bars"),
    "stop_loss_bp": (
        Tier.SPEC,
        "report: hard exit at -25% of entry premium ($4.20 -> $3.15). NOTE (ADR-004): "
        "realized loss on slipped basis is exactly 26/101 = 25.74%, not 25%.",
    ),
    "tranche1_gain_bp": (Tier.SPEC, "report: Tranche 1 at +100% ($8.40)"),
    "tranche1_contracts": (Tier.SPEC, "report: liquidate 1 contract at Tranche 1"),
    "runner_floor_bp": (
        Tier.SPEC,
        "report: runner stop $4.62 = breakeven +10% of QUOTED entry. NOTE: dominated by "
        "the 12% trail from the $8.40 peak (739 > 462) — dead code unless peak collapses "
        "(finding runner-stop-unreachable). Kept because SPEC states it.",
    ),
    "trailing_bp": (Tier.SPEC, "report: strict 12% dynamic trailing stop from peak"),
    "force_flat_et": (
        Tier.SAFETY,
        "ABSENT from spec (finding no-eod-flat-rule / B3): an ITM 0DTE runner would be "
        "auto-exercised into ~$20,500 of stock on an $850 account. 15:30 ET chosen.",
    ),
    "force_flat_close_buffer_min": (
        Tier.SAFETY,
        "half-days close 13:00 — force-flat must pull forward (min(configured, close-30m)).",
    ),
    "daily_loss_limit_cents": (
        Tier.SAFETY,
        "ABSENT from spec. 25% of sub-portfolio ($212.50). Stops the re-entry loop a "
        "single -25% stop would otherwise allow all day.",
    ),
    "live_trading": (
        Tier.SAFETY,
        "OFF. Real-money trading is an owner-exclusive act; see require_paper_mode().",
    ),
    "risk_free_rate": (
        Tier.ASSUMPTION,
        "BS rate for IV/delta. 4.5% ~ mid-2026 short T-bill; intraday 0DTE delta is "
        "almost insensitive to it (T ~ 2e-4 years).",
    ),
    "tickers": (
        Tier.ASSUMPTION,
        "scanner universe intersected with 0DTE reality (finding B4, measured live "
        "2026-07-21): SPY/QQQ list dailies; NVDA lists Mon/Wed/Fri only. The engine "
        "NEVER assumes availability — it checks the chain for a same-day expiry.",
    ),
    "tick_schedules": (
        Tier.ASSUMPTION,
        "OCC penny program: SPY/QQQ full-penny; NVDA penny-program (finding "
        "MONEY-tick-rounding: $4.62 is not a legal NVDA price above $3).",
    ),
}


class LiveTradingBlocked(RuntimeError):
    pass


def require_paper_mode(cfg: StrategyConfig) -> None:
    """Boot gate: refuse to start if anything asks for live trading.

    Flipping this requires the OWNER to (1) set cfg.live_trading=True, (2) set
    env QTS_LIVE_TRADING_OWNER_ACK to the exact Arabic sentence below, and even
    then the runtime refuses — there is no live broker in this codebase.
    """
    ack = os.environ.get("QTS_LIVE_TRADING_OWNER_ACK", "")
    expected = "أنا المالك وأتحمل مسؤولية التداول الحقيقي"
    if cfg.live_trading:
        if ack != expected:
            raise LiveTradingBlocked(
                "live_trading=True but owner acknowledgement missing. "
                "Paper trading is the only supported mode."
            )
        raise LiveTradingBlocked(
            "Owner acknowledgement present, but NO live broker exists in this "
            "codebase by design. Live trading remains an owner-exclusive act "
            "outside this system."
        )


def provenance_complete() -> list[str]:
    """Field names missing a provenance entry (tested to be empty)."""
    declared = {f.name for f in fields(StrategyConfig)}
    return sorted(declared - set(PROVENANCE))
