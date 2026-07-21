from __future__ import annotations

import math

import pytest

from qts_core.pricing import (
    MIN_T_YEARS,
    ContractAnalytics,
    PricingError,
    analyze_contract,
    bs_delta,
    bs_price,
    forward_from_parity,
    implied_vol,
    norm_cdf,
)

# Report-scenario constants: NVDA $204.79, K=$205, 10:15 -> 16:00 ET on a 0DTE.
S, K = 204.79, 205.0
T_0DTE = 5.75 / (24.0 * 365.0)
R = 0.045


class TestBlackScholes:
    def test_norm_cdf_symmetry(self) -> None:
        assert norm_cdf(0.0) == pytest.approx(0.5)
        assert norm_cdf(1.0) + norm_cdf(-1.0) == pytest.approx(1.0)

    def test_atm_call_positive_and_below_spot(self) -> None:
        p = bs_price(S, K, T_0DTE, R, 0.68)
        assert 0 < p < S

    def test_put_call_parity_holds(self) -> None:
        sigma = 0.68
        c = bs_price(S, K, T_0DTE, R, sigma, call=True)
        p = bs_price(S, K, T_0DTE, R, sigma, call=False)
        lhs = c - p
        rhs = S - K * math.exp(-R * T_0DTE)
        assert lhs == pytest.approx(rhs, abs=1e-10)

    def test_zero_sigma_gives_discounted_intrinsic(self) -> None:
        deep_itm = bs_price(210.0, 205.0, T_0DTE, R, 0.0)
        fwd = 210.0 * math.exp(R * T_0DTE)
        assert deep_itm == pytest.approx(math.exp(-R * T_0DTE) * (fwd - 205.0))

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(PricingError):
            bs_price(-1.0, K, T_0DTE, R, 0.5)
        with pytest.raises(PricingError):
            bs_delta(S, K, T_0DTE, R, 0.0)


class TestDelta:
    def test_atm_0dte_call_delta_near_half(self) -> None:
        d = bs_delta(S, K, T_0DTE, R, 0.68)
        assert 0.45 <= d <= 0.55  # the report's band, at the report's scenario

    def test_deep_itm_delta_near_one(self) -> None:
        assert bs_delta(230.0, 205.0, T_0DTE, R, 0.68) > 0.99

    def test_deep_otm_delta_near_zero(self) -> None:
        assert bs_delta(180.0, 205.0, T_0DTE, R, 0.68) < 0.01

    def test_put_delta_is_call_minus_one(self) -> None:
        c = bs_delta(S, K, T_0DTE, R, 0.68, call=True)
        p = bs_delta(S, K, T_0DTE, R, 0.68, call=False)
        assert c - p == pytest.approx(1.0)

    def test_t_to_zero_becomes_step(self) -> None:
        # Documented hazard: at tiny T, delta ~ step around the strike.
        tiny = MIN_T_YEARS
        assert bs_delta(205.5, 205.0, tiny, R, 0.3) > 0.9
        assert bs_delta(204.5, 205.0, tiny, R, 0.3) < 0.1


class TestImpliedVol:
    def test_roundtrip(self) -> None:
        for sigma in (0.2, 0.68, 1.5):
            price = bs_price(S, K, T_0DTE, R, sigma)
            iv = implied_vol(price, S, K, T_0DTE, R)
            assert iv is not None
            assert iv == pytest.approx(sigma, abs=1e-5)

    def test_below_intrinsic_returns_none(self) -> None:
        assert implied_vol(0.0001, 210.0, 205.0, T_0DTE, R) is None

    def test_zero_or_negative_price_returns_none(self) -> None:
        assert implied_vol(0.0, S, K, T_0DTE, R) is None
        assert implied_vol(-1.0, S, K, T_0DTE, R) is None

    def test_absurd_price_returns_none(self) -> None:
        assert implied_vol(S * 1.5, S, K, T_0DTE, R) is None


class TestParityForward:
    def test_forward_recovers_spot_growth(self) -> None:
        sigma = 0.68
        c = bs_price(S, K, T_0DTE, R, sigma, call=True)
        p = bs_price(S, K, T_0DTE, R, sigma, call=False)
        f = forward_from_parity(c, p, K, T_0DTE, R)
        assert f == pytest.approx(S * math.exp(R * T_0DTE), abs=1e-8)


class TestAnalyzeContract:
    def test_computed_source_preferred(self) -> None:
        price = bs_price(S, K, T_0DTE, R, 0.68)
        a = analyze_contract(
            mid_price=price, spot=S, strike=K, t_years=T_0DTE, rate=R, call=True, iv_hint=9.9
        )
        assert a.iv_source == "computed"
        assert a.iv == pytest.approx(0.68, abs=1e-5)
        assert a.delta is not None

    def test_hint_fallback_when_mid_unusable(self) -> None:
        a = analyze_contract(
            mid_price=0.0001,
            spot=210.0,
            strike=205.0,
            t_years=T_0DTE,
            rate=R,
            call=True,
            iv_hint=0.55,
        )
        assert a.iv_source == "provider_hint"
        assert a.iv == 0.55

    def test_unavailable_when_no_source(self) -> None:
        a = analyze_contract(
            mid_price=0.0001,
            spot=210.0,
            strike=205.0,
            t_years=T_0DTE,
            rate=R,
            call=True,
            iv_hint=None,
        )
        assert a == ContractAnalytics(mid=0.0001, iv=None, delta=None, iv_source="unavailable")
