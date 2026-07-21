"""Local option pricing: forward from parity -> implied vol -> delta.

ADR-002: greeks are NOT part of the provider interface. yfinance returns no
greeks at all and Yahoo's own IV is documented-unreliable, so every greek this
system acts on is computed HERE, from quoted prices, reproducibly.

Model: Black-Scholes on a forward. US equity options are American-style, so
this delta is an approximation — acceptable for a 0DTE ATM call (early
exercise premium ~0 for calls on low-dividend names intraday), and the
approximation is uniform across providers, which is the point.

Numerical honesty at T->0: delta approaches a step function around the strike,
which makes the 0.45-0.55 band knife-edged late in the day (finding
BLOCK-no-delta-source / gamma-instability). Nothing here hides that; the
checklist simply reports the computed value.

Zero third-party dependencies: normal CDF via math.erf, implied vol via
guarded bisection. Deterministic across platforms to double precision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SQRT2 = math.sqrt(2.0)
MIN_T_YEARS = 1.0 / (365.0 * 24.0 * 60.0)  # one minute — 0DTE never hits T=0 while open
MIN_SIGMA, MAX_SIGMA = 1e-4, 5.0


class PricingError(ValueError):
    pass


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def bs_price(
    spot: float, strike: float, t_years: float, rate: float, sigma: float, *, call: bool = True
) -> float:
    """Black-Scholes price (no dividend term; q folded into forward usage)."""
    if spot <= 0 or strike <= 0:
        raise PricingError(f"non-positive spot/strike: {spot}/{strike}")
    t = max(t_years, MIN_T_YEARS)
    if sigma <= 0:
        # Degenerate: discounted intrinsic on the forward.
        fwd = spot * math.exp(rate * t)
        intrinsic = max(fwd - strike, 0.0) if call else max(strike - fwd, 0.0)
        return math.exp(-rate * t) * intrinsic
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    df = math.exp(-rate * t)
    if call:
        return spot * norm_cdf(d1) - strike * df * norm_cdf(d2)
    return strike * df * norm_cdf(-d2) - spot * norm_cdf(-d1)


def bs_delta(
    spot: float, strike: float, t_years: float, rate: float, sigma: float, *, call: bool = True
) -> float:
    if spot <= 0 or strike <= 0 or sigma <= 0:
        raise PricingError(f"invalid inputs spot={spot} strike={strike} sigma={sigma}")
    t = max(t_years, MIN_T_YEARS)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    return norm_cdf(d1) if call else norm_cdf(d1) - 1.0


def implied_vol(
    option_price: float,
    spot: float,
    strike: float,
    t_years: float,
    rate: float,
    *,
    call: bool = True,
    tol: float = 1e-8,
    max_iter: int = 200,
) -> float | None:
    """Implied vol via guarded bisection. None when no vol can explain the price.

    Returns None (not an exception) for prices at/below intrinsic or above the
    spot bound — those are data-quality outcomes the checklist must see and
    report, not crash on.
    """
    if option_price <= 0:
        return None
    t = max(t_years, MIN_T_YEARS)
    lo_price = bs_price(spot, strike, t, rate, MIN_SIGMA, call=call)
    hi_price = bs_price(spot, strike, t, rate, MAX_SIGMA, call=call)
    if option_price <= lo_price or option_price >= hi_price:
        return None
    lo, hi = MIN_SIGMA, MAX_SIGMA
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        price = bs_price(spot, strike, t, rate, mid, call=call)
        if abs(price - option_price) < tol:
            return mid
        if price < option_price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def forward_from_parity(
    call_mid: float, put_mid: float, strike: float, t_years: float, rate: float
) -> float:
    """Forward from put-call parity: F = K + e^{rT}(C - P).

    Model-free (parity holds for European and near-enough ATM American
    intraday). Lets us infer the effective forward without a dividend feed.
    """
    t = max(t_years, MIN_T_YEARS)
    return strike + math.exp(rate * t) * (call_mid - put_mid)


@dataclass(frozen=True, slots=True)
class ContractAnalytics:
    """Everything the checklist needs about one contract, computed locally."""

    mid: float
    iv: float | None
    delta: float | None
    iv_source: str  # "computed" | "provider_hint" | "unavailable"


def analyze_contract(
    *,
    mid_price: float,
    spot: float,
    strike: float,
    t_years: float,
    rate: float,
    call: bool,
    iv_hint: float | None = None,
) -> ContractAnalytics:
    """IV from the quoted mid; delta from that IV. Provider IV only as fallback,
    and the source is always labeled so reports can say which one they used."""
    iv = implied_vol(mid_price, spot, strike, t_years, rate, call=call)
    source = "computed"
    if iv is None and iv_hint is not None and 0 < iv_hint < MAX_SIGMA:
        iv, source = iv_hint, "provider_hint"
    if iv is None:
        return ContractAnalytics(mid=mid_price, iv=None, delta=None, iv_source="unavailable")
    delta = bs_delta(spot, strike, t_years, rate, iv, call=call)
    return ContractAnalytics(mid=mid_price, iv=iv, delta=delta, iv_source=source)
