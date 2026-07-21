"""Two-layer backtest engine (ADR-003).

LAYER 1 — signals on REAL underlying bars: fully honest, verifiable. The
checklist runs on historical bars exactly as it would live (the MarketView
firewall applies unchanged).

LAYER 2 — options P&L is MODELED: a synthetic ATM chain is priced with
Black-Scholes at a stated IV, entries fill at the NEXT bar's open (never the
deciding bar's close — finding LA-close-fill-same-bar), and intrabar exits use
a CONSERVATIVE path assumption, documented here:

    Within a bar, the STOP is evaluated at the premium implied by the bar's
    LOW before the TARGET at the bar's HIGH (long-call premium is monotone in
    the underlying). When both could have triggered, the loss wins. This
    overstates nothing (finding LA-intrabar-path-ordering).

Every result object carries modeled=True and its assumptions. Sharpe is NOT
reported below 30 sessions (finding BT-sharpe-annualization); profit factor
degenerates to None when there are no losses (finding BT-profit-factor).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from qts_core.clock import session_close_et
from qts_core.config import StrategyConfig
from qts_core.models import Bar, MarketView, OptionQuote
from qts_core.money import BP, CONTRACT_MULTIPLIER
from qts_core.pricing import bs_price
from qts_core.risk import Phase, evaluate, open_position, size_entry
from qts_core.signals.checklist import evaluate_entry


@dataclass(frozen=True, slots=True)
class SessionData:
    """One historical session: date, its 5m bars, and the prior sessions
    available at its open (for RVOL/indicator warm-up)."""

    session_date: dt.date
    session_open_et: dt.datetime
    force_flat_at: dt.datetime
    bars: tuple[Bar, ...]
    prior_sessions: tuple[tuple[Bar, ...], ...]
    symbol: str
    atm_iv: float  # entry IV assumption for the MODELED premium path


@dataclass(frozen=True, slots=True)
class TradeRecord:
    session_date: dt.date
    occ_symbol: str
    contracts: int
    entry_quote_cents: int
    cost_basis_per_contract_cents: int
    exits: tuple[tuple[str, int, int], ...]  # (reason, contracts, premium_cents)
    realized_pnl_cents: int
    modeled: bool = True


@dataclass(frozen=True, slots=True)
class BacktestResult:
    modeled: bool
    assumptions: dict[str, str]
    sessions: int
    signal_count: int
    trades: tuple[TradeRecord, ...]
    net_pnl_cents: int
    win_rate: float | None
    profit_factor: float | None  # None when degenerate (no losses)
    max_drawdown_cents: int
    sharpe: float | None  # None below 30 sessions — honesty gate
    notes: tuple[str, ...] = field(default=())


def _t_years(now: dt.datetime, session_date: dt.date) -> float:
    """Years to the ACTUAL close (half days end 13:00) — see finding F1."""
    close = session_close_et(session_date)
    return max((close - now).total_seconds(), 60.0) / (365.0 * 24.0 * 3600.0)


def _premium_cents(
    spot: float, strike: float, now: dt.datetime, session_date: dt.date, iv: float, rate: float
) -> int:
    p = bs_price(spot, strike, _t_years(now, session_date), rate, iv)
    return max(round(p * 100), 1)


def _synthetic_chain(
    sess: SessionData, now: dt.datetime, spot: float, cfg: StrategyConfig
) -> tuple[OptionQuote, ...]:
    """MODELED ATM call quote: BS mid at the stated IV, 3c-wide market (the
    report's own liquidity example)."""
    grid = cfg.backtest_strike_grid_cents
    strike_cents = round(spot * 100 / grid) * grid
    mid = _premium_cents(
        spot, strike_cents / 100.0, now, sess.session_date, sess.atm_iv, cfg.risk_free_rate
    )
    half = cfg.backtest_quote_width_cents // 2
    return (
        OptionQuote(
            underlying=sess.symbol,
            expiry=sess.session_date,
            strike_cents=strike_cents,
            right="C",
            bid_cents=max(mid - half, 1),
            ask_cents=mid + (cfg.backtest_quote_width_cents - half),
            volume=1000,
            open_interest=1000,
            received_at=now,
        ),
    )


def run_session(
    sess: SessionData, cfg: StrategyConfig
) -> tuple[list[TradeRecord], int, list[tuple[dt.datetime, int]]]:
    """Backtest one session. Returns (trades, signal_count, equity_marks)."""
    trades: list[TradeRecord] = []
    signals = 0
    equity_marks: list[tuple[dt.datetime, int]] = []
    tick = cfg.tick_schedule_for(sess.symbol)
    position = None
    strike_cents = 0
    realized = 0
    trade_realized = 0
    exits: list[tuple[str, int, int]] = []

    for i, bar in enumerate(sess.bars):
        now = bar.ts_close
        if position is not None:
            # --- MODELED premium path, conservative order: low first ------
            strike = strike_cents / 100.0
            marks = (
                _premium_cents(
                    bar.low, strike, now, sess.session_date, sess.atm_iv, cfg.risk_free_rate
                ),
                _premium_cents(
                    bar.high, strike, now, sess.session_date, sess.atm_iv, cfg.risk_free_rate
                ),
                _premium_cents(
                    bar.close, strike, now, sess.session_date, sess.atm_iv, cfg.risk_free_rate
                ),
            )
            phase_before = position.phase
            for mark in marks:
                position, orders = evaluate(position, mark, now, sess.force_flat_at, cfg, tick)
                for o in orders:
                    # BT-gap-through-level-fill: when the bar gapped THROUGH the
                    # trigger, the fill is the gapped price, not the level we
                    # wished for. Selling always takes the worse of the two.
                    fill_px = min(o.trigger_premium_cents, mark)
                    pnl = (
                        fill_px * CONTRACT_MULTIPLIER * o.contracts
                        - position.cost_basis_per_contract_cents * o.contracts
                        - cfg.commission_per_contract_cents * o.contracts
                    )
                    realized += pnl
                    trade_realized += pnl
                    exits.append((o.reason.value, o.contracts, fill_px))
                if position.phase is Phase.CLOSED:
                    trades.append(
                        TradeRecord(
                            session_date=sess.session_date,
                            occ_symbol=position.occ_symbol,
                            contracts=sum(c for _, c, _ in exits),
                            entry_quote_cents=position.entry_quote_cents,
                            cost_basis_per_contract_cents=(position.cost_basis_per_contract_cents),
                            exits=tuple(exits),
                            realized_pnl_cents=trade_realized,
                        )
                    )
                    position = None
                    exits = []
                    trade_realized = 0
                    break
                if position.phase is not phase_before:
                    # BT-samebar-trail-peak-lookahead: a phase change mid-bar
                    # (e.g. TRANCHE1 at the bar HIGH) must NOT be followed by
                    # judging the runner against another mark from the SAME
                    # bar — that assumes knowledge of the intrabar path. The
                    # runner is evaluated from the next bar onward.
                    break
            equity_marks.append((now, realized))
            continue

        # --- flat: look for an entry on this COMPLETED bar ---------------
        if i + 1 >= len(sess.bars):
            break  # no next bar to fill at — decision would be unfillable
        chain = _synthetic_chain(sess, now, bar.close, cfg)
        view = MarketView(
            now=now,
            session_date=sess.session_date,
            bars=tuple(sess.bars[: i + 1]),
            prior_sessions=sess.prior_sessions,
            chain=chain,
            underlying_last=bar.close,
            meta={"symbol": sess.symbol},
        )
        decision = evaluate_entry(view, cfg, sess.session_open_et)
        if not decision.approved or decision.selection is None:
            equity_marks.append((now, realized))
            continue
        signals += 1
        sel = decision.selection
        sized = size_entry(cfg, sel.quote.ask_cents, tick)
        if sized.contracts == 0:
            equity_marks.append((now, realized))
            continue
        # Fill at NEXT bar open + slippage (never this bar's close).
        next_bar = sess.bars[i + 1]
        strike_cents = sel.quote.strike_cents
        fill_at = next_bar.ts_close - dt.timedelta(minutes=cfg.bar_interval_min)
        fill_premium = _premium_cents(
            next_bar.open,
            strike_cents / 100.0,
            fill_at,  # the bar's OPEN instant — not its close (extra theta)
            sess.session_date,
            sess.atm_iv,
            cfg.risk_free_rate,
        )
        slipped = -(-fill_premium * (BP + cfg.sizing_slippage_buffer_bp) // BP)
        position = open_position(
            symbol=sess.symbol,
            occ_symbol=sel.quote.occ_symbol,
            contracts=sized.contracts,
            entry_quote_cents=fill_premium,
            fill_cost_per_contract_cents=slipped * CONTRACT_MULTIPLIER
            + cfg.commission_per_contract_cents,
        )
        realized -= 0  # cash accounting happens on exits; entry is basis
        equity_marks.append((now, realized))

    # Session end with a position still open should not happen (force-flat),
    # but if bars ran out first, flatten at the last close mark — and say so.
    if position is not None and sess.bars:
        last = sess.bars[-1]
        mark = _premium_cents(
            last.close,
            strike_cents / 100.0,
            last.ts_close,
            sess.session_date,
            sess.atm_iv,
            cfg.risk_free_rate,
        )
        pnl = (
            mark * CONTRACT_MULTIPLIER * position.contracts
            - position.cost_basis_per_contract_cents * position.contracts
            - cfg.commission_per_contract_cents * position.contracts
        )
        realized += pnl
        trade_realized += pnl
        exits.append(("END_OF_DATA", position.contracts, mark))
        trades.append(
            TradeRecord(
                session_date=sess.session_date,
                occ_symbol=position.occ_symbol,
                contracts=sum(c for _, c, _ in exits),
                entry_quote_cents=position.entry_quote_cents,
                cost_basis_per_contract_cents=position.cost_basis_per_contract_cents,
                exits=tuple(exits),
                # BT-eod-tranche-pnl-dropped: this trade's EARLIER legs (e.g. a
                # TRANCHE1 sale) belong to it too; recording only the final leg
                # silently shrank net P&L.
                realized_pnl_cents=trade_realized,
            )
        )
    return trades, signals, equity_marks


def run_backtest(sessions: list[SessionData], cfg: StrategyConfig) -> BacktestResult:
    all_trades: list[TradeRecord] = []
    signal_count = 0
    equity = 0
    peak = 0
    max_dd = 0
    session_pnls: list[int] = []
    notes: list[str] = []

    for sess in sessions:
        trades, signals, marks = run_session(sess, cfg)
        all_trades.extend(trades)
        signal_count += signals
        session_pnl = sum(t.realized_pnl_cents for t in trades)
        session_pnls.append(session_pnl)
        for _, realized in marks:
            cur = equity + realized
            peak = max(peak, cur)
            max_dd = max(max_dd, peak - cur)
        equity += session_pnl

    wins = [t for t in all_trades if t.realized_pnl_cents > 0]
    losses = [t for t in all_trades if t.realized_pnl_cents < 0]
    win_rate = len(wins) / len(all_trades) if all_trades else None
    gross_win = sum(t.realized_pnl_cents for t in wins)
    gross_loss = -sum(t.realized_pnl_cents for t in losses)
    if gross_loss > 0:
        profit_factor: float | None = gross_win / gross_loss
    else:
        profit_factor = None
        if all_trades:
            notes.append("profit_factor undefined: zero losing trades in sample")

    sharpe: float | None = None
    if len(session_pnls) >= 30:
        mean = sum(session_pnls) / len(session_pnls)
        var = sum((x - mean) ** 2 for x in session_pnls) / (len(session_pnls) - 1)
        sd = var**0.5
        if sd > 0:
            sharpe = (mean / sd) * (252**0.5)
    else:
        notes.append(
            f"sharpe not reported: {len(session_pnls)} sessions < 30 minimum "
            "(intraday annualization would be meaningless)"
        )

    return BacktestResult(
        modeled=True,
        assumptions={
            "options_premiums": "Black-Scholes from underlying bars at stated ATM IV "
            "(NOT real option prices — B2 open, ADR-007)",
            "entry_fill": f"next bar open +{cfg.sizing_slippage_buffer_bp}bp slippage, tick-legal",
            "intrabar_path": "conservative: stop at bar low evaluated before target at bar high",
            "commission": f"{cfg.commission_per_contract_cents}c/contract/side",
            "max_drawdown": "computed on REALIZED equity only; open-position "
            "mark-to-market excursions are not included (understates intraday DD)",
        },
        sessions=len(sessions),
        signal_count=signal_count,
        trades=tuple(all_trades),
        net_pnl_cents=sum(t.realized_pnl_cents for t in all_trades),
        win_rate=win_rate,
        profit_factor=profit_factor,
        max_drawdown_cents=max_dd,
        sharpe=sharpe,
        notes=tuple(notes),
    )
