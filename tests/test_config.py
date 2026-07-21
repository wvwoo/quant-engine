from __future__ import annotations

import pytest

from qts_core.config import (
    PROVENANCE,
    LiveTradingBlocked,
    StrategyConfig,
    Tier,
    provenance_complete,
    require_paper_mode,
)
from qts_core.money import TickSchedule


class TestProvenance:
    def test_every_field_has_provenance(self) -> None:
        assert provenance_complete() == []

    def test_no_orphan_provenance_entries(self) -> None:
        from dataclasses import fields

        declared = {f.name for f in fields(StrategyConfig)}
        assert set(PROVENANCE) <= declared

    def test_safety_fields_are_marked_safety(self) -> None:
        # ADR-008: rules absent from the spec must be visibly SAFETY, not SPEC.
        for name in (
            "force_flat_et",
            "force_flat_close_buffer_min",
            "daily_loss_limit_cents",
            "live_trading",
        ):
            assert PROVENANCE[name][0] is Tier.SAFETY, name

    def test_ambiguous_spec_fields_are_assumptions(self) -> None:
        for name in ("rsi_period", "rsi_overbought", "rvol_lookback_days", "macd_fast"):
            assert PROVENANCE[name][0] is Tier.ASSUMPTION, name


class TestDefaults:
    def test_report_numbers(self) -> None:
        cfg = StrategyConfig()
        assert cfg.sub_portfolio_cents == 85_000
        assert cfg.sizing_slippage_buffer_bp == 100
        assert cfg.spread_reject_max_bp == 350
        assert cfg.stop_loss_bp == -2_500
        assert cfg.tranche1_gain_bp == 10_000
        assert cfg.trailing_bp == 1_200

    def test_unknown_symbol_fails_loud(self) -> None:
        """DELIBERATE behaviour change (N-08). The old contract guessed NICKEL
        for unknown symbols and its comment claimed the guess 'only costs
        granularity'. Measured: floor(3c, NICKEL) == 0 — a sell-side price of
        1-4 cents floors to ZERO, so a misclassified penny-program symbol
        (AAPL is one) books a fabricated 100% loss at force-flat and renders
        a $0.00 fill in the ledger for a leg that filled. A wrong guess that
        corrupts money is worse than no guess: unknown symbols now raise with
        the exact fix in the message."""
        import pytest

        with pytest.raises(KeyError, match="tick_schedules"):
            StrategyConfig().tick_schedule_for("TSLA")

    def test_known_symbols_still_resolve_case_insensitively(self) -> None:
        assert StrategyConfig().tick_schedule_for("spy") is TickSchedule.FULL_PENNY
        assert StrategyConfig().tick_schedule_for("NVDA") is TickSchedule.PENNY_PROGRAM


class TestLiveTradingGate:
    def test_paper_mode_passes(self) -> None:
        require_paper_mode(StrategyConfig())  # must not raise

    def test_live_flag_without_ack_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("QTS_LIVE_TRADING_OWNER_ACK", raising=False)
        with pytest.raises(LiveTradingBlocked, match="acknowledgement missing"):
            require_paper_mode(StrategyConfig(live_trading=True))

    def test_live_flag_with_ack_still_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even a correct owner ack cannot enable live trading here: no live
        # broker exists in this codebase by design.
        monkeypatch.setenv(
            "QTS_LIVE_TRADING_OWNER_ACK", "أنا المالك وأتحمل مسؤولية التداول الحقيقي"
        )
        with pytest.raises(LiveTradingBlocked, match="NO live broker"):
            require_paper_mode(StrategyConfig(live_trading=True))
