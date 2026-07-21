"""Pure technical indicators. No I/O, no clock, no config — values in, value out.

Every function returns ``None`` when it cannot be computed honestly (not
enough data, undefined denominator). The checklist treats None as FAIL with a
reason — silence is never a pass.

Look-ahead constructions (findings LA-*):
- vwap: cumulative over the session SO FAR — never the full day.
- orb_high: only bars strictly inside the opening range, and only once the
  range is COMPLETE.
- rvol: today's cumulative volume at elapsed-t vs the mean cumulative volume
  at the SAME elapsed-t over prior sessions — never vs completed-day totals.
- rsi/macd: computed over completed bars only (the MarketView firewall already
  guarantees no future bars exist).
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Sequence

from qts_core.clock import to_et
from qts_core.models import Bar


def vwap(bars: Sequence[Bar]) -> float | None:
    """Session VWAP over completed bars so far (typical price x volume)."""
    num = 0.0
    den = 0
    for b in bars:
        tp = (b.high + b.low + b.close) / 3.0
        num += tp * b.volume
        den += b.volume
    if den == 0:
        return None
    return num / den


def orb_high(
    bars: Sequence[Bar], session_open_et: dt.datetime, orb_minutes: int, now: dt.datetime
) -> float | None:
    """High of the opening range. None until the range is COMPLETE.

    A bar belongs to the range iff its close time is <= open + orb_minutes.
    Requiring now > range-end prevents acting on a half-formed range
    (finding LA-orb-window-boundary).
    """
    range_end = session_open_et + dt.timedelta(minutes=orb_minutes)
    if now < range_end:
        return None
    highs = [b.high for b in bars if b.ts_close <= range_end]
    if not highs:
        return None
    return max(highs)


def cumulative_volume(bars: Sequence[Bar]) -> int:
    return sum(b.volume for b in bars)


def rvol(
    session_bars: Sequence[Bar],
    prior_sessions: Sequence[Sequence[Bar]],
    now: dt.datetime,
    lookback: int | None = None,
    mode: str = "per_bar",
) -> float | None:
    """Relative volume. Two constructions, both same-elapsed-time (no look-ahead).

    mode="per_bar" (DEFAULT, evidence-based):
        THIS bar's volume vs the mean volume of the SAME clock-minute bar over
        prior sessions. Spiky — it can actually reach the report's "240%".
    mode="cumulative":
        session-to-date volume vs the mean session-to-date volume at the same
        time of day. Smooth, and therefore heavily damped.

    WHY per_bar IS THE DEFAULT — measured, not assumed. Over 577 in-window
    bars across 29 real SPY sessions (2026-06/07):
        cumulative: p50 0.88, max 1.74  ->   0/577 bars reach 2.0  (gate DEAD)
        per_bar   : p50 0.83, p99 2.93, max 5.01 -> 22/577 (3.8%) reach 2.0
    The report cites an OBSERVED 240%, which only the per-bar construction can
    produce (it sits at the ~1.9th percentile there). Choosing the definition
    that can express the spec's own datum is not curve-fitting: no threshold
    was tuned, and the 2.0 gate is unchanged. Resolves the ambiguity flagged as
    finding rvol-denominator-undefined.

    Comparing a partial session against COMPLETED-day averages would be the
    naive reading and is look-ahead-adjacent (finding LA-rvol-completed-day);
    neither mode does that.
    """
    if not prior_sessions:
        return None
    if lookback is not None:
        # cfg.rvol_lookback_days was documented but never enforced: the mean
        # ran over EVERY supplied session (finding F2).
        prior_sessions = prior_sessions[-lookback:]
    cutoff = to_et(now).timetz()
    baselines: list[int] = []
    if mode == "per_bar":
        if not session_bars:
            return None
        num = session_bars[-1].volume
        stamp = to_et(session_bars[-1].ts_close).timetz()
        for sess in prior_sessions:
            match = next((b.volume for b in sess if to_et(b.ts_close).timetz() == stamp), None)
            if match is not None:
                baselines.append(match)
    elif mode == "cumulative":
        num = cumulative_volume(session_bars)
        for sess in prior_sessions:
            baselines.append(sum(b.volume for b in sess if to_et(b.ts_close).timetz() <= cutoff))
    else:
        raise ValueError(f"unknown rvol mode: {mode!r}")
    positive = [b for b in baselines if b > 0]
    if not positive:
        return None
    return num / (sum(positive) / len(positive))


def _wilder_smooth(values: Sequence[float], period: int) -> float:
    avg = sum(values[:period]) / period
    for v in values[period:]:
        avg = (avg * (period - 1) + v) / period
    return avg


def rsi(closes: Sequence[float], period: int) -> float | None:
    """Wilder RSI. None with fewer than period+1 closes."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in itertools.pairwise(closes):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = _wilder_smooth(gains, period)
    avg_loss = _wilder_smooth(losses, period)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _ema_series(values: Sequence[float], period: int) -> list[float]:
    k = 2.0 / (period + 1.0)
    out: list[float] = []
    ema = sum(values[:period]) / period  # SMA seed
    out.extend([ema] * period)
    for v in values[period:]:
        ema = v * k + ema * (1.0 - k)
        out.append(ema)
    return out


def macd_histogram(closes: Sequence[float], fast: int, slow: int, signal: int) -> float | None:
    """MACD histogram (macd - signal). None until slow+signal closes exist."""
    if len(closes) < slow + signal:
        return None
    fast_ema = _ema_series(closes, fast)
    slow_ema = _ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(fast_ema, slow_ema, strict=True)]
    usable = macd_line[slow - 1 :]
    signal_line = _ema_series(usable, signal)
    return usable[-1] - signal_line[-1]
