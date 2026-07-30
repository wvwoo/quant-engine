"""OptionChainSource Protocol + provider registry.

Before this, qts_core/providers/__init__.py was 0 bytes and YFinanceSource was
constructed BY NAME at live.py and run_backtest.py. No flag, env var or config
field selected a provider — even though ADR-002 exists precisely so that
swapping one would not touch a line of strategy code. "Give it a better data
feed" therefore meant editing entry points.

The load-bearing property here is that the DEFAULT PATH DOES NOT MOVE:
YFinanceSource must satisfy the Protocol with zero behavioural change, and the
golden suite must stay byte-identical. A seam that alters the thing it wraps is
not a seam.
"""

from __future__ import annotations

import datetime as dt
import inspect

import pytest

from qts_core.config import StrategyConfig
from qts_core.providers import OptionChainSource
from qts_core.providers import registry as reg
from qts_core.providers.yfinance_source import YFinanceSource

SESSION = dt.date(2026, 6, 17)


class TestProtocolShape:
    def test_the_protocol_declares_exactly_the_five_public_methods(self) -> None:
        """Five, not four: fetch_chain was missing from the original inventory
        of this surface and would have been left out of the contract."""
        declared = {
            n
            for n in vars(OptionChainSource)
            if not n.startswith("_") and callable(getattr(OptionChainSource, n, None))
        }
        assert declared == {
            "expirations",
            "has_same_day_expiry",
            "fetch_bars",
            "fetch_chain",
            "build_view",
        }

    def test_yfinance_source_satisfies_the_protocol_at_runtime(self) -> None:
        assert isinstance(YFinanceSource("SPY"), OptionChainSource)

    @pytest.mark.parametrize(
        "name",
        ["expirations", "has_same_day_expiry", "fetch_bars", "fetch_chain", "build_view"],
    )
    def test_each_signature_matches_the_incumbent_implementation_exactly(self, name: str) -> None:
        """Copied verbatim from YFinanceSource, not paraphrased. If the two ever
        drift, the Protocol is documentation rather than a contract."""
        proto = inspect.signature(getattr(OptionChainSource, name))
        impl = inspect.signature(getattr(YFinanceSource, name))
        assert proto.parameters.keys() == impl.parameters.keys()
        for pname in proto.parameters:
            if pname == "self":
                continue
            assert proto.parameters[pname].kind == impl.parameters[pname].kind, pname
            assert proto.parameters[pname].default == impl.parameters[pname].default, pname

    def test_the_protocol_carries_no_greeks(self) -> None:
        """ADR-002: greeks are OURS (qts_core.pricing), never the provider's.
        Alpaca returns no greeks at all for 0DTE (division by days-to-expiry),
        and vendors disagree on r/q/sigma/clock/model, so their deltas are not
        even comparable. Prices only, at the interface."""
        source = inspect.getsource(OptionChainSource)
        for banned in ("delta", "gamma", "theta", "vega", "greek", "implied_vol"):
            assert banned not in source.lower().replace("iv_hint", "")


class TestRegistry:
    def test_default_is_yfinance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("QTS_DATA_PROVIDER", raising=False)
        assert reg.resolve_name() == "yfinance"

    def test_env_var_selects_a_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QTS_DATA_PROVIDER", "alpaca")
        assert reg.resolve_name() == "alpaca"

    def test_explicit_argument_beats_the_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A --provider flag on one run must not be silently overridden by a
        stale export in the operator's shell."""
        monkeypatch.setenv("QTS_DATA_PROVIDER", "alpaca")
        assert reg.resolve_name("yfinance") == "yfinance"

    def test_case_and_whitespace_are_forgiven(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QTS_DATA_PROVIDER", "  YFinance \n")
        assert reg.resolve_name() == "yfinance"

    def test_unknown_provider_is_a_loud_refusal_listing_what_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QTS_DATA_PROVIDER", "bloomberg")
        with pytest.raises(reg.UnknownProvider) as exc:
            reg.resolve_name()
        msg = str(exc.value)
        assert "bloomberg" in msg
        assert "yfinance" in msg, "the message must list the providers that DO exist"

    def test_available_lists_registered_names(self) -> None:
        assert "yfinance" in reg.available()
        assert "alpaca" in reg.available()

    def test_make_source_returns_the_incumbent_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("QTS_DATA_PROVIDER", raising=False)
        src = reg.make_source("SPY")
        assert isinstance(src, YFinanceSource)
        assert isinstance(src, OptionChainSource)

    def test_make_source_threads_cfg_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """P2: live.py built `YFinanceSource(symbol)` with no cfg, so the source
        silently constructed its own StrategyConfig(). A --provider flag that
        could not carry configuration would be inert by construction."""
        monkeypatch.delenv("QTS_DATA_PROVIDER", raising=False)
        cfg = StrategyConfig(bars_fetch_days=7)
        src = reg.make_source("SPY", cfg=cfg)
        assert isinstance(src, YFinanceSource)
        assert src.cfg.bars_fetch_days == 7, "cfg must reach the provider"

    def test_symbol_is_normalised_by_whichever_provider_is_built(self) -> None:
        assert reg.make_source("spy").symbol == "SPY"

    def test_alpaca_provider_is_selectable_by_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Proof the switch actually switches — the whole point of the lane."""
        monkeypatch.setenv("QTS_DATA_PROVIDER", "alpaca")
        monkeypatch.setenv("APCA_API_KEY_ID", "keyid00000001")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "secret00000001")
        src = reg.make_source("SPY")
        assert not isinstance(src, YFinanceSource)
        assert type(src).__name__ == "AlpacaDataSource"
        assert isinstance(src, OptionChainSource)


class TestHonestyGuardIsRequiredOfEveryProvider:
    """Every MarketView must declare its origin and its freshness. A 15-minute
    indicative quote that is indistinguishable from a real-time one is exactly
    the species of false claim this project exists to prevent. The per-provider
    assertions live with each provider's own fake transport; what is enforced
    HERE is that the requirement is uniform and machine-checked."""

    REQUIRED_META_KEYS = ("symbol", "provider", "delayed", "feed")

    def test_the_required_keys_are_declared_in_one_place(self) -> None:
        assert reg.REQUIRED_META_KEYS == self.REQUIRED_META_KEYS

    def test_the_validator_accepts_a_complete_meta(self) -> None:
        reg.assert_meta_is_honest(
            {"symbol": "SPY", "provider": "yfinance", "delayed": "true", "feed": "yahoo-delayed"}
        )

    @pytest.mark.parametrize("missing", REQUIRED_META_KEYS)
    def test_the_validator_rejects_an_incomplete_meta(self, missing: str) -> None:
        meta = {
            "symbol": "SPY",
            "provider": "yfinance",
            "delayed": "true",
            "feed": "yahoo-delayed",
        }
        del meta[missing]
        with pytest.raises(reg.DishonestMeta, match=missing):
            reg.assert_meta_is_honest(meta)

    def test_the_validator_rejects_a_non_boolean_delayed_flag(self) -> None:
        """ "maybe" is not an answer to "is this data delayed"."""
        with pytest.raises(reg.DishonestMeta, match="delayed"):
            reg.assert_meta_is_honest(
                {"symbol": "SPY", "provider": "x", "delayed": "maybe", "feed": "f"}
            )

    def test_every_registered_provider_produces_an_honest_view(self) -> None:
        """The uniform check: each provider is asked for the meta it would stamp
        on a view, and every one must pass the validator. A new provider cannot
        be registered without declaring its freshness."""
        for name in reg.available():
            meta = reg.declared_meta(name, "SPY")
            reg.assert_meta_is_honest(meta)
            assert meta["provider"] == name
