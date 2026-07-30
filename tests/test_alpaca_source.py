"""AlpacaDataSource — every path through a fake transport, no wire.

Mirrors tests/test_broker_alpaca.py deliberately: same injectable-transport
shape, same offline coverage of the failure statuses that actually happen to
operators (401, 403, 429, malformed body, empty chain). The data host is a
module constant and this class has no order path, so a data client cannot be
turned into a trading client by editing a setting.
"""

from __future__ import annotations

import datetime as dt
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from qts_core.broker_alpaca import (
    AlpacaAccountRejected,
    AlpacaCredentialsMissing,
    AlpacaUnreachable,
)
from qts_core.cache import DiskCache
from qts_core.clock import NY
from qts_core.config import StrategyConfig
from qts_core.providers import OptionChainSource, registry
from qts_core.providers.alpaca_source import (
    ALPACA_DATA_BASE_URL,
    AlpacaBadResponse,
    AlpacaDataSource,
    AlpacaRateLimited,
    expiry_from_occ,
)

SESSION = dt.date(2026, 7, 22)
NOW = dt.datetime(2026, 7, 22, 11, 0, tzinfo=NY)
CFG = StrategyConfig()


class FakeTransport:
    """Scripted data API. Records paths; answers per route."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.snapshots: tuple[int, dict[str, Any]] = (200, {"snapshots": {}})
        self.bars: tuple[int, dict[str, Any]] = (200, {"bars": []})

    def __call__(self, method: str, path: str) -> tuple[int, dict[str, Any]]:
        self.paths.append(path)
        if "/options/snapshots/" in path:
            return self.snapshots
        return self.bars


def src(t: FakeTransport, tmp_path: Path) -> AlpacaDataSource:
    return AlpacaDataSource("SPY", cache=DiskCache(tmp_path / "c"), cfg=CFG, transport=t)


def _bar(ts: str, c: float = 500.0, v: float = 1000.0) -> dict[str, Any]:
    return {"t": ts, "o": c - 1, "h": c + 1, "l": c - 2, "c": c, "v": v}


def _snap(bid: float = 4.20, ask: float = 4.25, oi: int = 500, vol: float = 100) -> dict[str, Any]:
    return {
        "latestQuote": {"bp": bid, "ap": ask},
        "dailyBar": {"v": vol},
        "openInterest": oi,
    }


class TestStructuralSafety:
    def test_data_host_is_a_constant_and_is_not_the_trading_host(self) -> None:
        """Two-file change by design, like the broker's paper host."""
        assert ALPACA_DATA_BASE_URL == "https://data.alpaca.markets"
        assert "paper-api" not in ALPACA_DATA_BASE_URL

    def test_the_module_has_no_order_placing_path(self) -> None:
        """A market-data client must be structurally incapable of trading, not
        merely uninterested in it."""
        import inspect

        from qts_core.providers import alpaca_source

        source = inspect.getsource(alpaca_source)
        assert "/v2/orders" not in source
        assert '"POST"' not in source
        assert "urlencode" in source  # it builds GET queries and nothing else

    def test_every_request_this_source_makes_is_a_get(self, tmp_path: Path) -> None:
        class Recorder(FakeTransport):
            def __init__(self) -> None:
                super().__init__()
                self.methods: list[str] = []

            def __call__(self, method: str, path: str) -> tuple[int, dict[str, Any]]:
                self.methods.append(method)
                return super().__call__(method, path)

        t = Recorder()
        t.bars = (200, {"bars": [_bar("2026-07-22T14:30:00Z")]})
        s = src(t, tmp_path)
        s.fetch_bars(now=NOW)
        s.has_same_day_expiry(SESSION)
        assert set(t.methods) == {"GET"}

    def test_no_greeks_are_read_from_the_venue(self, tmp_path: Path) -> None:
        """ADR-002. Even offered, a vendor delta is not ours and is not used."""
        t = FakeTransport()
        t.snapshots = (
            200,
            {
                "snapshots": {
                    "SPY260722C00500000": {
                        **_snap(),
                        "greeks": {"delta": 0.99},
                        "impliedVolatility": 0.5,
                    }
                }
            },
        )
        quotes = src(t, tmp_path).fetch_chain(session_date=SESSION, now=NOW, spot=500.0)
        assert len(quotes) == 1
        assert quotes[0].iv_hint is None, "a vendor IV must not enter the view"


class TestProtocolConformance:
    def test_satisfies_the_protocol(self, tmp_path: Path) -> None:
        assert isinstance(src(FakeTransport(), tmp_path), OptionChainSource)

    def test_symbol_is_normalised(self, tmp_path: Path) -> None:
        s = AlpacaDataSource("spy", cache=DiskCache(tmp_path / "c"), transport=FakeTransport())
        assert s.symbol == "SPY"


class TestCredentials:
    def test_missing_keys_fail_closed_with_the_file_and_the_doc(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
        with pytest.raises(AlpacaCredentialsMissing) as exc:
            AlpacaDataSource("SPY")
        msg = str(exc.value)
        assert "KEY_ACQUISITION.md" in msg
        assert "--provider alpaca" in msg

    def test_keys_reach_the_headers_from_the_secrets_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        f = tmp_path / "s.env"
        f.write_text("APCA_API_KEY_ID=dataKeyId001\nAPCA_API_SECRET_KEY=dataSecret001\n")
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
            AlpacaDataSource("SPY").has_same_day_expiry(SESSION)
        assert seen["apca-api-key-id"] == "dataKeyId001"
        assert seen["apca-api-secret-key"] == "dataSecret001"


class TestFailureStatuses:
    def test_401_is_a_named_rejection(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (401, {"message": "unauthorized"})
        with pytest.raises(AlpacaAccountRejected, match="REJECTED"):
            src(t, tmp_path).has_same_day_expiry(SESSION)

    def test_403_is_a_named_rejection(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (403, {"message": "forbidden"})
        with pytest.raises(AlpacaAccountRejected):
            src(t, tmp_path).has_same_day_expiry(SESSION)

    def test_429_names_the_rate_limit_and_the_knob_that_fixes_it(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (429, {"message": "too many requests"})
        with pytest.raises(AlpacaRateLimited) as exc:
            src(t, tmp_path).has_same_day_expiry(SESSION)
        assert "QTS_MIN_REQUEST_INTERVAL_S" in str(exc.value)

    def test_other_non_200_is_a_bad_response(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (503, {"message": "maintenance"})
        with pytest.raises(AlpacaBadResponse, match="503"):
            src(t, tmp_path).has_same_day_expiry(SESSION)

    def test_a_rejection_body_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from qts_core import secrets as sec

        marked = "PKZZDATACANARY00001"
        monkeypatch.setenv("APCA_API_KEY_ID", marked)
        sec.get("APCA_API_KEY_ID")
        t = FakeTransport()
        t.snapshots = (401, {"message": f"key {marked} bad"})
        with pytest.raises(AlpacaAccountRejected) as exc:
            src(t, tmp_path).has_same_day_expiry(SESSION)
        assert marked not in str(exc.value)

    def test_malformed_snapshots_payload_is_named_not_a_typeerror(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (200, {"snapshots": ["not", "an", "object"]})
        with pytest.raises(AlpacaBadResponse, match="not an object"):
            src(t, tmp_path).has_same_day_expiry(SESSION)

    def test_malformed_bars_payload_is_named(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.bars = (200, {"bars": {"unexpected": "shape"}})
        with pytest.raises(AlpacaBadResponse, match="not a list"):
            src(t, tmp_path).fetch_bars(now=NOW)

    def test_network_loss_is_unreachable_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APCA_API_KEY_ID", "keyid00000001")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "secret00000001")

        def dead(req: Any, timeout: float = 0) -> Any:
            raise urllib.error.URLError("no route")

        monkeypatch.setattr(urllib.request, "urlopen", dead)
        with pytest.raises(AlpacaUnreachable, match="data host unreachable"):
            AlpacaDataSource("SPY").has_same_day_expiry(SESSION)


class TestB4Gate:
    def test_empty_snapshots_means_no_same_day_expiry(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (200, {"snapshots": {}})
        assert src(t, tmp_path).has_same_day_expiry(SESSION) is False

    def test_missing_snapshots_key_is_treated_as_empty_not_an_error(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (200, {})
        assert src(t, tmp_path).has_same_day_expiry(SESSION) is False

    def test_a_listed_expiry_is_detected(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (200, {"snapshots": {"SPY260722C00500000": _snap()}})
        assert src(t, tmp_path).has_same_day_expiry(SESSION) is True

    def test_b4_asks_for_one_exact_date_not_a_window(self, tmp_path: Path) -> None:
        """Cheaper and more reliable than parsing OCC keys over a range — and
        B4 is a blocking gate that runs for every symbol every day."""
        t = FakeTransport()
        t.snapshots = (200, {"snapshots": {"SPY260722C00500000": _snap()}})
        src(t, tmp_path).has_same_day_expiry(SESSION)
        assert "expiration_date=2026-07-22" in t.paths[0]
        assert "expiration_date_gte" not in t.paths[0]

    def test_the_free_feed_is_requested_explicitly(self, tmp_path: Path) -> None:
        """The documented server-side default is the PAID opra feed, so relying
        on it would fail confusingly on a free account."""
        t = FakeTransport()
        src(t, tmp_path).has_same_day_expiry(SESSION)
        assert "feed=indicative" in t.paths[0]


class TestOccParsing:
    @pytest.mark.parametrize(
        ("occ", "expected"),
        [
            ("SPY260722C00743000", dt.date(2026, 7, 22)),
            ("NVDA260619P00120500", dt.date(2026, 6, 19)),
        ],
    )
    def test_expiry_is_derived_from_the_occ_symbol(self, occ: str, expected: dt.date) -> None:
        assert expiry_from_occ(occ) == expected

    @pytest.mark.parametrize("bad", ["", "SPY", "GARBAGE", "SPY999999C00743000"])
    def test_unparseable_keys_return_none_rather_than_raising(self, bad: str) -> None:
        """One malformed key in a large snapshot must not cost the whole chain."""
        assert expiry_from_occ(bad) is None

    def test_expirations_are_derived_and_deduplicated(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (
            200,
            {
                "snapshots": {
                    "SPY260722C00500000": _snap(),
                    "SPY260722P00500000": _snap(),
                    "SPY260724C00500000": _snap(),
                    "JUNK": _snap(),
                }
            },
        )
        assert src(t, tmp_path).expirations() == [dt.date(2026, 7, 22), dt.date(2026, 7, 24)]

    def test_expirations_with_a_session_date_are_disk_cached(self, tmp_path: Path) -> None:
        """Same two-behaviours-behind-one-argument contract as the incumbent."""
        t = FakeTransport()
        t.snapshots = (200, {"snapshots": {"SPY260722C00500000": _snap()}})
        s = src(t, tmp_path)
        assert s.expirations(SESSION) == [SESSION]
        before = len(t.paths)
        assert s.expirations(SESSION) == [SESSION]
        assert len(t.paths) == before, "a cached expiry list was refetched"


class TestChain:
    def test_calls_only_puts_are_dropped(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (
            200,
            {
                "snapshots": {
                    "SPY260722C00500000": _snap(),
                    "SPY260722P00500000": _snap(),
                }
            },
        )
        quotes = src(t, tmp_path).fetch_chain(session_date=SESSION, now=NOW, spot=500.0)
        assert [q.right for q in quotes] == ["C"]

    def test_strike_is_decoded_from_the_occ_symbol(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (200, {"snapshots": {"SPY260722C00743000": _snap()}})
        quotes = src(t, tmp_path).fetch_chain(session_date=SESSION, now=NOW, spot=743.0)
        assert quotes[0].strike_cents == 74_300

    def test_quotes_carry_integer_cents(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (200, {"snapshots": {"SPY260722C00500000": _snap(bid=4.20, ask=4.25)}})
        q = src(t, tmp_path).fetch_chain(session_date=SESSION, now=NOW, spot=500.0)[0]
        assert (q.bid_cents, q.ask_cents) == (420, 425)

    def test_a_quoteless_snapshot_still_becomes_a_no_market_quote(self, tmp_path: Path) -> None:
        """Reporting 'no market' beats silently losing the strike."""
        t = FakeTransport()
        t.snapshots = (200, {"snapshots": {"SPY260722C00500000": {"openInterest": 1}}})
        q = src(t, tmp_path).fetch_chain(session_date=SESSION, now=NOW, spot=500.0)[0]
        assert (q.bid_cents, q.ask_cents) == (0, 0)

    def test_empty_chain_is_an_empty_list_not_a_crash(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (200, {"snapshots": {}})
        assert src(t, tmp_path).fetch_chain(session_date=SESSION, now=NOW, spot=500.0) == []

    def test_a_non_dict_snapshot_entry_is_skipped(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.snapshots = (200, {"snapshots": {"SPY260722C00500000": "garbage"}})
        assert src(t, tmp_path).fetch_chain(session_date=SESSION, now=NOW, spot=500.0) == []


class TestBars:
    def test_bars_are_converted_and_the_forming_bar_is_dropped(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.bars = (
            200,
            {
                "bars": [
                    _bar("2026-07-22T14:30:00Z"),  # 10:30 ET open -> 10:35 close
                    _bar("2026-07-22T18:00:00Z"),  # 14:00 ET -> closes after NOW
                ]
            },
        )
        bars = src(t, tmp_path).fetch_bars(now=NOW)
        assert len(bars) == 1, "a bar closing after now is a look-ahead leak"

    def test_raw_adjustment_is_requested(self, tmp_path: Path) -> None:
        """Adjusted history back-applies future splits onto the past."""
        t = FakeTransport()
        src(t, tmp_path).fetch_bars(now=NOW)
        assert "adjustment=raw" in t.paths[0]

    def test_the_free_equity_feed_is_requested_explicitly(self, tmp_path: Path) -> None:
        t = FakeTransport()
        src(t, tmp_path).fetch_bars(now=NOW)
        assert "feed=iex" in t.paths[0]

    def test_the_interval_is_passed_as_alpacas_timeframe(self, tmp_path: Path) -> None:
        t = FakeTransport()
        src(t, tmp_path).fetch_bars(now=NOW, interval_min=5)
        assert "timeframe=5Min" in t.paths[0]

    def test_one_malformed_row_costs_that_row_only(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.bars = (
            200,
            {
                "bars": [
                    {"t": "not-a-time", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
                    _bar("2026-07-22T14:30:00Z"),
                    {"no_t_field": True},
                ]
            },
        )
        assert len(src(t, tmp_path).fetch_bars(now=NOW)) == 1

    def test_missing_bars_key_is_empty_not_an_error(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.bars = (200, {})
        assert src(t, tmp_path).fetch_bars(now=NOW) == []


class TestBuildView:
    def test_view_declares_provider_feed_and_delay(self, tmp_path: Path) -> None:
        """The honesty guard: a derived, delayed feed must never be
        indistinguishable from a real-time one."""
        t = FakeTransport()
        t.bars = (200, {"bars": [_bar("2026-07-22T14:30:00Z", c=500.0)]})
        t.snapshots = (200, {"snapshots": {"SPY260722C00500000": _snap()}})
        view = src(t, tmp_path).build_view(now=NOW, session_date=SESSION)
        assert view.meta["provider"] == "alpaca"
        assert view.meta["feed"] == "alpaca-indicative"
        assert view.meta["delayed"] == "true"
        registry.assert_meta_is_honest(dict(view.meta))

    def test_session_and_prior_bars_are_split(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.bars = (
            200,
            {
                "bars": [
                    _bar("2026-07-21T14:30:00Z", c=498.0),
                    _bar("2026-07-22T14:30:00Z", c=500.0),
                ]
            },
        )
        t.snapshots = (200, {"snapshots": {}})
        view = src(t, tmp_path).build_view(now=NOW, session_date=SESSION)
        assert len(view.bars) == 1
        assert len(view.prior_sessions) == 1
        assert view.underlying_last == 500.0

    def test_no_bars_means_no_spot_and_no_chain(self, tmp_path: Path) -> None:
        t = FakeTransport()
        t.bars = (200, {"bars": []})
        view = src(t, tmp_path).build_view(now=NOW, session_date=SESSION)
        assert view.bars == ()
        assert view.chain == ()
        assert view.underlying_last is None
