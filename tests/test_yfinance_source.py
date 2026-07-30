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
from qts_core.config import StrategyConfig
from qts_core.providers import yfinance_source as yfs
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


class TestRequestIntervalIsValidatedAtUseNotImport:
    """A-02. QTS_MIN_REQUEST_INTERVAL_S was the ONE env var read at module
    import: `_LIMITER = RateLimiter(float(os.environ.get(...)))` at column 1.
    live.py and run_backtest.py both import this module unconditionally, so a
    value like "1,5" (a comma decimal separator — entirely plausible for an
    operator on an Arabic or European locale) raised a bare ValueError from
    provider internals before argparse, before require_paper_mode, before even
    `--help` could render. It also had zero test coverage."""

    def test_absent_falls_back_to_the_config_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("QTS_MIN_REQUEST_INTERVAL_S", raising=False)
        assert yfs.request_interval_s() == StrategyConfig().min_request_interval_s

    def test_empty_string_is_treated_as_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QTS_MIN_REQUEST_INTERVAL_S", "")
        assert yfs.request_interval_s() == StrategyConfig().min_request_interval_s

    @pytest.mark.parametrize(("raw", "expected"), [("0", 0.0), ("2", 2.0), ("0.25", 0.25)])
    def test_valid_values_parse(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
    ) -> None:
        monkeypatch.setenv("QTS_MIN_REQUEST_INTERVAL_S", raw)
        assert yfs.request_interval_s() == expected

    @pytest.mark.parametrize("raw", ["1,5", "abc", "1.2.3", "1s", "nan", "inf", "-1"])
    def test_bad_values_raise_a_named_error_naming_the_variable(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("QTS_MIN_REQUEST_INTERVAL_S", raw)
        with pytest.raises(yfs.InvalidRequestInterval) as exc:
            yfs.request_interval_s()
        msg = str(exc.value)
        assert "QTS_MIN_REQUEST_INTERVAL_S" in msg, "the operator must know WHICH var"
        assert raw in msg, "the operator must see what they actually set"

    def test_importing_the_module_with_a_bad_value_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression itself: import must survive a malformed value so the
        CLI can start, parse args and report the problem in its own words."""
        import importlib

        monkeypatch.setenv("QTS_MIN_REQUEST_INTERVAL_S", "1,5")
        importlib.reload(yfs)  # would raise ValueError before this fix
        assert yfs.YFinanceSource  # module usable

    def test_live_and_run_backtest_still_import_with_a_bad_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both entry points import the provider at top level; neither may die
        on an env typo before it can print a diagnosis."""
        import importlib

        monkeypatch.setenv("QTS_MIN_REQUEST_INTERVAL_S", "not-a-number")
        importlib.reload(yfs)
        import qts_core.live as _live
        import qts_core.run_backtest as _rb

        assert importlib.reload(_live).main
        assert importlib.reload(_rb).main

    def test_the_limiter_is_shared_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Yahoo throttles per client, so a per-source limiter would let a
        3-symbol run fire 3x the rate. Memoised, not per-instance."""
        monkeypatch.delenv("QTS_MIN_REQUEST_INTERVAL_S", raising=False)
        yfs.reset_limiter()
        assert yfs.limiter() is yfs.limiter()

    def test_a_bad_value_surfaces_when_the_limiter_is_built(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QTS_MIN_REQUEST_INTERVAL_S", "oops")
        yfs.reset_limiter()
        with pytest.raises(yfs.InvalidRequestInterval):
            yfs.limiter()


class TestDeadReExportIsGone:
    def test_ny_tz_alias_removed(self) -> None:
        """`NY_TZ = NY  # re-export for CLI convenience` had zero importers
        repo-wide; the comment asserted a consumer that did not exist. Removed
        before the Protocol extraction could enshrine it in the public surface."""
        assert not hasattr(yfs, "NY_TZ")
