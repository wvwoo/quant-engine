"""`qts` dispatcher + launchd agent generator.

Two contracts are asserted here and both matter more than the CLI itself:
  1. EVERY legacy invocation still works. Adding a front door must not break the
     four commands the README documents.
  2. `install-agent` writes NOTHING without --install, and the plist it produces
     avoids the three traps a naive one would fall into (KeepAlive on a script
     whose success is exit 0, a fixed schedule that DST breaks, and an inherited
     environment a LaunchAgent does not have).

The new modules also have to clear ci.sh's 85% per-file floor in the same change
that adds them.
"""

from __future__ import annotations

import datetime as dt
import plistlib
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from qts_core import agent, cli
from qts_core.clock import NY

REPO = Path(__file__).resolve().parent.parent


def _recorder(sink: list[list[str]]) -> Callable[[list[str]], int]:
    """Record argv and return 0.

    Not `lambda a: sink.append(a) or 0`: list.append returns None, so mypy
    (correctly) rejects it as a value-returning expression.
    """

    def record(argv: list[str]) -> int:
        sink.append(argv)
        return 0

    return record


class TestUsage:
    def test_no_args_prints_usage_and_succeeds(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main([]) == 0
        assert "usage: qts" in capsys.readouterr().out

    @pytest.mark.parametrize("flag", ["-h", "--help", "help"])
    def test_help_forms(self, flag: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main([flag]) == 0
        assert "commands:" in capsys.readouterr().out

    def test_every_advertised_command_is_dispatchable(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A command listed in the usage text but not routed would be a lie."""
        cli.main([])
        listed = capsys.readouterr().out
        for name in cli.COMMANDS:
            assert name in listed

    def test_an_unknown_command_exits_2_and_lists_the_real_ones(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["frobnicate"]) == 2
        err = capsys.readouterr().err
        assert "unknown command" in err
        assert "doctor" in err

    def test_usage_documents_the_legacy_invocations(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.main([])
        out = capsys.readouterr().out
        assert "python -m qts_core.live" in out
        assert "run_session.sh" in out


class TestDispatch:
    def test_doctor_is_routed_and_its_exit_code_is_forwarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: list[list[str]] = []

        def fake_main(argv: list[str]) -> int:
            seen.append(argv)
            return 7

        from qts_core import doctor

        monkeypatch.setattr(doctor, "main", fake_main)
        assert cli.main(["doctor", "--offline", "--db", "x.db"]) == 7
        assert seen == [["--offline", "--db", "x.db"]]

    def test_run_forwards_argv_verbatim_to_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Argv is forwarded rather than re-declared, so there is exactly one
        definition of --db/--symbols/--broker/--provider."""
        seen: list[list[str]] = []
        from qts_core import live

        monkeypatch.setattr(live, "main", _recorder(seen))
        assert cli.main(["run", "--symbols", "SPY", "--provider", "alpaca"]) == 0
        assert seen == [["--symbols", "SPY", "--provider", "alpaca"]]

    def test_backtest_forwards_argv_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[list[str]] = []
        from qts_core import run_backtest

        monkeypatch.setattr(run_backtest, "main", _recorder(seen))
        assert cli.main(["backtest", "--symbol", "QQQ", "--days", "5"]) == 0
        assert seen == [["--symbol", "QQQ", "--days", "5"]]

    def test_loop_shells_out_to_the_existing_script(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_session.sh is NOT rewritten to call `qts` — it is driven by
        stub-based tests, and replacing its invocation would break them."""
        seen: list[list[str]] = []
        monkeypatch.setattr(cli, "_shell", _recorder(seen))
        assert cli.main(["loop"]) == 0
        assert seen[0][0].endswith("run_session.sh")

    def test_dashboard_uses_the_default_port_and_honours_an_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []
        monkeypatch.setattr(cli, "_shell", _recorder(seen))
        cli.main(["dashboard"])
        assert "8787" in seen[0]
        cli.main(["dashboard", "--port", "9999"])
        assert "9999" in seen[1]


class TestStatus:
    def test_no_db_is_reported_not_crashed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["status", "--db", str(tmp_path / "none.db")]) == 0
        assert "nothing has run yet" in capsys.readouterr().out

    def test_status_summarises_a_real_db(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from qts_core.clock import TradingClock
        from qts_core.store import StateStore

        db = tmp_path / "s.db"
        store = StateStore(db)
        session = TradingClock.system().session_date()
        store.log_decision(dt.datetime.now(dt.UTC), session, "SPY", False, "{}", data_age_min=4.0)
        store.close()
        assert cli.main(["status", "--db", str(db)]) == 0
        out = capsys.readouterr().out
        assert "decisions=1" in out
        assert "data age n=1" in out

    def test_status_says_when_no_ages_were_recorded(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from qts_core.store import StateStore

        db = tmp_path / "s.db"
        StateStore(db).close()
        cli.main(["status", "--db", str(db)])
        assert "no samples recorded" in capsys.readouterr().out


class TestReport:
    def test_missing_db_exits_2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["report", "--db", str(tmp_path / "none.db")]) == 2

    def test_report_writes_to_a_file_when_asked(self, tmp_path: Path) -> None:
        from qts_core.store import StateStore

        db = tmp_path / "s.db"
        StateStore(db).close()
        out = tmp_path / "r.md"
        assert cli.main(["report", "--db", str(db), "--out", str(out)]) == 0
        assert "PAPER" in out.read_text()

    def test_report_goes_to_stdout_by_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from qts_core.store import StateStore

        db = tmp_path / "s.db"
        StateStore(db).close()
        assert cli.main(["report", "--db", str(db)]) == 0
        assert "PAPER" in capsys.readouterr().out


class TestScheduleIsDerivedFromTheExchangeCalendar:
    """StartCalendarInterval fires on LOCAL wall-clock time, but the bell is
    09:30 America/New_York and US daylight saving moves that relative to every
    other zone twice a year. A hardcoded local time is wrong for half the year."""

    def test_a_wednesday_morning_schedules_the_same_day(self) -> None:
        now = dt.datetime(2026, 6, 17, 6, 0, tzinfo=NY)  # Wednesday, pre-open
        s = agent.next_session_schedule(now, margin_min=45)
        assert s.session == dt.date(2026, 6, 17)
        assert s.open_et.hour == 9 and s.open_et.minute == 30

    def test_an_afternoon_run_schedules_the_NEXT_session_not_a_passed_bell(self) -> None:
        """The bug the old WAIT_FOR_OPEN had: computing this morning's open in
        the afternoon makes the wait expire instantly."""
        now = dt.datetime(2026, 6, 17, 14, 0, tzinfo=NY)
        s = agent.next_session_schedule(now)
        assert s.session == dt.date(2026, 6, 18)
        assert s.open_et > now

    def test_a_friday_evening_skips_the_weekend(self) -> None:
        now = dt.datetime(2026, 6, 19, 20, 0, tzinfo=NY)  # Friday night
        s = agent.next_session_schedule(now)
        assert s.session.weekday() == 0, "Monday, not Saturday"

    def test_the_margin_is_applied_before_the_open(self) -> None:
        now = dt.datetime(2026, 6, 17, 6, 0, tzinfo=NY)
        s = agent.next_session_schedule(now, margin_min=45)
        fire = s.open_local - dt.timedelta(minutes=45)
        assert (s.local_hour, s.local_minute) == (fire.hour, fire.minute)

    def test_dst_changes_the_local_fire_time(self) -> None:
        """The whole reason the schedule is computed: a fixed offset would be an
        hour wrong for months at a time."""
        summer = agent.next_session_schedule(dt.datetime(2026, 7, 1, 6, 0, tzinfo=NY))
        winter = agent.next_session_schedule(dt.datetime(2026, 12, 1, 6, 0, tzinfo=NY))
        assert summer.open_et.utcoffset() != winter.open_et.utcoffset()

    def test_a_broken_calendar_refuses_to_guess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(agent, "is_trading_day", lambda _d: False)
        with pytest.raises(RuntimeError, match="refusing to guess"):
            agent.next_session_schedule(dt.datetime(2026, 6, 17, 6, 0, tzinfo=NY))


class TestPlistShape:
    def _plist(self, tmp_path: Path) -> dict[str, object]:
        s = agent.next_session_schedule(dt.datetime(2026, 6, 17, 6, 0, tzinfo=NY))
        return agent.build_plist(repo=REPO, schedule=s, secrets_file=tmp_path / "s.env")

    def test_there_is_no_unconditional_keepalive(self, tmp_path: Path) -> None:
        """run_session.sh exits 0 on the clean 'outside the session' path, so
        KeepAlive would relaunch it all night in a tight loop."""
        p = self._plist(tmp_path)
        assert "KeepAlive" not in p

    def test_it_runs_at_load_and_on_a_daily_calendar_interval(self, tmp_path: Path) -> None:
        p = self._plist(tmp_path)
        assert p["RunAtLoad"] is True
        interval = p["StartCalendarInterval"]
        assert isinstance(interval, dict)
        assert set(interval) == {"Hour", "Minute"}

    def test_a_throttle_interval_bounds_any_restart_storm(self, tmp_path: Path) -> None:
        throttle = self._plist(tmp_path)["ThrottleInterval"]
        assert isinstance(throttle, int)
        assert throttle >= 60

    def test_the_environment_is_passed_explicitly(self, tmp_path: Path) -> None:
        """A LaunchAgent inherits nothing from the login shell — not PATH, and
        certainly not keys sourced by ~/.zshrc."""
        env = self._plist(tmp_path)["EnvironmentVariables"]
        assert isinstance(env, dict)
        assert str(env["QTS_SECRETS_FILE"]).endswith("s.env")
        assert env["WAIT_FOR_OPEN"] == "1", "the loop must self-correct to the real bell"
        assert "PATH" in env

    def test_log_paths_are_dated(self, tmp_path: Path) -> None:
        """The last unattended run left a ONE-LINE log because nothing captured
        the session; the measured bar ages went to a terminal that no longer
        exists."""
        p = self._plist(tmp_path)
        assert "day_run_20260617.log" in str(p["StandardOutPath"])
        assert str(p["StandardErrorPath"]).endswith(".err.log")

    def test_stdout_and_stderr_go_to_different_files(self, tmp_path: Path) -> None:
        p = self._plist(tmp_path)
        assert p["StandardOutPath"] != p["StandardErrorPath"]

    def test_it_renders_as_valid_plist_xml(self, tmp_path: Path) -> None:
        text = agent.render(self._plist(tmp_path))
        assert plistlib.loads(text.encode())["Label"] == agent.LABEL


class TestInstallAgentIsDryRunByDefault:
    def test_default_writes_nothing_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "sa.com.execlogic.qts.plist"
        monkeypatch.setattr(agent, "PLIST_PATH", target)
        assert cli.main(["install-agent"]) == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert not target.exists(), "the default must not touch LaunchAgents"

    def test_install_flag_writes_the_file_but_does_not_load_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "sa.com.execlogic.qts.plist"
        monkeypatch.setattr(agent, "PLIST_PATH", target)
        assert cli.main(["install-agent", "--install"]) == 0
        out = capsys.readouterr().out
        assert target.exists()
        assert plistlib.loads(target.read_bytes())["Label"] == agent.LABEL
        assert "NOT loaded" in out, "loading is a separate owner command"
        assert "launchctl load" in out

    def test_the_dry_run_output_explains_the_derived_schedule(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(agent, "PLIST_PATH", tmp_path / "x.plist")
        cli.main(["install-agent"])
        out = capsys.readouterr().out
        assert "next XNYS session" in out
        assert "local" in out

    def test_custom_db_and_interval_reach_the_plist(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(agent, "PLIST_PATH", tmp_path / "x.plist")
        cli.main(["install-agent", "--db", "alt.db", "--interval", "60"])
        out = capsys.readouterr().out
        assert "alt.db" in out
        assert "<string>60</string>" in out


class TestLegacyInvocationsStillWork:
    """The contract that matters most: a front door must not break the doors
    that already existed. These run the real interpreter in a subprocess."""

    @pytest.mark.parametrize(
        "args",
        [
            ["-m", "qts_core.live", "--help"],
            ["-m", "qts_core.run_backtest", "--help"],
            ["-m", "qts_core.doctor", "--help"],
            ["-m", "qts_core.cli", "--help"],
        ],
    )
    def test_module_entry_points_still_respond(self, args: list[str]) -> None:
        out = subprocess.run(
            [sys.executable, *args], cwd=REPO, capture_output=True, text=True, timeout=90
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip()

    def test_the_zero_install_shim_exists_and_is_executable(self) -> None:
        """pyproject declares a console script, but that only exists after
        `pip install -e .`. A fresh clone has neither, so the shim is the path a
        new machine actually takes."""
        shim = REPO / "qts"
        assert shim.exists()
        assert shim.stat().st_mode & 0o111, "the shim must be executable"
        assert "qts_core.cli" in shim.read_text()

    def test_run_session_sh_still_invokes_the_module_not_the_new_cli(self) -> None:
        """Rewriting this line would break tests/test_run_session_sh.py, which
        drives the script with stubs."""
        text = (REPO / "run_session.sh").read_text()
        assert "-m qts_core.live" in text

    def test_pyproject_declares_the_console_script(self) -> None:
        import tomllib

        data = tomllib.loads((REPO / "pyproject.toml").read_text())
        assert data["project"]["scripts"]["qts"] == "qts_core.cli:main"
        assert "build-system" in data, "a console script needs a build backend"

    def test_the_console_script_target_is_importable_and_callable(self) -> None:
        module_path, _, func = "qts_core.cli:main".partition(":")
        mod = __import__(module_path, fromlist=[func])
        assert callable(getattr(mod, func))


class TestInstallTargetIsResolvedLate:
    """Regression guard for a real incident.

    `install(plist, path: Path = PLIST_PATH)` bound the default at function
    DEFINITION time, so patching agent.PLIST_PATH had no effect and the test
    below wrote a live LaunchAgent into the developer's actual home directory —
    precisely the action this module exists to leave to the owner. It was never
    loaded and was deleted, but the shape had to change: any module-level path
    naming a side effect outside the repo must be resolved at CALL time.
    """

    def test_install_honours_a_patched_module_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "patched.plist"
        monkeypatch.setattr(agent, "PLIST_PATH", target)
        written = agent.install({"Label": "x"})
        assert written == target
        assert target.exists()

    def test_the_signature_does_not_capture_the_real_path_as_a_default(self) -> None:
        """Asserted on the signature itself, because the failure mode is silent:
        the function still works, it just writes somewhere nobody asked for."""
        import inspect

        default = inspect.signature(agent.install).parameters["path"].default
        assert default is None, "install() must resolve PLIST_PATH at call time, not bind it"

    def test_an_explicit_path_still_wins(self, tmp_path: Path) -> None:
        target = tmp_path / "explicit.plist"
        assert agent.install({"Label": "x"}, target) == target
