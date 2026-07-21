"""Checklist tests: a fully-passing scenario, then table-driven single-filter
knockouts, plus the truncation (look-ahead regression) property."""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from qts_core.clock import NY
from qts_core.config import StrategyConfig
from qts_core.models import Bar, MarketView, OptionQuote
from qts_core.pricing import bs_price
from qts_core.signals.checklist import EntryDecision, evaluate_entry, select_contract

SESSION = dt.date(2026, 6, 17)
OPEN = dt.datetime(2026, 6, 17, 9, 30, tzinfo=NY)
CFG = StrategyConfig()


def et(hh: int, mm: int, day: int = 17) -> dt.datetime:
    return dt.datetime(2026, 6, day, hh, mm, tzinfo=NY)


def _session_bars(now: dt.datetime, *, breakout: bool = True, volume: int = 3000) -> list[Bar]:
    """5m bars 9:35..now, deterministic. ORB high exactly 204.10; after the
    range, MIXED steps (net up) so RSI stays in the 50-70 zone while the close
    still clears the ORB high at 10:15. No bar is ever retouched — slicing a
    longer session at t reproduces exactly the bars of a session built to t."""
    orb_steps = (0.25, -0.10, 0.15)  # closes 203.75, 203.65, 203.80
    post_steps = (0.30, -0.18, 0.24, -0.12)  # net +0.06/bar with real losses
    bars: list[Bar] = []
    t = et(9, 35)
    price = 203.5
    i = 0
    while t <= now:
        in_orb = t <= OPEN + dt.timedelta(minutes=CFG.orb_minutes)
        if in_orb:
            step = orb_steps[i % 3]
            h = 204.10 if i == 0 else price + abs(step) + 0.20
        else:
            step = post_steps[i % 4] if breakout else -0.05
            h = price + abs(step) + 0.10
        c = price + step
        lo = min(price, c) - 0.15
        bars.append(
            Bar(ts_close=t, open=price, high=max(h, price, c), low=lo, close=c, volume=volume)
        )
        price = c
        t += dt.timedelta(minutes=5)
        i += 1
    return bars


def _prior_session(day: dt.date, *, volume: int = 1000) -> list[Bar]:
    """A full prior session of 5m bars with mixed-but-upward drift (RSI 50-70)."""
    bars: list[Bar] = []
    t = dt.datetime.combine(day, dt.time(9, 35), tzinfo=NY)
    close_t = dt.datetime.combine(day, dt.time(16, 0), tzinfo=NY)
    price = 200.0
    steps = (0.30, -0.20, 0.20, -0.15)  # net +0.15 per cycle, real losses present
    i = 0
    while t <= close_t:
        step = steps[i % 4]
        bars.append(
            Bar(ts_close=t, open=price, high=price + abs(step) + 0.2,
                low=price - abs(step) - 0.2, close=price + step, volume=volume)
        )
        price += step
        t += dt.timedelta(minutes=5)
        i += 1
    return bars


def _prior_dates(n: int) -> list[dt.date]:
    """The n weekdays strictly before SESSION, ascending (month-safe)."""
    out: list[dt.date] = []
    d = SESSION - dt.timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return list(reversed(out))


def _chain(now: dt.datetime, *, spot: float = 204.79) -> tuple[OptionQuote, ...]:
    """Same-day ATM call whose mid prices to IV=0.68 (delta in band), plus a
    decoy far-OTM strike that must be rejected by the delta band."""
    t_years = (et(16, 0) - now).total_seconds() / (365 * 24 * 3600)
    atm_mid = bs_price(spot, 205.0, t_years, CFG.risk_free_rate, 0.68)
    mid_c = round(atm_mid * 100)
    atm = OptionQuote(
        underlying="NVDA", expiry=SESSION, strike_cents=20500, right="C",
        bid_cents=mid_c - 1, ask_cents=mid_c + 2, volume=1200, open_interest=5000,
        received_at=now,
    )
    otm_mid = bs_price(spot, 215.0, t_years, CFG.risk_free_rate, 0.68)
    otm = OptionQuote(
        underlying="NVDA", expiry=SESSION, strike_cents=21500, right="C",
        bid_cents=max(round(otm_mid * 100) - 1, 1), ask_cents=round(otm_mid * 100) + 2,
        volume=300, open_interest=900, received_at=now,
    )
    return (atm, otm)


def make_view(
    now: dt.datetime | None = None,
    *,
    breakout: bool = True,
    volume: int = 3000,
    chain: tuple[OptionQuote, ...] | None = None,
    priors: int = 20,
) -> MarketView:
    now = now or et(10, 15)
    return MarketView(
        now=now,
        session_date=SESSION,
        bars=tuple(_session_bars(now, breakout=breakout, volume=volume)),
        prior_sessions=tuple(tuple(_prior_session(d)) for d in _prior_dates(priors)),
        chain=chain if chain is not None else _chain(now),
        underlying_last=204.79,
        meta={"symbol": "NVDA"},
    )


def check(decision: EntryDecision, name: str) -> bool:
    return next(c for c in decision.checks if c.name == name).passed


class TestFullPass:
    def test_all_seven_checks_pass(self) -> None:
        d = evaluate_entry(make_view(), CFG, OPEN)
        assert [(c.name, c.passed) for c in d.checks] == [
            ("trading_window", True),
            ("orb_breakout", True),
            ("vwap_alignment", True),
            ("rvol", True),
            ("rsi_non_overbought", True),
            ("macd_histogram", True),
            ("contract_gates", True),
        ]
        assert d.approved
        assert d.selection is not None
        assert d.selection.quote.strike_cents == 20500  # ATM chosen, decoy rejected
        assert 0.45 <= d.selection.delta <= 0.55
        assert d.selection.iv_source == "computed"

    def test_decision_is_deterministic(self) -> None:
        a = evaluate_entry(make_view(), CFG, OPEN)
        b = evaluate_entry(make_view(), CFG, OPEN)
        assert a == b


class TestKnockouts:
    """Each scenario breaks exactly one input; exactly that check must fail."""

    def test_outside_window(self) -> None:
        d = evaluate_entry(make_view(et(11, 45)), CFG, OPEN)
        assert not check(d, "trading_window")
        assert not d.approved

    def test_before_window_start_orb_period(self) -> None:
        d = evaluate_entry(make_view(et(9, 45)), CFG, OPEN)
        assert not check(d, "trading_window")

    def test_no_breakout(self) -> None:
        d = evaluate_entry(make_view(breakout=False), CFG, OPEN)
        assert not check(d, "orb_breakout")
        assert not d.approved

    def test_low_volume_fails_rvol(self) -> None:
        d = evaluate_entry(make_view(volume=400), CFG, OPEN)  # 400 vs prior 1000/bar
        assert not check(d, "rvol")
        assert not d.approved

    def test_no_priors_fails_rvol_with_reason(self) -> None:
        d = evaluate_entry(make_view(priors=0), CFG, OPEN)
        rv = next(c for c in d.checks if c.name == "rvol")
        assert not rv.passed
        assert "baseline" in rv.reason

    def test_empty_chain_fails_contract_gates(self) -> None:
        d = evaluate_entry(make_view(chain=()), CFG, OPEN)
        gate = next(c for c in d.checks if c.name == "contract_gates")
        assert not gate.passed
        assert not d.approved

    def test_wide_spread_rejected(self) -> None:
        now = et(10, 15)
        base = _chain(now)[0]
        wide = dataclasses.replace(base, bid_cents=base.mid_cents - 40, ask_cents=base.mid_cents + 40)  # type: ignore[operator]
        d = evaluate_entry(make_view(chain=(wide,)), CFG, OPEN)
        assert not check(d, "contract_gates")
        assert any("spread" in r for r in d.veto_reasons)

    def test_dead_quote_rejected_not_perfect(self) -> None:
        now = et(10, 15)
        dead = dataclasses.replace(_chain(now)[0], bid_cents=0, ask_cents=0)
        d = evaluate_entry(make_view(chain=(dead,)), CFG, OPEN)
        assert not check(d, "contract_gates")
        assert any("no market" in r for r in d.veto_reasons)

    def test_next_day_expiry_is_not_0dte(self) -> None:
        # B4: availability is checked, never assumed. A Tuesday NVDA chain
        # with only a Wednesday expiry must FAIL with the 0DTE reason.
        now = et(10, 15)
        tomorrow = dataclasses.replace(_chain(now)[0], expiry=dt.date(2026, 6, 18))
        d = evaluate_entry(make_view(chain=(tomorrow,)), CFG, OPEN)
        gate = next(c for c in d.checks if c.name == "contract_gates")
        assert not gate.passed
        assert "no same-day (0DTE)" in gate.reason


class TestTruncationProperty:
    """freqtrade-style look-ahead regression: the decision at time t computed
    from data-through-t must be IDENTICAL to the decision at time t inside a
    longer session. If an indicator peeked forward, these would differ."""

    def test_decision_stable_under_future_extension(self) -> None:
        t = et(10, 15)
        truncated = evaluate_entry(make_view(t), CFG, OPEN)
        # Build the longer session (through 11:00), then slice back to <= t.
        full = _session_bars(et(11, 0))
        sliced = tuple(b for b in full if b.ts_close <= t)
        view_sliced = MarketView(
            now=t,
            session_date=SESSION,
            bars=sliced,
            prior_sessions=tuple(tuple(_prior_session(d)) for d in _prior_dates(20)),
            chain=_chain(t),
            underlying_last=204.79,
            meta={"symbol": "NVDA"},
        )
        again = evaluate_entry(view_sliced, CFG, OPEN)
        assert truncated.checks == again.checks
        assert truncated.approved == again.approved


class TestSelectContract:
    def test_reasons_are_explanations_not_silence(self) -> None:
        view = make_view(chain=())
        sel, reasons = select_contract(view, CFG)
        assert sel is None
        assert reasons  # every rejection is a sentence, never a swallow

    def test_iv_ceiling_enforced(self) -> None:
        tight = dataclasses.replace(CFG, iv_max=0.10)
        sel, reasons = select_contract(make_view(), tight)
        assert sel is None
        assert any("IV" in r for r in reasons)
