"""Session-report rendering.

Before this file the ONLY thing that rendered a report was the single golden
path, so every branch here could break with all gates green. It also could not
see the timezone defect at all: the golden fixtures build `now` with
tzinfo=NY, so slicing the raw ISO string happened to yield ET. Production
builds `now` from TradingClock.now_utc(), where the same slice yields UTC
(G-08). These tests use UTC stamps deliberately.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from qts_core.clock import NY
from qts_core.config import StrategyConfig
from qts_core.report import render_session_report
from qts_core.store import OrderIntent, StateStore, client_order_id

CFG = StrategyConfig(commission_per_contract_cents=0)
SESSION = dt.date(2026, 6, 17)


def _blob(approved: bool = False) -> str:
    return json.dumps(
        {
            "now_et": "10:14",
            "symbol": "SPY",
            "approved": approved,
            "checks": [
                {"name": "rvol", "passed": False, "value": "0.73", "threshold": ">= 2.0"},
                {"name": "vwap_alignment", "passed": True, "value": "1.0", "threshold": "> vwap"},
            ],
            "selection": None,
        },
        sort_keys=True,
    )


class TestExchangeTimes:
    """G-08: the document is headed ET; every time in it must BE ET."""

    def test_utc_stamp_renders_as_exchange_time(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        # exactly what live.py stores: TradingClock.now_utc() for a 10:14 ET decision
        now_utc = dt.datetime(2026, 6, 17, 10, 14, tzinfo=NY).astimezone(dt.UTC)
        assert now_utc.isoformat().startswith("2026-06-17T14:14"), "precondition: stored as UTC"
        store.log_decision(now_utc, SESSION, "SPY", False, _blob())
        report = render_session_report(store, CFG, SESSION)
        assert "| 10:14 | SPY |" in report, "a 10:14 ET decision was rendered in UTC"
        assert "14:14" not in report

    def test_ny_stamp_is_unchanged(self, tmp_path: Path) -> None:
        """Fixtures already stamp in NY; conversion must be a no-op for them,
        which is why the golden gate never caught this."""
        store = StateStore(tmp_path / "s.db")
        store.log_decision(
            dt.datetime(2026, 6, 17, 10, 14, tzinfo=NY), SESSION, "SPY", False, _blob()
        )
        assert "| 10:14 | SPY |" in render_session_report(store, CFG, SESSION)

    def test_time_column_is_labelled_et(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        store.log_decision(
            dt.datetime(2026, 6, 17, 10, 14, tzinfo=NY), SESSION, "SPY", False, _blob()
        )
        report = render_session_report(store, CFG, SESSION)
        assert "Time (ET)" in report, "an unlabelled time column invites the same bug back"


class TestRenderingBranches:
    def test_empty_session_still_renders_a_banner(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        report = render_session_report(store, CFG, SESSION)
        assert "PAPER TRADING — MODELED FILLS" in report
        assert "Decisions Evaluated:** `0`" in report

    def test_approved_decision_renders_the_full_checklist(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        store.log_decision(
            dt.datetime(2026, 6, 17, 10, 14, tzinfo=NY), SESSION, "SPY", True, _blob(True)
        )
        report = render_session_report(store, CFG, SESSION)
        assert "Last approved decision" in report
        assert "**rvol**" in report

    def test_order_ledger_renders(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "s.db")
        now = dt.datetime(2026, 6, 17, 10, 15, tzinfo=NY)
        coid = client_order_id("qts-v1", SESSION, "SPY260617C00628000", "BUY", 0)
        store.journal_intent(
            OrderIntent(coid, SESSION, "qts-v1", "SPY260617C00628000", "BUY", 1, 420, "ENTRY", 0),
            now,
        )
        store.record_fill(coid, now, 424, -42400, 0)
        report = render_session_report(store, CFG, SESSION)
        assert "ORDER LEDGER (MODELED FILLS)" in report
        assert "FILLED" in report
