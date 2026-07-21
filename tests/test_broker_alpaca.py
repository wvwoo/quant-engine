"""AlpacaPaperBroker (ADR-012) — every path through a fake transport, no wire.

The safety property under test is structural: the base URL is a module
constant pointing at the PAPER host, unreachable from config or environment.
Changing it requires editing reviewed source AND this test — a two-file,
two-eyes change by construction.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from qts_core.broker_alpaca import (
    ALPACA_PAPER_BASE_URL,
    AlpacaCredentialsMissing,
    AlpacaOrderNotFilled,
    AlpacaPaperBroker,
)
from qts_core.clock import NY
from qts_core.store import OrderIntent, client_order_id

SESSION = dt.date(2026, 6, 17)
NOW = dt.datetime(2026, 6, 17, 10, 15, tzinfo=NY)
OCC = "SPY260617C00628000"


def intent(side: str = "BUY", contracts: int = 2, seq: int = 0) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id("qts-v1", SESSION, OCC, side, seq),
        session_date=SESSION,
        strategy="qts-v1",
        occ_symbol=OCC,
        side=side,
        contracts=contracts,
        limit_cents=424,
        reason="ENTRY",
        seq=seq,
    )


class FakeTransport:
    """Scripted venue. Records every call; answers from a queue per route."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.lookup: tuple[int, dict[str, Any]] = (404, {})
        self.submit: tuple[int, dict[str, Any]] = (200, {})
        self.polls: list[tuple[int, dict[str, Any]]] = []
        self.final: tuple[int, dict[str, Any]] = (200, {})

    def __call__(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, path, body))
        if path.startswith("/v2/orders:by_client_order_id"):
            return self.lookup
        if method == "POST":
            return self.submit
        if method == "DELETE":
            return 204, {}
        if self.polls:
            return self.polls.pop(0)
        return self.final

    def posted(self) -> list[dict[str, Any]]:
        return [b for m, _, b in self.calls if m == "POST" and b is not None]


def order(status: str, qty: int = 2, avg: str = "4.29", oid: str = "oid-1") -> dict[str, Any]:
    return {
        "id": oid,
        "status": status,
        "filled_qty": str(qty if status == "filled" else 0),
        "filled_avg_price": avg if status == "filled" else None,
    }


def broker(t: FakeTransport, timeout: float = 5.0) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(t, poll_timeout_s=timeout, poll_interval_s=0, sleep=lambda _s: None)


class TestStructuralSafety:
    def test_base_url_is_the_paper_host_and_a_constant(self) -> None:
        """Two-file change by design: repointing the adapter means editing the
        module constant AND this assertion."""
        assert ALPACA_PAPER_BASE_URL == "https://paper-api.alpaca.markets"

    def test_missing_credentials_fail_closed_with_instructions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
        with pytest.raises(AlpacaCredentialsMissing, match="PAPER account keys"):
            AlpacaPaperBroker()


class TestExecute:
    def test_limit_order_fills_and_maps_to_a_fill(self) -> None:
        t = FakeTransport()
        t.submit = (200, order("accepted"))
        t.polls = [(200, order("filled", qty=2, avg="4.29"))]
        fill = broker(t).execute(intent(), 420, 424, NOW)
        assert fill.premium_cents == 429
        assert fill.cost_cents == -429 * 100 * 2  # BUY: cash out, signed
        assert fill.commission_cents == 0  # the venue's number, from the FILL
        assert fill.modeled is False, "a venue execution is not our model"
        posted = t.posted()[0]
        assert posted["symbol"] == OCC
        assert posted["type"] == "limit"
        assert posted["limit_price"] == "4.24"
        assert posted["client_order_id"] == intent().client_order_id

    def test_replay_returns_the_original_fill_without_resubmitting(self) -> None:
        """Cross-process idempotency: the deterministic coid is registered AT
        the venue, so a restarted process recovers the original fill from the
        broker itself — stronger than the in-memory model broker ever was."""
        t = FakeTransport()
        t.lookup = (200, order("filled", qty=2, avg="4.29"))
        fill = broker(t).execute(intent(), 420, 424, NOW)
        assert fill.premium_cents == 429
        assert t.posted() == [], "a replay must never submit a second order"

    def test_unfilled_order_is_cancelled_and_raises(self) -> None:
        t = FakeTransport()
        t.submit = (200, order("accepted"))
        t.final = (200, order("accepted"))  # never fills; also the post-cancel read
        with pytest.raises(AlpacaOrderNotFilled, match="not filled within"):
            broker(t, timeout=0).execute(intent(), 420, 424, NOW)
        assert any(m == "DELETE" for m, _, _ in t.calls), "timeout must cancel"

    def test_fill_landing_during_the_cancel_race_is_kept(self) -> None:
        t = FakeTransport()
        t.submit = (200, order("accepted"))
        t.final = (200, order("filled", qty=2, avg="4.30"))  # filled while cancelling
        fill = broker(t, timeout=0).execute(intent(), 420, 424, NOW)
        assert fill.premium_cents == 430, "a fill that landed mid-cancel must not be lost"

    def test_partial_fill_is_reported_never_absorbed(self) -> None:
        t = FakeTransport()
        t.lookup = (200, dict(order("filled", qty=1), filled_qty="1"))
        with pytest.raises(AlpacaOrderNotFilled, match="partial fill 1/2"):
            broker(t).execute(intent(contracts=2), 420, 424, NOW)

    def test_venue_rejection_is_loud(self) -> None:
        t = FakeTransport()
        t.submit = (403, {"message": "options level too low"})
        with pytest.raises(RuntimeError, match="submit failed"):
            broker(t).execute(intent(), 420, 424, NOW)


class TestUnmarkedExit:
    def test_force_flat_is_a_market_sell_at_the_venue(self) -> None:
        """With a REAL (paper) position at the venue, booking a synthetic
        zero-fill locally would fork the books. The venue flattens for real."""
        t = FakeTransport()
        t.submit = (200, order("accepted"))
        t.polls = [(200, order("filled", qty=2, avg="0.11"))]
        fill = broker(t).execute_unmarked_exit(intent(side="SELL"), NOW)
        posted = t.posted()[0]
        assert posted["type"] == "market"
        assert posted["side"] == "sell"
        assert fill.premium_cents == 11
        assert fill.cost_cents == 11 * 100 * 2  # SELL: cash in

    def test_venue_failure_raises_instead_of_faking_a_close(self) -> None:
        t = FakeTransport()
        t.submit = (500, {"message": "venue down"})
        with pytest.raises(RuntimeError, match="force-flat submit failed"):
            broker(t).execute_unmarked_exit(intent(side="SELL"), NOW)


class TestAccountCheck:
    def test_verify_returns_the_account_number(self) -> None:
        t = FakeTransport()
        t.final = (200, {"account_number": "PA3XYZ"})
        assert broker(t).verify_paper_account() == "PA3XYZ"

    def test_verify_fails_loud_on_auth_error(self) -> None:
        t = FakeTransport()
        t.final = (401, {"message": "unauthorized"})
        with pytest.raises(RuntimeError, match="account check failed"):
            broker(t).verify_paper_account()
