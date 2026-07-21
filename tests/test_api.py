"""FastAPI surface tests.

qts_core/api.py measured 0.0% coverage: nothing imported it, so every endpoint
the dashboard depends on — including the missing-database path that decides
whether the page shows "connected" — was unverified. TestClient runs the app
in-process; pytest-socket keeps the wire shut.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qts_core import api
from qts_core.clock import NY
from qts_core.store import OrderIntent, StateStore, client_order_id

SESSION = dt.date(2026, 6, 17)
NOW = dt.datetime(2026, 6, 17, 10, 15, tzinfo=NY)
LIVE_OCC = "NVDA260617C00205000"  # expires on SESSION
STALE_OCC = "NVDA260616C00205000"  # expired the session before


def _position(occ: str, contracts: int = 2) -> dict[str, object]:
    return {
        "symbol": "NVDA",
        "occ_symbol": occ,
        "phase": "OPEN",
        "contracts": contracts,
        "entry_quote_cents": 420,
        "cost_basis_per_contract_cents": 42400,
        "peak_premium_cents": 420,
        "realized_pnl_cents": 0,
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("QTS_DB", str(tmp_path / "paper.db"))
    monkeypatch.setenv("QTS_SESSION_DATE", SESSION.isoformat())
    return TestClient(api.app)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "paper.db")


class TestMissingDatabase:
    """The dashboard's connection light is driven by this branch. A green
    light with no database behind it is the exact dishonesty this project
    exists to avoid."""

    def test_overview_reports_disconnected(self, client: TestClient) -> None:
        body = client.get("/api/overview").json()
        assert body["connected"] is False
        assert "not found" in body["reason"]

    def test_execution_reports_disconnected(self, client: TestClient) -> None:
        body = client.get("/api/execution").json()
        assert body["connected"] is False
        assert body["orders"] == []

    def test_system_reports_unreachable_db(self, client: TestClient) -> None:
        body = client.get("/api/system").json()
        assert body["state_db"]["reachable"] is False
        assert body["live_trading"].startswith("OFF")


class TestOverview:
    def test_untouched_day_is_flagged_unmeasured(
        self, client: TestClient, store: StateStore
    ) -> None:
        """G-15: with no snapshot the endpoint falls back to the CONFIGURED
        $850. That number is a mandate, not a measurement, and on a no-trade
        day — which the measured evidence says is nearly every day — the
        dashboard displayed it as though the engine had computed it."""
        body = client.get("/api/overview").json()
        assert body["connected"] is True
        assert body["equity_measured"] is False
        assert body["cash_cents"] == body["sub_portfolio_cents"]

    def test_snapshot_marks_equity_as_measured(self, client: TestClient, store: StateStore) -> None:
        store.snapshot_equity(NOW, SESSION, 80_000, 5_000, -1_000)
        body = client.get("/api/overview").json()
        assert body["equity_measured"] is True
        assert body["cash_cents"] == 80_000
        assert body["equity_cents"] == 85_000
        assert body["equity_points"] == [{"ts": NOW.isoformat(), "equity_cents": 85_000}]

    def test_expired_position_is_not_reported_as_open(
        self, client: TestClient, store: StateStore
    ) -> None:
        """G-07: open_positions() filtered only on phase, so a 0DTE row from a
        previous session was served as a live holding — contradicting the
        quarantine the session loop documents and enforces."""
        store.save_position(LIVE_OCC, _position(LIVE_OCC), NOW)
        store.save_position(STALE_OCC, _position(STALE_OCC), NOW)
        body = client.get("/api/overview").json()
        assert list(body["open_positions"]) == [LIVE_OCC]
        assert list(body["expired_positions"]) == [STALE_OCC]

    def test_closed_position_appears_in_neither_bucket(
        self, client: TestClient, store: StateStore
    ) -> None:
        closed = dict(_position(LIVE_OCC), phase="CLOSED", contracts=0)
        store.save_position(LIVE_OCC, closed, NOW)
        body = client.get("/api/overview").json()
        assert body["open_positions"] == {}
        assert body["expired_positions"] == {}


class TestExecution:
    def test_orders_and_decisions_render(self, client: TestClient, store: StateStore) -> None:
        coid = client_order_id("qts-v1", SESSION, LIVE_OCC, "BUY", 0)
        store.journal_intent(
            OrderIntent(coid, SESSION, "qts-v1", LIVE_OCC, "BUY", 2, 424, "ENTRY", 0), NOW
        )
        store.record_fill(coid, NOW, 429, -85_800, 0)
        store.log_decision(NOW, SESSION, "NVDA", True, json.dumps({"checks": []}))
        body = client.get("/api/execution").json()
        assert body["connected"] is True
        assert len(body["orders"]) == 1
        order = body["orders"][0]
        assert order["status"] == "FILLED"
        assert order["modeled"] is True, "every paper fill must carry its MODELED tag"
        assert len(body["decisions"]) == 1
        assert body["decisions"][0]["approved"] is True


class TestRiskRules:
    def test_every_rule_carries_provenance(self, client: TestClient) -> None:
        body = client.get("/api/risk/rules").json()
        assert body["rules"], "an empty rules table would render an empty honest-looking panel"
        for rule in body["rules"]:
            assert rule["tier"] in {"SPEC", "DERIVED", "ASSUMPTION", "SAFETY"}
            assert rule["citation"]

    def test_live_trading_is_advertised_as_off(self, client: TestClient) -> None:
        rules = {r["id"]: r for r in client.get("/api/risk/rules").json()["rules"]}
        assert rules["live_trading"]["value"].startswith("OFF")
        assert rules["live_trading"]["tier"] == "SAFETY"

    def test_rvol_threshold_is_reported_unchanged(self, client: TestClient) -> None:
        """Pins the owner-gated threshold: a silent loosening would be
        curve-fitting, so it is asserted at the surface the owner reads."""
        rules = {r["id"]: r for r in client.get("/api/risk/rules").json()["rules"]}
        assert rules["rvol_min"]["value"] == "2.0"


class TestDashboardAndSystem:
    def test_root_serves_the_dashboard(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_system_reports_paper_mode(self, client: TestClient, store: StateStore) -> None:
        body = client.get("/api/system").json()
        assert body["mode"] == "PAPER"
        assert body["state_db"]["reachable"] is True
        assert "PaperBroker" in body["modules"]["broker"]


class TestBacktestEndpoint:
    """G-12: a backtest existed only on stdout. Serving it is useful; serving
    it without its caveats would be worse than not serving it at all."""

    def test_absence_is_explicit(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QTS_BACKTEST_DIR", str(tmp_path / "empty"))
        body = client.get("/api/backtest").json()
        assert body["available"] is False
        assert "run" in body["reason"], "an empty result must say how to produce one"

    def test_saved_result_is_served_with_its_tag(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qts_core.artifacts import write_artifacts
        from qts_core.backtest import BacktestResult

        monkeypatch.setenv("QTS_BACKTEST_DIR", str(tmp_path / "arts"))
        res = BacktestResult(
            modeled=True,
            assumptions={"options_premiums": "Black-Scholes (B2 open)"},
            sessions=20,
            signal_count=0,
            trades=(),
            net_pnl_cents=0,
            win_rate=None,
            profit_factor=None,
            max_drawdown_cents=0,
            sharpe=None,
            notes=("sharpe not reported: 20 sessions < 30 minimum",),
        )
        write_artifacts(
            res,
            symbol="SPY",
            days=30,
            atm_iv=0.20,
            generated_at=dt.datetime(2026, 6, 17, 16, 0, tzinfo=NY),
        )
        body = client.get("/api/backtest").json()
        assert body["available"] is True
        assert body["modeled"] is True
        assert "MODELED" in body["modeled_note"]
        assert "B2" in body["barrier_open"]
        assert body["results"]["signal_count"] == 0
        assert "net_pnl_cents_MODELED" in body["results"]
