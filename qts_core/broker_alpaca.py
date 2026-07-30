"""Alpaca PAPER broker bridge (ADR-012). Real order infrastructure, zero real money.

The base URL is a MODULE CONSTANT on purpose: it is not readable from config,
not readable from the environment, and not a constructor parameter. Aiming
this adapter at the live API requires editing reviewed source — a structural
impossibility, not a setting. That is the whole safety design, and it is
pinned by a test.

Key material never transits this codebase's logs: keys are read fail-closed
from the environment at construction and only ever placed in request headers.

Idempotency is the SAME contract the modeled PaperBroker honors, but stronger:
the deterministic client_order_id (uuid5 over strategy|session|occ|side|seq)
is registered WITH Alpaca, and execute() queries by_client_order_id BEFORE
submitting. A crashed-and-restarted process therefore recovers the ORIGINAL
fill from the broker itself — cross-process idempotency, which the in-memory
model broker could only approximate within one process.

Fills from here are NOT tagged modeled: the premium is a real NBBO execution
from Alpaca's paper simulator, not our Black-Scholes model. Commission is
taken from the fill (Alpaca paper charges none), never from config — the
ledger and the P&L must agree by construction (review F4).

ASSUMPTION (tagged per protocol): Alpaca options orders accept
time_in_force="day" only; order polling treats {"filled"} as terminal success
and {"canceled", "expired", "rejected"} as terminal failure.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from qts_core import secrets
from qts_core.broker import Fill
from qts_core.money import CONTRACT_MULTIPLIER
from qts_core.store import OrderIntent

# NOT configurable. See module docstring before touching this line.
ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"

# (method, path, json_body_or_None) -> (http_status, decoded_json)
Transport = Callable[[str, str, dict[str, Any] | None], tuple[int, dict[str, Any]]]


class AlpacaCredentialsMissing(RuntimeError):
    """APCA_API_KEY_ID / APCA_API_SECRET_KEY are absent from env AND the file."""


class AlpacaAccountRejected(RuntimeError):
    """The keys are PRESENT but the venue refused them (HTTP 401/403).

    A separate type from AlpacaCredentialsMissing on purpose (A-01). These are
    different operator problems with different fixes — "you have not pasted a
    key yet" versus "the key you pasted is wrong, revoked, or belongs to the
    LIVE dashboard" — and they used to be indistinguishable to callers: the
    absent path halted cleanly with rc 2 while a 401 raised a bare RuntimeError
    that slipped past `except AlpacaCredentialsMissing` in live.py and became an
    uncaught traceback. A rail that covers only the easy half is not a rail.
    """


class AlpacaUnreachable(RuntimeError):
    """The paper host could not be reached at all (DNS/socket/TLS failure).

    Also its own type: an offline laptop is neither a missing key nor a bad one,
    and telling the owner to check their key when the network is down sends them
    to the wrong place entirely.
    """


class AlpacaOrderNotFilled(RuntimeError):
    """The order did not fully fill within the poll window; it was cancelled.

    Raising (instead of inventing a fill) keeps the ledger honest: the intent
    stays INTENT, reconcile() retires it to REJECTED, and the next pass may
    try again. Partial fills also land here in v1 — a partial is REPORTED,
    never silently absorbed, because the ledger has no partial-leg concept."""


def _default_transport(key_id: str, secret: str) -> Transport:
    def request(method: str, path: str, body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(
            ALPACA_PAPER_BASE_URL + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "APCA-API-KEY-ID": key_id,
                "APCA-API-SECRET-KEY": secret,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:  # error bodies are still JSON
            return exc.code, json.loads(exc.read() or b"{}")
        except urllib.error.URLError as exc:
            # Not an HTTP answer at all: no DNS, no route, TLS refused. Naming
            # it here stops it escaping as an anonymous traceback three frames
            # up, where the operator would have no idea the network was the
            # cause (A-01).
            raise AlpacaUnreachable(
                f"alpaca paper host unreachable ({ALPACA_PAPER_BASE_URL}): {exc.reason}"
            ) from exc

    return request


def _cents(price: str | float | None) -> int:
    if price is None:
        return 0
    return round(float(price) * 100)


class AlpacaPaperBroker:
    """Implements the Broker protocol against Alpaca's PAPER endpoint."""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        poll_timeout_s: float = 10.0,
        poll_interval_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if transport is None:
            # Resolved through qts_core.secrets, NOT os.environ directly: a
            # LaunchAgent inherits none of the login shell environment, so an
            # env-only read made unattended operation impossible rather than
            # merely awkward. secrets.get() checks the environment first and
            # falls back to ~/.config/secrets.env (0600 enforced).
            key_id = secrets.get("APCA_API_KEY_ID", required=False)
            secret = secrets.get("APCA_API_SECRET_KEY", required=False)
            if not key_id or not secret:
                raise AlpacaCredentialsMissing(
                    "set APCA_API_KEY_ID and APCA_API_SECRET_KEY (PAPER account keys) "
                    f"in the environment or in {secrets.secrets_file_path()} (mode 0600) "
                    "— this codebase never stores them. Steps: KEY_ACQUISITION.md"
                )
            transport = _default_transport(key_id, secret)
        self._request = transport
        self._poll_timeout_s = poll_timeout_s
        self._poll_interval_s = poll_interval_s
        self._sleep = sleep

    # ------------------------------------------------------------------ api
    def verify_paper_account(self) -> str:
        """Boot check: the account answers on the PAPER host. Returns its id.

        Every message here passes through secrets.redact() because a venue is
        free to echo the offending key back inside its own error body, and an
        error path is exactly where nobody is watching for a leak.
        """
        status, body = self._request("GET", "/v2/account", None)
        if status in (401, 403):
            raise AlpacaAccountRejected(
                secrets.redact(
                    f"alpaca REJECTED these keys (HTTP {status}). The keys are present but "
                    "not accepted. Check they were generated on the PAPER dashboard "
                    "(app.alpaca.markets/paper/...) and not the live one, and that they have "
                    f"not been regenerated since. Venue said: {body}"
                )
            )
        if status != 200:
            raise RuntimeError(
                secrets.redact(f"alpaca paper account check failed: HTTP {status} {body}")
            )
        return str(body.get("account_number", "?"))

    def execute(
        self, intent: OrderIntent, bid_cents: int, ask_cents: int, now: dt.datetime
    ) -> Fill:
        del bid_cents, ask_cents  # the venue's book decides; ours is advisory
        existing = self._find_by_client_order_id(intent.client_order_id)
        if existing is not None:
            return self._fill_from_order(existing, intent, now)
        status, order = self._request(
            "POST",
            "/v2/orders",
            {
                "symbol": intent.occ_symbol,
                "qty": str(intent.contracts),
                "side": "buy" if intent.side == "BUY" else "sell",
                "type": "limit",
                "limit_price": f"{intent.limit_cents / 100:.2f}",
                "time_in_force": "day",  # ASSUMPTION: options accept day only
                "client_order_id": intent.client_order_id,
            },
        )
        if status not in (200, 201):
            raise RuntimeError(f"alpaca order submit failed: HTTP {status} {order}")
        return self._await_full_fill(order, intent, now)

    def execute_unmarked_exit(self, intent: OrderIntent, now: dt.datetime) -> Fill:
        """Force-flat without a usable local mark: a MARKET sell at the venue.

        Unlike the modeled broker there is a REAL (paper) position at Alpaca,
        so booking a synthetic zero-proceeds fill locally would make the two
        books diverge. The venue is asked to flatten for real; if IT fails,
        we raise and the caller retries next pass — an honest open position
        beats a fictional closed one."""
        existing = self._find_by_client_order_id(intent.client_order_id)
        if existing is not None:
            return self._fill_from_order(existing, intent, now)
        status, order = self._request(
            "POST",
            "/v2/orders",
            {
                "symbol": intent.occ_symbol,
                "qty": str(intent.contracts),
                "side": "sell",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": intent.client_order_id,
            },
        )
        if status not in (200, 201):
            raise RuntimeError(f"alpaca force-flat submit failed: HTTP {status} {order}")
        return self._await_full_fill(order, intent, now)

    # ------------------------------------------------------------- internals
    def _find_by_client_order_id(self, coid: str) -> dict[str, Any] | None:
        status, body = self._request(
            "GET", f"/v2/orders:by_client_order_id?client_order_id={coid}", None
        )
        if status == 200 and body.get("id"):
            return body
        return None

    def _await_full_fill(
        self, order: dict[str, Any], intent: OrderIntent, now: dt.datetime
    ) -> Fill:
        deadline = time.monotonic() + self._poll_timeout_s
        current = order
        while True:
            state = str(current.get("status", ""))
            if state == "filled":
                return self._fill_from_order(current, intent, now)
            if state in ("canceled", "expired", "rejected"):
                raise AlpacaOrderNotFilled(
                    f"{intent.occ_symbol} {intent.side}: terminal status {state!r}"
                )
            if time.monotonic() >= deadline:
                break
            self._sleep(self._poll_interval_s)
            _, current = self._request("GET", f"/v2/orders/{order['id']}", None)
        # Timed out: cancel, then read the FINAL state — the fill may have
        # landed during the cancel race, and that fill must not be lost.
        self._request("DELETE", f"/v2/orders/{order['id']}", None)
        _, final = self._request("GET", f"/v2/orders/{order['id']}", None)
        if str(final.get("status", "")) == "filled":
            return self._fill_from_order(final, intent, now)
        filled_qty = int(float(final.get("filled_qty") or 0))
        raise AlpacaOrderNotFilled(
            f"{intent.occ_symbol} {intent.side}: not filled within "
            f"{self._poll_timeout_s:.0f}s (filled {filled_qty}/{intent.contracts}); cancelled"
        )

    def _fill_from_order(
        self, order: dict[str, Any], intent: OrderIntent, now: dt.datetime
    ) -> Fill:
        if str(order.get("status", "")) != "filled":
            raise AlpacaOrderNotFilled(
                f"{intent.occ_symbol}: replayed order exists but status is "
                f"{order.get('status')!r}, not filled"
            )
        filled_qty = int(float(order.get("filled_qty") or 0))
        if filled_qty != intent.contracts:
            raise AlpacaOrderNotFilled(
                f"{intent.occ_symbol}: partial fill {filled_qty}/{intent.contracts} — "
                "v1 requires full fills; resolve at the broker"
            )
        premium = _cents(order.get("filled_avg_price"))
        sign = -1 if intent.side == "BUY" else 1
        return Fill(
            client_order_id=intent.client_order_id,
            premium_cents=premium,
            cost_cents=sign * premium * CONTRACT_MULTIPLIER * intent.contracts,
            commission_cents=0,  # Alpaca paper charges none; from the FILL, not config
            filled_at=now,
            modeled=False,  # real NBBO execution at the paper venue — not our model
        )
