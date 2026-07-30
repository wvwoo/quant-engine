"""qts doctor — every branch, offline.

Two properties are load-bearing and both are asserted here:
  1. NEVER prints a secret. Not the value, not a prefix.
  2. OFFLINE IS A RESULT. With no network the provider checks SKIP and say why;
     they do not fail, because a readiness tool that cannot run on a train is a
     readiness tool nobody runs.

The new module also has to clear ci.sh's 85% per-file coverage floor in the same
change that introduces it — a gate added precisely because api.py, live.py and
run_backtest.py once sat at exactly 0.0%.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from qts_core import doctor
from qts_core.clock import NY
from qts_core.config import StrategyConfig
from qts_core.store import StateStore

CFG = StrategyConfig()
SESSION = dt.date(2026, 6, 17)
NOW = dt.datetime(2026, 6, 17, 10, 15, tzinfo=NY)
MARKED = "PKZZDOCTORCANARY0001"


def _account(level: int = 2, number: str = "PA3TESTACCT") -> dict[str, Any]:
    return {
        "account_number": number,
        "status": "ACTIVE",
        "options_approved_level": level,
        "options_trading_level": level,
    }


class TestVerdictArithmetic:
    def test_all_ok_is_ready_exit_zero(self) -> None:
        rep = doctor.Report()
        rep.add("a", "OK", "fine")
        rep.add("b", "INFO", "noted")
        assert (rep.verdict, rep.exit_code) == ("READY", 0)

    def test_a_warn_is_exit_one(self) -> None:
        rep = doctor.Report()
        rep.add("a", "OK", "fine")
        rep.add("b", "WARN", "hmm")
        assert (rep.verdict, rep.exit_code) == ("WARN", 1)

    def test_a_skip_is_a_warn_not_a_pass(self) -> None:
        """An unchecked precondition must not read as a checked one."""
        rep = doctor.Report()
        rep.add("net", "SKIP", "offline")
        assert rep.exit_code == 1

    def test_a_block_dominates_everything(self) -> None:
        rep = doctor.Report()
        rep.add("a", "OK", "fine")
        rep.add("b", "WARN", "hmm")
        rep.add("c", "BLOCK", "no")
        assert (rep.verdict, rep.exit_code) == ("BLOCK", 2)

    def test_an_empty_report_is_ready(self) -> None:
        assert doctor.Report().exit_code == 0


class TestRedaction:
    def test_a_secret_never_reaches_a_check_detail(self) -> None:
        from qts_core import secrets as sec

        sec.reset_registry()
        sec._register(MARKED)
        rep = doctor.Report()
        rep.add("leaky", "BLOCK", f"venue said {MARKED} is bad")
        assert MARKED not in rep.checks[0].detail
        assert MARKED not in rep.render()
        assert MARKED not in rep.to_json()

    def test_the_key_check_reports_presence_not_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APCA_API_KEY_ID", MARKED)
        monkeypatch.setenv("APCA_API_SECRET_KEY", MARKED + "S")
        rep = doctor.Report()
        doctor.check_keys(rep, offline=True)
        text = rep.render()
        assert "present" in text
        assert MARKED not in text
        assert MARKED[:6] not in text, "not even a prefix may leak"


class TestOfflineIsAResult:
    def test_offline_skips_the_fetch_and_says_so(self) -> None:
        rep = doctor.Report()
        doctor.check_provider(rep, CFG, offline=True, name="yfinance")
        details = {c.name: c for c in rep.checks}
        assert details["provider"].status == "OK"
        assert details["provider fetch"].status == "SKIP"
        assert "offline" in details["provider fetch"].detail.lower()

    def test_offline_skips_live_key_verification(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APCA_API_KEY_ID", "keyid00000001")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "secret00000001")
        rep = doctor.Report()
        doctor.check_keys(rep, offline=True)
        acct = next(c for c in rep.checks if c.name == "alpaca account")
        assert acct.status == "SKIP"

    def test_an_unknown_provider_blocks_and_lists_the_real_ones(self) -> None:
        rep = doctor.Report()
        doctor.check_provider(rep, CFG, offline=True, name="bloomberg")
        c = rep.checks[0]
        assert c.status == "BLOCK"
        assert "yfinance" in c.fix


class TestKeyBranches:
    def test_absent_keys_are_info_not_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The default backend is 'model'; no key is needed to trade paper."""
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
        rep = doctor.Report()
        doctor.check_keys(rep, offline=True)
        assert all(c.status in ("INFO", "SKIP") for c in rep.checks)
        assert rep.exit_code <= 1

    def test_an_implausible_key_warns_with_the_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APCA_API_KEY_ID", '"quoted-paste"')
        monkeypatch.setenv("APCA_API_SECRET_KEY", "secret00000001")
        rep = doctor.Report()
        doctor.check_keys(rep, offline=True)
        bad = next(c for c in rep.checks if c.name.endswith("KEY_ID"))
        assert bad.status == "WARN"
        assert "quote" in bad.detail.lower()

    def test_a_live_accepted_key_reports_ok_and_masks_the_account(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APCA_API_KEY_ID", "keyid00000001")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "secret00000001")
        rep = doctor.Report()
        doctor.check_keys(rep, offline=False, verify=lambda: _account())
        acct = next(c for c in rep.checks if c.name == "alpaca account")
        assert acct.status == "OK"
        assert "…ACCT" in acct.detail
        assert "PA3TESTACCT" not in acct.detail, "the full account number is not printed"

    def test_a_rejected_key_blocks_with_a_pointer_to_the_paper_dashboard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APCA_API_KEY_ID", "keyid00000001")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "secret00000001")

        def boom() -> dict[str, Any]:
            raise RuntimeError("HTTP 401: unauthorized")

        rep = doctor.Report()
        doctor.check_keys(rep, offline=False, verify=boom)
        acct = next(c for c in rep.checks if c.name == "alpaca account")
        assert acct.status == "BLOCK"
        assert "PAPER" in acct.fix

    def test_a_live_account_number_that_is_not_paper_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keys generated on the LIVE dashboard are the likeliest owner mistake."""
        monkeypatch.setenv("APCA_API_KEY_ID", "keyid00000001")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "secret00000001")
        rep = doctor.Report()
        doctor.check_keys(rep, offline=False, verify=lambda: _account(number="123456789"))
        acct = next(c for c in rep.checks if c.name == "alpaca account")
        assert acct.status == "WARN"
        assert "paper" in acct.detail.lower()

    @pytest.mark.parametrize(
        ("level", "status"), [(0, "BLOCK"), (1, "BLOCK"), (2, "OK"), (3, "OK")]
    )
    def test_options_level_gate_matches_what_buying_calls_needs(
        self, monkeypatch: pytest.MonkeyPatch, level: int, status: str
    ) -> None:
        """The strategy BUYS CALLS, which is level 2. Level 1 is covered
        calls/cash-secured puts and cannot place this trade."""
        monkeypatch.setenv("APCA_API_KEY_ID", "keyid00000001")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "secret00000001")
        rep = doctor.Report()
        doctor.check_keys(rep, offline=False, verify=lambda: _account(level=level))
        lvl = next(c for c in rep.checks if c.name == "options level")
        assert lvl.status == status

    def test_a_missing_options_level_warns_rather_than_assuming(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APCA_API_KEY_ID", "keyid00000001")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "secret00000001")
        rep = doctor.Report()
        doctor.check_keys(rep, offline=False, verify=lambda: {"account_number": "PA1"})
        lvl = next(c for c in rep.checks if c.name == "options level")
        assert lvl.status == "WARN"


class TestSecretsFileMode:
    def test_a_0600_file_is_ok(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        f = tmp_path / "s.env"
        f.write_text("A=bbbbbbbbbbbb\n")
        f.chmod(0o600)
        monkeypatch.setenv("QTS_SECRETS_FILE", str(f))
        rep = doctor.Report()
        doctor.check_secrets_file(rep)
        assert rep.checks[0].status == "OK"

    def test_a_world_readable_file_blocks_with_the_chmod(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        f = tmp_path / "s.env"
        f.write_text("A=bbbbbbbbbbbb\n")
        f.chmod(0o644)
        monkeypatch.setenv("QTS_SECRETS_FILE", str(f))
        rep = doctor.Report()
        doctor.check_secrets_file(rep)
        assert rep.checks[0].status == "BLOCK"
        assert "chmod 600" in rep.checks[0].fix

    def test_an_absent_file_is_only_informational(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("QTS_SECRETS_FILE", str(tmp_path / "nope.env"))
        rep = doctor.Report()
        doctor.check_secrets_file(rep)
        assert rep.checks[0].status == "INFO"


class TestEnvironment:
    def test_a_malformed_throttle_blocks_by_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QTS_MIN_REQUEST_INTERVAL_S", "1,5")
        rep = doctor.Report()
        doctor.check_environment(rep, CFG)
        bad = next(c for c in rep.checks if c.name == "provider throttle")
        assert bad.status == "BLOCK"
        assert "QTS_MIN_REQUEST_INTERVAL_S" in bad.detail

    def test_a_low_disk_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import shutil as _sh

        class Usage:
            total, used, free = 100 * 1024**3, 99 * 1024**3, 1 * 1024**3

        monkeypatch.setattr(_sh, "disk_usage", lambda _p: Usage)
        rep = doctor.Report()
        doctor.check_environment(rep, CFG)
        disk = next(c for c in rep.checks if c.name == "disk")
        assert disk.status == "BLOCK"

    def test_python_and_venv_are_reported(self) -> None:
        rep = doctor.Report()
        doctor.check_environment(rep, CFG)
        names = [c.name for c in rep.checks]
        assert "python" in names
        assert "venv" in names


class TestTime:
    def test_a_trading_day_reports_the_session_window(self) -> None:
        class Clock:
            def now_utc(self) -> dt.datetime:
                return NOW.astimezone(dt.UTC)

            def session_date(self) -> dt.date:
                return SESSION  # 2026-06-17, a Wednesday

        rep = doctor.Report()
        doctor.check_time(rep, clock=Clock())  # type: ignore[arg-type]
        names = {c.name: c for c in rep.checks}
        assert names["session"].status == "OK"
        assert "09:30-16:00 ET" in names["session"].detail
        assert names["next bell"].detail.startswith("market OPEN")

    def test_a_weekend_warns_rather_than_pretending(self) -> None:
        class Weekend:
            def now_utc(self) -> dt.datetime:
                return dt.datetime(2026, 6, 20, 14, 0, tzinfo=dt.UTC)

            def session_date(self) -> dt.date:
                return dt.date(2026, 6, 20)  # Saturday

        rep = doctor.Report()
        doctor.check_time(rep, clock=Weekend())  # type: ignore[arg-type]
        sess = next(c for c in rep.checks if c.name == "session")
        assert sess.status == "WARN"
        assert "not an XNYS session" in sess.detail

    def test_before_the_bell_reports_seconds_to_open(self) -> None:
        class Early:
            def now_utc(self) -> dt.datetime:
                return dt.datetime(2026, 6, 17, 8, 0, tzinfo=NY).astimezone(dt.UTC)

            def session_date(self) -> dt.date:
                return SESSION

        rep = doctor.Report()
        doctor.check_time(rep, clock=Early())  # type: ignore[arg-type]
        bell = next(c for c in rep.checks if c.name == "next bell")
        assert "until the open" in bell.detail

    def test_after_the_close_says_so(self) -> None:
        class Late:
            def now_utc(self) -> dt.datetime:
                return dt.datetime(2026, 6, 17, 20, 0, tzinfo=NY).astimezone(dt.UTC)

            def session_date(self) -> dt.date:
                return SESSION

        rep = doctor.Report()
        doctor.check_time(rep, clock=Late())  # type: ignore[arg-type]
        bell = next(c for c in rep.checks if c.name == "next bell")
        assert "closed" in bell.detail


class TestFlagsCarryTheirEffect:
    def test_every_flag_prints_a_value_a_tier_and_an_effect(self) -> None:
        """A value alone is not information: 'staleness_gate_enabled=False' tells
        an operator nothing about what happens to a stale bar."""
        rep = doctor.Report()
        doctor.check_flags(rep, CFG)
        assert len(rep.checks) == 4
        for c in rep.checks:
            assert "—" in c.detail, "each flag must state its effect"
            assert "[SAFETY]" in c.detail, "provenance tier must be shown"
        assert all(c.status == "INFO" for c in rep.checks), "a flag value is not a verdict"


class TestState:
    def test_a_missing_db_is_informational(self, tmp_path: Path) -> None:
        rep = doctor.Report()
        doctor.check_state(rep, str(tmp_path / "none.db"), CFG)
        assert rep.checks[0].status == "INFO"

    def test_a_non_sqlite_file_blocks_instead_of_tracebacking(self, tmp_path: Path) -> None:
        """A-12's cousin: QTS_DB pointed at a text file used to raise from deep
        inside the store."""
        bad = tmp_path / "not.db"
        bad.write_text("this is not a database")
        rep = doctor.Report()
        doctor.check_state(rep, str(bad), CFG)
        assert rep.checks[0].status == "BLOCK"

    def test_a_backend_mismatch_blocks(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        store = StateStore(db)
        store.assert_backend("alpaca_paper")
        store.close()
        rep = doctor.Report()
        doctor.check_state(rep, str(db), CFG)  # CFG wants "model"
        backend = next(c for c in rep.checks if c.name == "state backend")
        assert backend.status == "BLOCK"
        assert "fresh --db" in backend.fix

    def test_recorded_data_ages_are_summarised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The samples A-06 started persisting are what the owner needs to choose
        a staleness threshold from data instead of guessing."""
        db = tmp_path / "s.db"
        store = StateStore(db)
        today = doctor.TradingClock.system().session_date()
        for age in (1.0, 5.0, 9.0):
            store.log_decision(NOW, today, "SPY", False, "{}", data_age_min=age)
        store.close()
        rep = doctor.Report()
        doctor.check_state(rep, str(db), CFG)
        c = next(c for c in rep.checks if c.name == "data age (recorded)")
        assert "n=3" in c.detail
        assert "median=5.0" in c.detail

    def test_no_samples_says_none_rather_than_inventing_a_number(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        StateStore(db).close()
        rep = doctor.Report()
        doctor.check_state(rep, str(db), CFG)
        c = next(c for c in rep.checks if c.name == "data age (recorded)")
        assert "no samples" in c.detail


class TestAutonomy:
    def test_a_missing_agent_points_at_the_generator(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(doctor, "PLIST_PATH", tmp_path / "absent.plist")
        monkeypatch.setattr(doctor, "_run", lambda _cmd: "")
        rep = doctor.Report()
        doctor.check_autonomy(rep)
        agent = rep.checks[0]
        assert agent.status == "INFO"
        assert "qts install-agent" in agent.fix

    def test_an_installed_but_unloaded_agent_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        plist = tmp_path / f"{doctor.PLIST_LABEL}.plist"
        plist.write_text("<plist/>")
        monkeypatch.setattr(doctor, "PLIST_PATH", plist)
        monkeypatch.setattr(doctor, "_run", lambda _cmd: "some.other.agent")
        rep = doctor.Report()
        doctor.check_autonomy(rep)
        assert rep.checks[0].status == "WARN"
        assert "launchctl load" in rep.checks[0].fix

    def test_a_loaded_agent_is_ok(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        plist = tmp_path / f"{doctor.PLIST_LABEL}.plist"
        plist.write_text("<plist/>")
        monkeypatch.setattr(doctor, "PLIST_PATH", plist)
        monkeypatch.setattr(doctor, "_run", lambda _cmd: f"123 0 {doctor.PLIST_LABEL}")
        rep = doctor.Report()
        doctor.check_autonomy(rep)
        assert rep.checks[0].status == "OK"

    def test_a_sleeping_machine_warns_and_says_the_fix_is_the_owners(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(doctor, "PLIST_PATH", tmp_path / "absent.plist")
        monkeypatch.setattr(doctor, "_run", lambda cmd: " sleep 1\n disksleep 10\n")
        rep = doctor.Report()
        doctor.check_autonomy(rep)
        sleepc = next(c for c in rep.checks if c.name == "sleep settings")
        assert sleepc.status == "WARN"
        assert "OWNER action" in sleepc.fix

    def test_sleep_disabled_is_ok(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(doctor, "PLIST_PATH", tmp_path / "absent.plist")
        monkeypatch.setattr(doctor, "_run", lambda cmd: " sleep 0\n disksleep 0\n")
        rep = doctor.Report()
        doctor.check_autonomy(rep)
        sleepc = next(c for c in rep.checks if c.name == "sleep settings")
        assert sleepc.status == "OK"

    def test_a_non_macos_host_skips_pmset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(doctor, "PLIST_PATH", tmp_path / "absent.plist")
        monkeypatch.setattr(doctor, "_run", lambda _cmd: "")
        rep = doctor.Report()
        doctor.check_autonomy(rep)
        sleepc = next(c for c in rep.checks if c.name == "sleep settings")
        assert sleepc.status == "SKIP"


class TestRunAndMain:
    def test_run_offline_produces_a_full_report_without_network(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(doctor, "_run", lambda _cmd: "")
        monkeypatch.setattr(doctor, "PLIST_PATH", tmp_path / "absent.plist")
        rep = doctor.run(db=str(tmp_path / "s.db"), offline=True, cfg=CFG)
        names = [c.name for c in rep.checks]
        for expected in ("python", "clock", "provider", "state db", "launchd agent"):
            assert expected in names
        assert rep.exit_code in (0, 1), "offline must never be a BLOCK on its own"

    def test_json_output_is_valid_and_carries_the_verdict(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(doctor, "_run", lambda _cmd: "")
        monkeypatch.setattr(doctor, "PLIST_PATH", tmp_path / "absent.plist")
        rc = doctor.main(["--offline", "--json", "--db", str(tmp_path / "s.db")])
        payload = json.loads(capsys.readouterr().out)
        assert payload["exit_code"] == rc
        assert payload["verdict"] in ("READY", "WARN", "BLOCK")
        assert payload["checks"], "the report must not be empty"

    def test_human_output_renders_a_verdict_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(doctor, "_run", lambda _cmd: "")
        monkeypatch.setattr(doctor, "PLIST_PATH", tmp_path / "absent.plist")
        doctor.main(["--offline", "--db", str(tmp_path / "s.db")])
        out = capsys.readouterr().out
        assert "QTS DOCTOR" in out
        assert "VERDICT:" in out

    def test_the_exit_code_is_the_report_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bad = tmp_path / "not.db"
        bad.write_text("nope")
        monkeypatch.setattr(doctor, "_run", lambda _cmd: "")
        monkeypatch.setattr(doctor, "PLIST_PATH", tmp_path / "absent.plist")
        rc = doctor.main(["--offline", "--db", str(bad)])
        assert rc == 2, "an unreadable db must be a blocking verdict"


class TestProviderFetchPath:
    def test_a_live_fetch_measures_bar_age_and_b4(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fetch path with an injected source: no network, real assertions."""
        from qts_core.models import Bar

        class Src:
            symbol = "SPY"

            def fetch_bars(self, *, now: dt.datetime, **_: Any) -> list[Bar]:
                return [
                    Bar(
                        ts_close=now - dt.timedelta(minutes=3),
                        open=1.0,
                        high=1.0,
                        low=1.0,
                        close=1.0,
                        volume=1,
                    )
                ]

            def has_same_day_expiry(self, session_date: dt.date) -> bool:
                return True

        monkeypatch.setattr(doctor.registry, "make_source", lambda *a, **k: Src())
        cfg = StrategyConfig(tickers=("SPY",))
        rep = doctor.Report()
        doctor.check_provider(rep, cfg, offline=False, name="yfinance")
        names = {c.name: c for c in rep.checks}
        assert names["bars SPY"].status == "OK"
        assert "3.0 min old" in names["bars SPY"].detail
        assert names["0DTE SPY"].status == "OK"
        assert "listed" in names["0DTE SPY"].detail

    def test_stale_bars_warn_without_blocking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from qts_core.models import Bar

        class Stale:
            symbol = "SPY"

            def fetch_bars(self, *, now: dt.datetime, **_: Any) -> list[Bar]:
                return [
                    Bar(
                        ts_close=now - dt.timedelta(minutes=120),
                        open=1.0,
                        high=1.0,
                        low=1.0,
                        close=1.0,
                        volume=1,
                    )
                ]

            def has_same_day_expiry(self, session_date: dt.date) -> bool:
                return False

        monkeypatch.setattr(doctor.registry, "make_source", lambda *a, **k: Stale())
        rep = doctor.Report()
        doctor.check_provider(rep, StrategyConfig(tickers=("SPY",)), offline=False, name="yfinance")
        names = {c.name: c for c in rep.checks}
        assert names["bars SPY"].status == "WARN"
        assert names["0DTE SPY"].detail.count("NOT listed") == 1

    def test_a_provider_exception_costs_the_symbol_not_the_report(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("provider down")

        monkeypatch.setattr(doctor.registry, "make_source", boom)
        rep = doctor.Report()
        doctor.check_provider(rep, StrategyConfig(tickers=("SPY",)), offline=False, name="yfinance")
        c = next(c for c in rep.checks if c.name == "provider SPY")
        assert c.status == "WARN"
        assert "provider down" in c.detail

    def test_no_bars_returned_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Empty:
            symbol = "SPY"

            def fetch_bars(self, *, now: dt.datetime, **_: Any) -> list[Any]:
                return []

        monkeypatch.setattr(doctor.registry, "make_source", lambda *a, **k: Empty())
        rep = doctor.Report()
        doctor.check_provider(rep, StrategyConfig(tickers=("SPY",)), offline=False, name="yfinance")
        assert next(c for c in rep.checks if c.name == "bars SPY").status == "WARN"


def test_run_command_helper_survives_a_missing_binary() -> None:
    assert doctor._run(["definitely-not-a-real-binary-xyz"]) == ""


def test_stranded_positions_are_surfaced(tmp_path: Path) -> None:
    """An expired UNSETTLED position must appear in the readiness report, not
    only in a WARN line the operator may have scrolled past."""
    db = tmp_path / "s.db"
    store = StateStore(db)
    store.save_position(
        "SPY260101C00500000",
        {
            "occ_symbol": "SPY260101C00500000",
            "symbol": "SPY",
            "expiry": "2026-01-01",
            "contracts": 1,
            "phase": "OPEN",
        },
        NOW,
    )
    store.close()
    rep = doctor.Report()
    doctor.check_state(rep, str(db), CFG)
    c = next(c for c in rep.checks if c.name == "stranded positions")
    assert c.status == "WARN"
    assert "SPY260101C00500000" in c.detail


def test_sqlite_import_is_used_for_the_legacy_check() -> None:
    """Guards the import above from being pruned as unused by a future tidy."""
    assert sqlite3.sqlite_version


class TestClockDriftIsCheckedOrDeclaredUnchecked:
    """A-09. Nothing verified the machine clock against anything, yet every
    time-based rail — the entry window and force-flat — is evaluated against it.
    A Mac that sleeps and fails to re-sync NTP can wake minutes off, and on a
    half-day force-flat pulls forward to 12:30, so drift moves a SAFETY rail."""

    def test_offline_skips_and_says_the_clock_is_unverified(self) -> None:
        rep = doctor.Report()
        doctor.check_clock_drift(rep, offline=True)
        c = rep.checks[0]
        assert c.status == "SKIP"
        assert "unverified" in c.detail

    def test_no_authority_skips_rather_than_reporting_zero_drift(self) -> None:
        """An unchecked clock must not read as a checked one."""
        rep = doctor.Report()
        doctor.check_clock_drift(rep, offline=False, authority=None)
        assert rep.checks[0].status == "SKIP"

    def test_a_small_drift_is_ok(self) -> None:
        rep = doctor.Report()
        doctor.check_clock_drift(
            rep,
            offline=False,
            authority=lambda: doctor.TradingClock.system().now_utc() + dt.timedelta(seconds=5),
        )
        c = rep.checks[0]
        assert c.status == "OK"
        assert "s from the authority" in c.detail

    def test_a_large_drift_blocks(self) -> None:
        rep = doctor.Report()
        doctor.check_clock_drift(
            rep,
            offline=False,
            authority=lambda: doctor.TradingClock.system().now_utc() + dt.timedelta(minutes=45),
        )
        assert rep.checks[0].status == "BLOCK"

    def test_drift_is_absolute_so_a_slow_clock_also_blocks(self) -> None:
        rep = doctor.Report()
        doctor.check_clock_drift(
            rep,
            offline=False,
            authority=lambda: doctor.TradingClock.system().now_utc() - dt.timedelta(minutes=45),
        )
        assert rep.checks[0].status == "BLOCK"

    def test_an_unreachable_authority_warns_without_blocking(self) -> None:
        def boom() -> dt.datetime:
            raise OSError("no route")

        rep = doctor.Report()
        doctor.check_clock_drift(rep, offline=False, authority=boom)
        assert rep.checks[0].status == "WARN"
        assert "unreachable" in rep.checks[0].detail

    def test_the_full_report_includes_the_drift_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(doctor, "_run", lambda _cmd: "")
        monkeypatch.setattr(doctor, "PLIST_PATH", tmp_path / "absent.plist")
        rep = doctor.run(db=str(tmp_path / "s.db"), offline=True, cfg=CFG)
        assert any(c.name == "clock drift" for c in rep.checks)
