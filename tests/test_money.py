"""Money layer tests.

The first block encodes the reference report's arithmetic as EXACT fractions.
These are the audit findings turned executable: if any of them fails, either
the implementation or the recon claim is wrong — no third option.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from qts_core.money import (
    BP,
    CONTRACT_MULTIPLIER,
    Cents,
    MoneyError,
    TickSchedule,
    buffered_contract_cost_cents,
    cents_from_decimal_str,
    cents_from_quote,
    contract_cost_cents,
    fmt,
    pct_level_cents,
    round_to_tick,
    size_position,
    tick_size_cents,
)

# ---------------------------------------------------------------- report arithmetic


class TestReportArithmetic:
    """institutional_quant_trading_report.md, recomputed exactly."""

    def test_buffered_cost_matches_report(self) -> None:
        # $4.20 x 100 x 1.01 = $424.20 — the report's own formula omits the
        # 100x multiplier in its LaTeX; the number only works with it.
        assert buffered_contract_cost_cents(420, 100) == 42420

    def test_sizing_two_contracts(self) -> None:
        n, unit, gross, residual = size_position(85000, 420, 100)
        assert n == 2
        assert unit == 42420
        assert gross == 84840  # $848.40 gross deployed
        assert residual == 160  # $1.60 residual buffer

    def test_knife_edge_one_cent_flips_size(self) -> None:
        # Finding knife-edge-sizing: 2 contracts survive on $0.0079 of headroom.
        # One cent of premium MUST flip the count to 1.
        n_now, *_ = size_position(85000, 420, 100)
        n_up, *_ = size_position(85000, 421, 100)
        assert (n_now, n_up) == (2, 1)

    def test_stop_target_runner_levels(self) -> None:
        assert pct_level_cents(420, -2500) == 315  # -25% stop
        assert pct_level_cents(420, +10000) == 840  # +100% tranche-1
        assert pct_level_cents(420, +1000) == 462  # breakeven +10% runner floor

    def test_realized_stop_loss_is_26_over_101_not_25_percent(self) -> None:
        # Finding stop-loss-not-25pct: with cost basis 42420 (slipped) and stop
        # exit at 315/share (31500/contract), the realized loss fraction is
        # exactly 26/101 = 25.7425...% — not the labeled 25%.
        basis, stop_proceeds = 42420, 31500
        assert Fraction(basis - stop_proceeds, basis) == Fraction(26, 101)

    def test_target_multiple_is_200_over_101(self) -> None:
        # Tranche-1 exit at 840/share against slipped basis 424.20/share.
        assert Fraction(84000, 42420) == Fraction(200, 101)

    def test_runner_floor_multiple_is_110_over_101(self) -> None:
        assert Fraction(46200, 42420) == Fraction(110, 101)

    def test_tranche1_principal_shortfall_is_840_cents(self) -> None:
        # Finding tranche1-principal-shortfall: selling 1 contract at $840
        # recovers $840.00 against $848.40 deployed — $8.40 stays at risk,
        # so "entirely risk-free" is false by exactly this amount.
        _, _unit, gross, _ = size_position(85000, 420, 100)
        proceeds_one_contract = 840 * CONTRACT_MULTIPLIER
        assert gross - proceeds_one_contract == 840  # cents == $8.40

    def test_trailing_stop_dominates_runner_floor(self) -> None:
        # Finding runner-stop-unreachable: 12% trail from the 840 peak sits at
        # 739 (floor rounding) — permanently above the 462 floor. The floor is
        # dead code unless the peak collapses below ~525.
        trail_from_peak = pct_level_cents(840, -1200)
        assert trail_from_peak == 739
        assert trail_from_peak > 462

    def test_capital_floor_after_full_stop(self) -> None:
        # Report claims "absolute floor preservation at $630.00 cash equity":
        # 2 contracts exiting at 315/share recover 63000 cents. True only
        # against the UNSLIPPED notional; against capital 85000 the floor
        # includes the residual: 63000 + 160 = 63160.
        stop_recovery = 2 * 315 * CONTRACT_MULTIPLIER
        assert stop_recovery == 63000
        _, _, _, residual = size_position(85000, 420, 100)
        assert stop_recovery + residual == 63160


# ---------------------------------------------------------------- boundaries


class TestBoundaries:
    def test_decimal_str_parses_exact(self) -> None:
        assert cents_from_decimal_str("4.20") == 420
        assert cents_from_decimal_str("850") == 85000
        assert cents_from_decimal_str("0.01") == 1

    def test_decimal_str_rejects_subcent(self) -> None:
        with pytest.raises(MoneyError):
            cents_from_decimal_str("4.242")

    def test_quote_float_boundary(self) -> None:
        assert cents_from_quote(4.20) == 420
        assert cents_from_quote(204.79) == 20479

    def test_quote_rejects_nan_and_inf(self) -> None:
        with pytest.raises(MoneyError):
            cents_from_quote(float("nan"))
        with pytest.raises(MoneyError):
            cents_from_quote(float("inf"))

    def test_quote_rejects_subcent_garbage(self) -> None:
        with pytest.raises(MoneyError):
            cents_from_quote(4.2037)

    def test_premium_must_be_positive(self) -> None:
        with pytest.raises(MoneyError):
            contract_cost_cents(0)
        with pytest.raises(MoneyError):
            contract_cost_cents(-420)

    def test_fmt_deterministic(self) -> None:
        assert fmt(42420) == "$424.20"
        assert fmt(-1092) == "-$10.92"
        assert fmt(160) == "$1.60"
        assert fmt(Cents(5)) == "$0.05"


# ---------------------------------------------------------------- commissions & ticks


class TestCommissionAndTicks:
    def test_commission_reduces_residual_not_count_here(self) -> None:
        n, _, gross, residual = size_position(85000, 420, 100, commission_per_contract_cents=65)
        assert n == 2
        assert gross == 84840 + 130
        assert residual == 85000 - 84970

    def test_commission_can_reduce_count(self) -> None:
        # With $0.79 headroom, a commission above it must drop to 1 contract.
        n, *_ = size_position(85000, 420, 100, commission_per_contract_cents=100)
        assert n == 1

    def test_tick_sizes(self) -> None:
        assert tick_size_cents(462, TickSchedule.FULL_PENNY) == 1
        assert tick_size_cents(462, TickSchedule.PENNY_PROGRAM) == 5
        assert tick_size_cents(250, TickSchedule.PENNY_PROGRAM) == 1
        assert tick_size_cents(462, TickSchedule.NICKEL) == 10
        assert tick_size_cents(250, TickSchedule.NICKEL) == 5

    def test_462_illegal_under_penny_program(self) -> None:
        # Finding MONEY-tick-rounding: $4.62 is not a legal NVDA price.
        assert round_to_tick(462, TickSchedule.PENNY_PROGRAM, "floor") == 460
        assert round_to_tick(462, TickSchedule.PENNY_PROGRAM, "ceil") == 465
        assert round_to_tick(462, TickSchedule.PENNY_PROGRAM, "nearest") == 460

    def test_462_legal_under_full_penny(self) -> None:
        assert round_to_tick(462, TickSchedule.FULL_PENNY) == 462

    def test_nearest_rounds_half_up(self) -> None:
        # 463 with tick 5: r=3, 3*2>=5 -> up
        assert round_to_tick(463, TickSchedule.PENNY_PROGRAM, "nearest") == 465

    def test_bp_constant_sanity(self) -> None:
        assert BP == 10_000


class TestExecutableCost:
    """Sizing must price a contract the way the BROKER will actually fill it.

    The report's formula rounds at CONTRACT granularity (420 x 1.01 -> 42420).
    A real fill rounds the per-SHARE price and snaps it to a legal tick
    (420 x 1.01 = 424.2 -> 425/share -> 42500/contract). Sizing on the cheaper
    figure commits capital that does not exist (finding QTS-R2).
    """

    def test_executable_cost_exceeds_report_formula(self) -> None:
        from qts_core.money import executable_contract_cost_cents

        assert buffered_contract_cost_cents(420, 100) == 42420  # report's number
        assert executable_contract_cost_cents(420, 100, TickSchedule.FULL_PENNY) == 42500

    def test_executable_cost_respects_nickel_grid(self) -> None:
        from qts_core.money import executable_contract_cost_cents

        # 420 x 1.01 = 424.2 -> ceil 425; on the NICKEL grid (>=300 -> 10c)
        # the next legal price is 430.
        assert executable_contract_cost_cents(420, 100, TickSchedule.NICKEL) == 43000

    def test_sizing_never_overspends_at_execution_prices(self) -> None:
        from qts_core.money import executable_contract_cost_cents

        commission = 65
        for ask in range(50, 3000, 11):
            for tick in TickSchedule:
                n, _, gross, _ = size_position(85000, ask, 100, commission, tick)
                if n == 0:
                    continue
                unit_cost = executable_contract_cost_cents(ask, 100, tick)
                actual = n * (unit_cost + commission)
                assert actual <= 85000, f"overspend ask={ask} tick={tick} n={n}"
                assert gross == actual

    def test_report_case_drops_to_one_contract_with_commission(self) -> None:
        # The bug: estimate said 2 contracts / $849.70, execution charged
        # $851.30 on an $850 mandate. Honest sizing takes 1.
        n, unit, gross, residual = size_position(85000, 420, 100, 65, TickSchedule.PENNY_PROGRAM)
        assert unit == 42500
        assert (n, gross, residual) == (1, 42565, 42435)

    def test_report_case_still_two_contracts_without_commission(self) -> None:
        # Commission-free (the report's own assumption) the answer is unchanged
        # at 2 contracts — but the residual is honestly $0.00, not $1.60.
        n, unit, gross, residual = size_position(85000, 420, 100, 0, TickSchedule.PENNY_PROGRAM)
        assert (n, unit, gross, residual) == (2, 42500, 85000, 0)
