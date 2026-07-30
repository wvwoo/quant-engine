"""AlpacaPaperBroker (ADR-012) — every path through a fake transport, no wire.

The safety property under test is structural: the base URL is a module
constant pointing at the PAPER host, unreachable from config or environment.
Changing it requires editing reviewed source AND this test — a two-file,
two-eyes change by construction.
"""

from __future__ import annotations

import datetime as dt
import email.message
import io
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from qts_core import secrets as sec
from qts_core.broker_alpaca import (
    ALPACA_PAPER_BASE_URL,
    AlpacaAccountRejected,
    AlpacaCredentialsMissing,
    AlpacaOrderNotFilled,
    AlpacaPaperBroker,
    AlpacaUnreachable,
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
        """CHANGED for A-01, deliberately and in the STRICTER direction.

        This test previously asserted only `RuntimeError, match="account check
        failed"` for a 401. That message is now reserved for non-auth failures,
        because collapsing "your key is wrong" into the same string as "the
        venue returned a 500" is what let live.py mis-handle the auth case in
        the first place. The assertion below is narrower than the one it
        replaces (a specific subclass, not bare RuntimeError), so nothing was
        weakened to make a change pass. Non-auth statuses keep the old contract
        and are asserted in TestRejectedKeyIsItsOwnFailure.
        """
        t = FakeTransport()
        t.final = (401, {"message": "unauthorized"})
        with pytest.raises(AlpacaAccountRejected, match="REJECTED"):
            broker(t).verify_paper_account()


class TestRejectedKeyIsItsOwnFailure:
    """A-01. A key that is PRESENT but REJECTED is a different operator problem
    from one that is ABSENT, and the two used to be indistinguishable: the
    absent path halted cleanly (rc 2) while 401 raised a bare RuntimeError that
    escaped live.py's `except AlpacaCredentialsMissing` and became a traceback."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_status_raises_the_named_rejection_type(self, status: int) -> None:
        t = FakeTransport()
        t.final = (status, {"message": "forbidden"})
        with pytest.raises(AlpacaAccountRejected) as exc:
            broker(t).verify_paper_account()
        assert str(status) in str(exc.value)

    def test_rejection_and_missing_are_not_catchable_as_one_another(self) -> None:
        assert not issubclass(AlpacaAccountRejected, AlpacaCredentialsMissing)
        assert not issubclass(AlpacaCredentialsMissing, AlpacaAccountRejected)

    def test_rejection_message_points_at_the_paper_dashboard(self) -> None:
        t = FakeTransport()
        t.final = (401, {"message": "unauthorized"})
        with pytest.raises(AlpacaAccountRejected) as exc:
            broker(t).verify_paper_account()
        msg = str(exc.value).lower()
        assert "rejected" in msg
        assert "paper" in msg

    def test_a_non_auth_failure_stays_a_plain_check_failure(self) -> None:
        """500 is the venue's problem, not the key's — do not misdiagnose it."""
        t = FakeTransport()
        t.final = (500, {"message": "boom"})
        with pytest.raises(RuntimeError, match="account check failed") as exc:
            broker(t).verify_paper_account()
        assert not isinstance(exc.value, AlpacaAccountRejected)

    def test_the_error_body_is_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A venue that echoes the key back in an error body must not leak it."""
        marked = "PKZZLEAKCANARY000001"
        monkeypatch.setenv("APCA_API_KEY_ID", marked)
        sec.get("APCA_API_KEY_ID")  # registers it for redaction
        t = FakeTransport()
        t.final = (401, {"message": f"key {marked} is not authorized"})
        with pytest.raises(AlpacaAccountRejected) as exc:
            broker(t).verify_paper_account()
        assert marked not in str(exc.value)
        assert sec.REDACTED in str(exc.value)


class TestUnreachableHostIsItsOwnFailure:
    def test_default_transport_url_error_becomes_a_named_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Network loss at boot must not surface as an anonymous traceback.

        The translation lives in the DEFAULT transport (the only one that owns a
        socket), so this exercises that real path by failing urlopen itself —
        not by injecting a fake transport, which would test nothing that ships.
        """
        monkeypatch.setenv("APCA_API_KEY_ID", "keyid00000001")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "secret00000001")

        def dead(req: Any, timeout: float = 0) -> Any:
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(urllib.request, "urlopen", dead)
        with pytest.raises(AlpacaUnreachable, match="unreachable"):
            AlpacaPaperBroker().verify_paper_account()

    def test_an_http_error_is_still_translated_to_a_status_not_an_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTPError carries a real status, so it must NOT become Unreachable."""
        monkeypatch.setenv("APCA_API_KEY_ID", "keyid00000001")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "secret00000001")

        def http_401(req: Any, timeout: float = 0) -> Any:
            raise urllib.error.HTTPError(
                ALPACA_PAPER_BASE_URL,
                401,
                "Unauthorized",
                email.message.Message(),
                io.BytesIO(b'{"m":"no"}'),
            )

        monkeypatch.setattr(urllib.request, "urlopen", http_401)
        with pytest.raises(AlpacaAccountRejected):
            AlpacaPaperBroker().verify_paper_account()

    def test_unreachable_is_distinct_from_rejected_and_missing(self) -> None:
        assert not issubclass(AlpacaUnreachable, AlpacaAccountRejected)
        assert not issubclass(AlpacaUnreachable, AlpacaCredentialsMissing)


class TestCredentialsComeFromTheSecretsLoader:
    """Keys must resolve through qts_core.secrets so an unattended LaunchAgent —
    which inherits none of the login shell environment — can authenticate."""

    def test_keys_are_sourced_from_the_secrets_file_when_env_is_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        f = tmp_path / "s.env"
        f.write_text("APCA_API_KEY_ID=fileKeyId0001\nAPCA_API_SECRET_KEY=fileSecret0001\n")
        f.chmod(0o600)
        monkeypatch.setenv("QTS_SECRETS_FILE", str(f))
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)

        seen: dict[str, str] = {}

        class Sentinel(Exception):
            pass

        def capture(req: Any, timeout: float = 0) -> Any:
            seen.update({k.lower(): v for k, v in req.headers.items()})
            raise Sentinel

        monkeypatch.setattr(urllib.request, "urlopen", capture)
        with pytest.raises(Sentinel):
            AlpacaPaperBroker().verify_paper_account()
        # The file's values reached the wire headers — nothing else could have.
        assert seen["Apca-api-key-id".lower()] == "fileKeyId0001"
        assert seen["Apca-api-secret-key".lower()] == "fileSecret0001"

    def test_still_fails_closed_when_neither_env_nor_file_has_keys(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("QTS_SECRETS_FILE", str(tmp_path / "absent.env"))
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
        with pytest.raises(AlpacaCredentialsMissing, match="PAPER account keys"):
            AlpacaPaperBroker()

    def test_an_insecure_secrets_file_is_refused_not_silently_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        f = tmp_path / "s.env"
        f.write_text("APCA_API_KEY_ID=x000000000000\nAPCA_API_SECRET_KEY=y000000000000\n")
        f.chmod(0o644)
        monkeypatch.setenv("QTS_SECRETS_FILE", str(f))
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
        with pytest.raises(sec.SecretsFileInsecure):
            AlpacaPaperBroker()
