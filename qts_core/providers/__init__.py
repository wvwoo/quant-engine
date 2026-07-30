"""The data-provider seam.

This package was 0 bytes. `YFinanceSource` was constructed BY NAME inside
live.py and run_backtest.py, and nothing — no flag, no env var, no config field
— selected a provider. That is the gap ADR-002 was written to make cheap to
close: greeks are computed locally by qts_core.pricing precisely so that
changing where PRICES come from touches no strategy code. The seam was simply
never cut.

`OptionChainSource` is that seam, and its signatures are copied VERBATIM from
the incumbent `YFinanceSource` rather than designed afresh. The direction
matters: a Protocol invented independently would quietly demand a behaviour
change from the one implementation that is already measured and golden-pinned.
The default path must not move.

Two shapes below are inherited, not chosen, and are written down so the next
implementer does not mistake them for accidents:

- `expirations(session_date=None)` has TWO behaviours behind one optional
  argument: with a date it is disk-cached under (symbol, session_date), because
  the listed expiries do not change intraday; without one it always hits the
  network. A new provider that ignores this starts paying for a fetch on every
  B4 check.
- `fetch_chain` is CALLS-ONLY and takes no `right` parameter. That is invisible
  in the signature and easy to reimplement as calls+puts, which the checklist
  does not expect. The strategy buys calls; the interface says so here in words,
  since the types cannot.

PRICES ONLY. ADR-002 forbids greeks at this boundary: Alpaca returns none at all
for 0DTE (Black-Scholes divides by days-to-expiry), ThetaData and Massive return
vendor-calculated ones, and vendors disagree on r, q, sigma, the clock and the
model — so their numbers are not comparable to each other, let alone to ours. A
test asserts no greek name appears in this Protocol's source.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

from qts_core.models import Bar, MarketView, OptionQuote

__all__ = ["OptionChainSource"]


@runtime_checkable
class OptionChainSource(Protocol):
    """Prices and bars for one underlying. Network I/O lives in implementations.

    `runtime_checkable` so the registry and tests can assert conformance of a
    constructed object. That check only verifies method NAMES exist, which is
    why tests/test_registry.py additionally compares every signature against
    YFinanceSource parameter by parameter.
    """

    symbol: str

    def expirations(self, session_date: dt.date | None = None) -> list[dt.date]:
        """Listed expiries. With `session_date`, served from the disk cache."""
        ...

    def has_same_day_expiry(self, session_date: dt.date) -> bool:
        """Gate B4: 0DTE availability is CHECKED per symbol per day, never
        assumed. NVDA lists Mon/Wed/Fri only; SPY/QQQ list dailies."""
        ...

    def fetch_bars(
        self, *, now: dt.datetime, days: int | None = None, interval_min: int | None = None
    ) -> list[Bar]:
        """Completed bars only. A bar whose close is after `now` is a look-ahead
        leak and must be dropped BEFORE the view is built."""
        ...

    def fetch_chain(
        self, *, session_date: dt.date, now: dt.datetime, spot: float
    ) -> list[OptionQuote]:
        """CALLS only, near the money. A row with unusable bid/ask still becomes
        a quote (0/0) so the checklist can REPORT 'no market' rather than
        silently losing the strike."""
        ...

    def build_view(self, *, now: dt.datetime, session_date: dt.date) -> MarketView:
        """Assemble the only object the strategy is allowed to see.

        The returned view's `meta` MUST carry symbol, provider, delayed and feed
        (see registry.assert_meta_is_honest): a delayed or derived feed that
        looks identical to a real-time one is a false claim, not a detail.
        """
        ...
