"""Which data provider this run uses, and the honesty contract it must meet.

Selection order, explicit and tested:
  1. an argument passed in (the --provider flag on one run)
  2. QTS_DATA_PROVIDER
  3. "yfinance" — the incumbent, so the default path is byte-identical

The flag beating the env var is deliberate: a stale `export QTS_DATA_PROVIDER=`
in an operator's shell must not silently override what they typed on the command
line for this run.

Providers are imported LAZILY inside the factory. yfinance pulls in pandas and
an Alpaca source needs credentials; importing every provider to build one would
make an unrelated provider's missing dependency (or missing key) break a run
that never wanted it.

`cfg` is threaded through explicitly. live.py used to build `YFinanceSource(symbol)`
with no config at all, so the source constructed its own StrategyConfig() —
which means a --provider flag that could not carry configuration would have been
inert by construction (finding P2).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING

from qts_core.config import StrategyConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from qts_core.providers import OptionChainSource

PROVIDER_ENV = "QTS_DATA_PROVIDER"
DEFAULT_PROVIDER = "yfinance"

# Every MarketView must answer these four questions about its own data. See
# assert_meta_is_honest for why "delayed" is not optional.
REQUIRED_META_KEYS = ("symbol", "provider", "delayed", "feed")


class UnknownProvider(ValueError):
    """A provider name that is not registered."""


class DishonestMeta(AssertionError):
    """A MarketView failed to declare its origin or its freshness."""


# name -> (builder, static meta describing what that provider's data IS)
_FEEDS: dict[str, str] = {
    # Yahoo: OPRA options documented 15-minute delayed (Yahoo's own
    # exchanges/data-providers table, ICE Data Services). Measured live in this
    # project at 3.1-4.9 min, which is fresher than nominal but still delayed.
    "yfinance": "yahoo-delayed",
    # Alpaca free tier: options come from the "Indicative Pricing Feed", a free
    # DERIVATIVE of OPRA — derivative quotes, trades delayed 15 minutes. Equity
    # bars on the same free tier are real-time IEX. The feed name records which
    # of those produced the quotes, because they are not the same claim.
    "alpaca": "alpaca-indicative",
}

_DELAYED: dict[str, str] = {"yfinance": "true", "alpaca": "true"}


def available() -> tuple[str, ...]:
    """Registered provider names, sorted for a stable error message."""
    return tuple(sorted(_FEEDS))


def resolve_name(name: str | None = None) -> str:
    """Pick the provider for this run and validate it."""
    chosen = name if name else os.environ.get(PROVIDER_ENV) or DEFAULT_PROVIDER
    chosen = chosen.strip().lower()
    if chosen not in _FEEDS:
        raise UnknownProvider(
            f"unknown data provider {chosen!r}. Available: {', '.join(available())}. "
            f"Set {PROVIDER_ENV} or pass --provider."
        )
    return chosen


def declared_meta(name: str, symbol: str) -> dict[str, str]:
    """The meta a provider stamps on every view it builds.

    Kept here rather than inside each provider so that registering a new source
    without declaring its freshness is impossible: available() and this table are
    the same dict, and a test walks every registered name through the validator.
    """
    resolved = resolve_name(name)
    return {
        "symbol": symbol.upper(),
        "provider": resolved,
        "delayed": _DELAYED[resolved],
        "feed": _FEEDS[resolved],
    }


def assert_meta_is_honest(meta: dict[str, str]) -> None:
    """Refuse a view that does not say where it came from and how stale it is.

    This is not defensive programming for its own sake. The engine's product is
    honest numbers; a derived, 15-minute-delayed indicative quote rendered
    identically to a real-time OPRA quote is a false claim about the market, and
    it would be invisible downstream — every consumer would read a price and
    have no way to ask how old it was.
    """
    for key in REQUIRED_META_KEYS:
        if key not in meta or not str(meta[key]).strip():
            raise DishonestMeta(
                f"MarketView.meta is missing {key!r}: a view must declare "
                f"{', '.join(REQUIRED_META_KEYS)}. Got keys: {sorted(meta)}"
            )
    if meta["delayed"] not in ("true", "false"):
        raise DishonestMeta(
            f"MarketView.meta['delayed'] must be 'true' or 'false', got "
            f"{meta['delayed']!r} — 'is this data delayed' has no third answer"
        )


def _build_yfinance(symbol: str, cfg: StrategyConfig) -> OptionChainSource:
    from qts_core.providers.yfinance_source import YFinanceSource

    return YFinanceSource(symbol, cfg=cfg)


def _build_alpaca(symbol: str, cfg: StrategyConfig) -> OptionChainSource:
    from qts_core.providers.alpaca_source import AlpacaDataSource

    return AlpacaDataSource(symbol, cfg=cfg)


_BUILDERS: dict[str, Callable[[str, StrategyConfig], OptionChainSource]] = {
    "yfinance": _build_yfinance,
    "alpaca": _build_alpaca,
}


def make_source(
    symbol: str, *, name: str | None = None, cfg: StrategyConfig | None = None
) -> OptionChainSource:
    """Build the selected provider for one symbol."""
    resolved = resolve_name(name)
    return _BUILDERS[resolved](symbol, cfg if cfg is not None else StrategyConfig())
