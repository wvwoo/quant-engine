"""Integer-cent money arithmetic.

Every cash amount in this system is an ``int`` number of US cents. Floats are
rejected at the boundary: option premiums quote in cents, the contract
multiplier is exactly 100, and the reference report's risk levels are exact
percentages — all of which are representable without floating point. The
specific bugs this module exists to prevent (recon findings MONEY-*):

- float floor division producing a contract count off by one whole contract
- the -25% stop threshold comparing unequal under binary float
- the report's cost formula silently dropping the 100x contract multiplier
- order prices that are not legal quote increments (e.g. $4.62 in a
  nickel-quoting class)
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Literal, NewType

Cents = NewType("Cents", int)

CONTRACT_MULTIPLIER = 100
BP = 10_000  # basis-point denominator: 100 bp == 1%


class MoneyError(ValueError):
    """Raised when a value crosses the money boundary in an unsafe form."""


class TickSchedule(Enum):
    """US listed-option minimum quote increments.

    FULL_PENNY   — $0.01 at every price level (SPY, QQQ, IWM).
    PENNY_PROGRAM — $0.01 below $3.00, $0.05 at/above (most penny-program
                    classes, NVDA included).
    NICKEL       — $0.05 below $3.00, $0.10 at/above (non-penny classes).
    """

    FULL_PENNY = "full_penny"
    PENNY_PROGRAM = "penny_program"
    NICKEL = "nickel"


def cents_from_decimal_str(amount: str) -> Cents:
    """Parse an exact decimal string ("4.20", "850") into cents.

    Rejects floats by construction (only ``str`` accepted) and rejects any
    value with sub-cent precision — config capital and spec thresholds are
    cent-exact or they are wrong.
    """
    try:
        d = Decimal(amount)
    except InvalidOperation as exc:  # pragma: no cover - message path
        raise MoneyError(f"not a decimal amount: {amount!r}") from exc
    cents = d * 100
    if cents != cents.to_integral_value():
        raise MoneyError(f"sub-cent precision rejected: {amount!r}")
    return Cents(int(cents))


def cents_from_quote(price: float) -> Cents:
    """The ONLY place a float price may enter the money domain.

    Market-data providers hand us floats. Quotes are cent-precision; anything
    further from a whole cent than a tolerance is corrupt data, not a price.
    """
    if price != price or price in (float("inf"), float("-inf")):
        raise MoneyError(f"non-finite quote: {price!r}")
    scaled = price * 100
    nearest = round(scaled)
    if abs(scaled - nearest) > 1e-6 * max(1.0, abs(scaled)):
        raise MoneyError(f"quote is not cent-precision: {price!r}")
    return Cents(int(nearest))


def fmt(cents: int) -> str:
    """Render cents as a dollar string: 42420 -> '$424.20'. Deterministic."""
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    return f"{sign}${c // 100}.{c % 100:02d}"


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def contract_cost_cents(premium_cents: int) -> Cents:
    """Cost of one contract at a per-share premium. The 100x lives HERE."""
    if premium_cents <= 0:
        raise MoneyError(f"premium must be positive: {premium_cents}")
    return Cents(premium_cents * CONTRACT_MULTIPLIER)


def buffered_contract_cost_cents(premium_cents: int, buffer_bp: int) -> Cents:
    """Contract cost inflated by a conservatism buffer, rounded UP.

    Report: $4.20 x 100 x 1.01 = $424.20 exactly -> 42000 * 10100 / 10000 = 42420.
    Ceiling keeps the estimate conservative when the product is not exact.
    """
    if buffer_bp < 0:
        raise MoneyError(f"negative buffer: {buffer_bp}")
    base = contract_cost_cents(premium_cents)
    return Cents(_ceil_div(base * (BP + buffer_bp), BP))


def size_position(
    capital_cents: int,
    premium_cents: int,
    buffer_bp: int,
    commission_per_contract_cents: int = 0,
) -> tuple[int, Cents, Cents, Cents]:
    """Volumetric allocation: floor(capital / buffered cost), all-integer.

    Returns (contracts, buffered_cost_per_contract, gross_committed, residual).
    Gross includes entry commissions; if commissions push gross past capital,
    the count is reduced — never overspend.
    """
    if capital_cents <= 0:
        raise MoneyError(f"capital must be positive: {capital_cents}")
    unit = buffered_contract_cost_cents(premium_cents, buffer_bp)
    n = capital_cents // unit
    while n > 0 and n * (unit + commission_per_contract_cents) > capital_cents:
        n -= 1
    gross = Cents(n * (unit + commission_per_contract_cents))
    return int(n), unit, gross, Cents(capital_cents - gross)


def pct_level_cents(base_cents: int, delta_bp: int) -> Cents:
    """base * (1 + delta_bp/10000) with exact integer floor.

    Stop:    pct_level_cents(420, -2500) -> 315   (-25%)
    Target:  pct_level_cents(420, +10000) -> 840  (+100%)
    Runner:  pct_level_cents(420, +1000)  -> 462  (+10%)
    """
    return Cents((base_cents * (BP + delta_bp)) // BP)


def tick_size_cents(price_cents: int, schedule: TickSchedule) -> int:
    if schedule is TickSchedule.FULL_PENNY:
        return 1
    if schedule is TickSchedule.PENNY_PROGRAM:
        return 1 if price_cents < 300 else 5
    return 5 if price_cents < 300 else 10


def round_to_tick(
    price_cents: int,
    schedule: TickSchedule,
    mode: Literal["floor", "ceil", "nearest"] = "nearest",
) -> Cents:
    """Snap a price to a legal quote increment for its class.

    $4.62 (462) under PENNY_PROGRAM (NVDA): tick=5 -> floor 460 / ceil 465.
    Under FULL_PENNY (SPY/QQQ): 462 is already legal.
    """
    tick = tick_size_cents(price_cents, schedule)
    q, r = divmod(price_cents, tick)
    if r == 0:
        return Cents(price_cents)
    if mode == "floor":
        return Cents(q * tick)
    if mode == "ceil":
        return Cents((q + 1) * tick)
    return Cents((q + (1 if r * 2 >= tick else 0)) * tick)
