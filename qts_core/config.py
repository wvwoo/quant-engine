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
    rvol_mode: str = "per_bar"  # "per_bar" | "cumulative"
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
    max_bar_age_min: int = 25
    staleness_gate_enabled: bool = False
    backtest_artifacts: bool = False
    broker_backend: str = "model"  # "model" | "alpaca_paper" (ADR-012)

    # --- provider / data shaping (were literals in the provider) ----------
    bars_fetch_days: int = 30
    max_strikes_around_atm: int = 12
    min_request_interval_s: float = 1.0
    max_intraday_days: int = 59

    # --- backtest MODELED chain (were literals in backtest.py) ------------
    backtest_strike_grid_cents: int = 500
    backtest_quote_width_cents: int = 3
    backtest_atm_iv: float = 0.20

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
        """Tick schedule for a symbol — KNOWN symbols only; unknown ones raise.

        The old behaviour silently guessed NICKEL and claimed the guess "only
        costs granularity". Measured falsification (N-08): NICKEL's tick is 5c
        below $3, and round_to_tick(..., "floor") sends any sell-side price of
        1-4 cents to ZERO — so a misclassified penny-program symbol (AAPL is
        one) books a fabricated 100% loss at force-flat and journals a $0.00
        fill for a leg that filled. In a system whose product is honest
        numbers, a wrong guess that corrupts money is strictly worse than a
        loud refusal (the central rule: no unstated guessing).
        """
        try:
            return self.tick_schedules[symbol.upper()]
        except KeyError:
            raise KeyError(
                f"no tick schedule for {symbol.upper()!r}: add it to "
                "StrategyConfig.tick_schedules (with provenance) before trading "
                "it — guessing a schedule can floor sell prices to $0.00"
            ) from None


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
    "rvol_mode": (
        Tier.ASSUMPTION,
        "report gives no RVOL denominator (finding rvol-denominator-undefined). Resolved by "
        "MEASUREMENT over 577 in-window bars / 29 real SPY sessions: the cumulative "
        "construction peaks at 1.74 so the 2.0 gate is unreachable (0/577), while per-bar "
        "reaches 5.01 with 3.8% of bars >= 2.0 — and only per-bar can produce the 240% the "
        "report itself cites. No threshold was tuned.",
    ),
    "rvol_lookback_days": (
        Tier.ASSUMPTION,
        "report says '20-day Moving Average' with no window definition. N prior SESSIONS "
        "are averaged (see rvol_mode for the numerator/denominator construction).",
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
    "broker_backend": (
        Tier.SAFETY,
        '"model" = the in-process modeled PaperBroker (default). "alpaca_paper" = '
        "real order infrastructure at Alpaca's PAPER venue (ADR-012): zero real money, "
        "owner-held keys via APCA_API_KEY_ID/APCA_API_SECRET_KEY, base URL hardcoded to "
        "paper-api.alpaca.markets and NOT configurable. One db = one backend, enforced "
        "by StateStore.assert_backend. There is still no live-trading path in this "
        "codebase; require_paper_mode is untouched.",
    ),
    "backtest_artifacts": (
        Tier.SAFETY,
        "OFF. A stdout backtest dies with the terminal; a FILE outlives its caveats — it "
        "can be forwarded or quoted back as if it were a track record. When enabled, every "
        "artifact carries the MODELED tag and the open-B2 note in the header, per field, "
        "and in the JSON body.",
    ),
    "max_bar_age_min": (
        Tier.SAFETY,
        "ABSENT from spec. Maximum age of the NEWEST bar before an ENTRY is refused. "
        "25 min = the free tier's documented ~15 min delay + one 5 min bar + operating "
        "headroom, so a normal delayed feed passes and a FROZEN one does not. Measured "
        "against bar ts_close, never OptionQuote.received_at — received_at is the fetch "
        "stamp, so a gate built on it would look like a guard and measure nothing. "
        "ENTRIES only: refusing to act on stale data must never strand an open position.",
    ),
    "staleness_gate_enabled": (
        Tier.SAFETY,
        "OFF until the age distribution has been measured on a real session. While OFF "
        "the age is still computed and reported (StepResult.data_age_min) so the "
        "threshold can be chosen from data rather than guessed.",
    ),
    "bars_fetch_days": (
        Tier.ASSUMPTION,
        "calendar days of intraday history requested per call. Was a literal days=10 in the "
        "provider, which can supply at most ~9 prior sessions — while every decision blob "
        "advertised a 20-day RVOL baseline (finding G-05). 30 calendar days ~ 20 sessions, "
        "so the label and the data now agree. Does NOT change rvol_min.",
    ),
    "max_strikes_around_atm": (
        Tier.ASSUMPTION,
        "strikes kept either side of spot when converting a chain. NOTE: a held contract "
        "can drift OUT of this window as the underlying moves, which is one way the exit "
        "path loses its mark — see the FORCE_FLAT_UNMARKED rail.",
    ),
    "min_request_interval_s": (
        Tier.ASSUMPTION,
        "client-side spacing between provider calls. Yahoo throttles per client, so the "
        "limiter is shared per process, not per symbol. Overridable via "
        "QTS_MIN_REQUEST_INTERVAL_S for slow links.",
    ),
    "max_intraday_days": (
        Tier.ASSUMPTION,
        "yfinance caps 5m history at ~60 days; requests are clamped to this.",
    ),
    "backtest_strike_grid_cents": (
        Tier.ASSUMPTION,
        "MODELED chain snaps to a $5 strike grid. This is a BACKTEST ARTIFACT and a named "
        "cause of the 79.1% contract_gates failure rate: real SPY lists $1 strikes, so the "
        "synthetic ATM strike can sit up to $2.50 from spot and miss the 0.45-0.55 delta "
        "band far more often than a real chain would. Configurable so the artifact can be "
        "measured rather than argued about.",
    ),
    "backtest_quote_width_cents": (
        Tier.ASSUMPTION,
        "MODELED bid/ask width around the BS mid (the report's own liquidity example).",
    ),
    "backtest_atm_iv": (
        Tier.ASSUMPTION,
        "flat ATM IV for the MODELED premium path. B2 is open (no real historical option "
        "premiums), so every number derived from this is MODELED by construction.",
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
