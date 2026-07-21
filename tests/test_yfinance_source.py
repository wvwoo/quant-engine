"""YFinanceSource — the network shell.

The pure row converters were tested (tests/test_provider_conversion.py); the
CLASS was not. Everything from line ~131 down — the cache path, the B4
availability check, bar/chain fetching and view assembly — measured 0%. The
yfinance Ticker is replaced by a stub, so no call leaves the process.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from qts_core.cache import DiskCache
from qts_core.clock import NY
from qts_core.providers.yfinance_source import YFinanceSource

SESSION = dt.date(2026, 6, 17)
NOW = dt.datetime(2026, 6, 17, 11, 0, tzinfo=NY)


class FakeFrame:
    """The slice of the pandas surface the provider actually touches."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.empty = not rows

    def iterrows(self):  # type: ignore[no-untyped-def]
        for r in self._rows:
            yield r.pop("__idx", None), r


class FakeChain:
    def __init__(self, calls: FakeFrame) -> None:
        self.calls = calls


class FakeTicker:
    def __init__(
        self,
        expiries: tuple[str, ...] = ("2026-06-17", "2026-06-19"),
        bars: list[dict[str, Any]] | None = None,
        chain_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.options = expiries
        self._bars = bars if bars is not None else _default_bars()
        self._chain_rows = chain_rows if chain_rows is not None else _default_chain()
        self.history_calls: list[dict[str, Any]] = []

    def history(self, **kwargs: Any) -> FakeFrame:
        self.history_calls.append(kwargs)
        return FakeFrame(list(self._bars))

    def option_chain(self, expiry: str) -> FakeChain:
        return FakeChain(FakeFrame(list(self._chain_rows)))


class _Idx:
    """Stands in for a pandas Timestamp index entry (bar OPEN time)."""

    def __init__(self, when: dt.datetime) -> None:
        self._when = when

    def to_pydatetime(self) -> dt.datetime:
        return self._when


def _default_bars() -> list[dict[str, Any]]:
    rows = []
    for i in range(4):
        open_t = dt.datetime(2026, 6, 17, 10, 0, tzinfo=NY) + dt.timedelta(minutes=5 * i)
        rows.append(
            {
                "__idx": _Idx(open_t),
                "Open": 200.0 + i,
                "High": 201.0 + i,
                "Low": 199.0 + i,
                "Close": 200.5 + i,
                "Volume": 1000.0 + i,
            }
        )
    # One bar still forming: its close is in the FUTURE of NOW and must be dropped.
    rows.append(
        {
            "__idx": _Idx(dt.datetime(2026, 6, 17, 12, 0, tzinfo=NY)),
            "Open": 210.0,
            "High": 211.0,
            "Low": 209.0,
            "Close": 210.5,
            "Volume": 500.0,
        }
    )
    return rows


def _default_chain() -> list[dict[str, Any]]:
    return [
        {
            "__idx": i,
            "strike": 200.0 + i,
            "bid": 4.20,
            "ask": 4.25,
            "volume": 100,
            "openInterest": 500,
            "impliedVolatility": 0.19,
        }
        for i in range(3)
    ]


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> YFinanceSource:
    ticker = FakeTicker()
    src = YFinanceSource("NVDA", cache=DiskCache(tmp_path / "cache"))
    monkeypatch.setattr(src, "_ticker", lambda: ticker)
    src._fake = ticker  # type: ignore[attr-defined]
    return src


class TestExpirations:
    def test_symbol_is_normalised(self) -> None:
        assert YFinanceSource("nvda").symbol == "NVDA"

    def test_expirations_are_parsed_as_dates(self, source: YFinanceSource) -> None:
        assert source.expirations() == [dt.date(2026, 6, 17), dt.date(2026, 6, 19)]

    def test_second_call_is_served_from_disk_cache(
        self, source: YFinanceSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert source.expirations(SESSION) == [dt.date(2026, 6, 17), dt.date(2026, 6, 19)]

        def explode():  # type: ignore[no-untyped-def]
            raise AssertionError("cached expiries were refetched")

        monkeypatch.setattr(source, "_ticker", explode)
        assert source.expirations(SESSION) == [dt.date(2026, 6, 17), dt.date(2026, 6, 19)]


class TestB4Gate:
    def test_same_day_expiry_present(self, source: YFinanceSource) -> None:
        assert source.has_same_day_expiry(SESSION) is True

    def test_same_day_expiry_absent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """NVDA lists Mon/Wed/Fri only — availability is CHECKED, never assumed."""
        src = YFinanceSource("NVDA", cache=DiskCache(tmp_path / "c"))
        monkeypatch.setattr(src, "_ticker", lambda: FakeTicker(expiries=("2026-06-19",)))
        assert src.has_same_day_expiry(SESSION) is False


class TestFetching:
    def test_in_progress_bar_is_dropped(self, source: YFinanceSource) -> None:
        bars = source.fetch_bars(now=NOW)
        assert len(bars) == 4, "a bar closing after now is a look-ahead leak"
        assert all(b.ts_close <= NOW for b in bars)

    def test_history_never_auto_adjusts(self, source: YFinanceSource) -> None:
        """auto_adjust back-applies future splits onto the past."""
        source.fetch_bars(now=NOW)
        assert source._fake.history_calls[0]["auto_adjust"] is False  # type: ignore[attr-defined]
        assert source._fake.history_calls[0]["prepost"] is False  # type: ignore[attr-defined]

    def test_chain_quotes_are_built(self, source: YFinanceSource) -> None:
        quotes = source.fetch_chain(session_date=SESSION, now=NOW, spot=201.0)
        assert len(quotes) == 3
        assert all(q.underlying == "NVDA" and q.expiry == SESSION for q in quotes)
        assert [q.strike_cents for q in quotes] == sorted(q.strike_cents for q in quotes)

    def test_empty_book_is_reported_not_crashed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        src = YFinanceSource("NVDA", cache=DiskCache(tmp_path / "c"))
        monkeypatch.setattr(src, "_ticker", lambda: FakeTicker(chain_rows=[]))
        assert src.fetch_chain(session_date=SESSION, now=NOW, spot=201.0) == []


class TestBuildView:
    def test_view_splits_session_from_prior_sessions(self, source: YFinanceSource) -> None:
        view = source.build_view(now=NOW, session_date=SESSION)
        assert len(view.bars) == 4
        assert view.prior_sessions == ()
        assert view.underlying_last == view.bars[-1].close
        assert view.meta["provider"] == "yfinance"
        assert view.meta["delayed"] == "true", "the delay must be declared, not implied"

    def test_prior_days_become_prior_sessions(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rows = []
        for day in (dt.date(2026, 6, 16), dt.date(2026, 6, 17)):
            for i in range(3):
                open_t = dt.datetime.combine(day, dt.time(10, 0), tzinfo=NY) + dt.timedelta(
                    minutes=5 * i
                )
                rows.append(
                    {
                        "__idx": _Idx(open_t),
                        "Open": 200.0,
                        "High": 201.0,
                        "Low": 199.0,
                        "Close": 200.5,
                        "Volume": 1000.0,
                    }
                )
        src = YFinanceSource("NVDA", cache=DiskCache(tmp_path / "c"))
        monkeypatch.setattr(src, "_ticker", lambda: FakeTicker(bars=rows))
        view = src.build_view(now=NOW, session_date=SESSION)
        assert len(view.bars) == 3
        assert len(view.prior_sessions) == 1
        assert len(view.prior_sessions[0]) == 3

    def test_no_session_bars_yields_no_chain(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With no spot there is nothing to centre a chain on; the view says so
        rather than inventing one."""
        src = YFinanceSource("NVDA", cache=DiskCache(tmp_path / "c"))
        monkeypatch.setattr(src, "_ticker", lambda: FakeTicker(bars=[]))
        view = src.build_view(now=NOW, session_date=SESSION)
        assert view.bars == ()
        assert view.chain == ()
        assert view.underlying_last is None
