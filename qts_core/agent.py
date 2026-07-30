"""launchd agent generator: one hands-off paper trading day.

`--dry-run` IS THE DEFAULT. This module prints a plist; installing it requires an
explicit `--install`, and even then it only writes the file — loading it is a
separate owner command that this code never runs. Installing a background agent
that trades on a schedule is an owner decision, and the difference between
"showed me the file" and "put it in my LaunchAgents" must be a flag, not a
surprise.

THREE THINGS THIS GETS RIGHT THAT A NAIVE PLIST WOULD NOT:

1. NO UNCONDITIONAL KeepAlive. run_session.sh exits 0 on the clean "outside the
   session" path — that is its documented success case. A plist with
   `KeepAlive: true` would therefore relaunch it immediately, forever, all night,
   and the loop would keep exiting 0 and being relaunched. `RunAtLoad` plus a
   daily `StartCalendarInterval` is the correct shape.

2. THE SCHEDULE IS DERIVED FROM THE EXCHANGE CALENDAR, NOT A FIXED OFFSET.
   StartCalendarInterval fires on LOCAL wall-clock time, but the bell is 09:30
   America/New_York — and US daylight saving moves that relative to Asia/Riyadh
   by an hour twice a year. So the fire time is computed from the next real XNYS
   session open, converted to local time, minus a margin. Crucially the agent
   then runs with WAIT_FOR_OPEN=1, which recomputes the true open from the wall
   clock every <=5 minutes: DST drift costs some idle waiting and can never
   cause a missed open. Belt and braces, because a missed open is a lost day and
   an early start is free.

3. THE ENVIRONMENT IS PASSED EXPLICITLY. A LaunchAgent inherits none of the
   login shell environment — not PATH, not anything from ~/.zshrc. Keys sourced
   into an interactive shell are simply absent. That is why the plist sets
   QTS_SECRETS_FILE and why qts_core.secrets reads that file itself rather than
   trusting the process environment.
"""

from __future__ import annotations

import datetime as dt
import plistlib
from dataclasses import dataclass
from pathlib import Path

from qts_core.clock import NY, is_trading_day, session_open_et

LABEL = "sa.com.execlogic.qts"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

# Start this far before the bell. Generous on purpose: WAIT_FOR_OPEN makes early
# free, while late is a lost trading day.
DEFAULT_MARGIN_MIN = 45


@dataclass(frozen=True)
class Schedule:
    local_hour: int
    local_minute: int
    session: dt.date
    open_et: dt.datetime
    open_local: dt.datetime
    margin_min: int

    def explain(self) -> str:
        return (
            f"next XNYS session {self.session} opens {self.open_et:%H:%M %Z} "
            f"= {self.open_local:%H:%M %Z} local; firing {self.margin_min} min earlier "
            f"at {self.local_hour:02d}:{self.local_minute:02d} local"
        )


def next_session_schedule(
    now: dt.datetime, *, margin_min: int = DEFAULT_MARGIN_MIN, horizon_days: int = 15
) -> Schedule:
    """Local wall-clock time to fire, derived from the exchange calendar.

    Looks for the next session whose open is still ahead of `now`, so running
    this in the afternoon schedules tomorrow rather than a bell that has passed.
    """
    now_et = now.astimezone(NY)
    probe = now_et.date()
    for _ in range(horizon_days + 1):
        if is_trading_day(probe):
            open_et = session_open_et(probe)
            if open_et > now_et:
                fire_local = (open_et - dt.timedelta(minutes=margin_min)).astimezone()
                return Schedule(
                    local_hour=fire_local.hour,
                    local_minute=fire_local.minute,
                    session=probe,
                    open_et=open_et,
                    open_local=open_et.astimezone(),
                    margin_min=margin_min,
                )
        probe += dt.timedelta(days=1)
    raise RuntimeError(
        f"no XNYS session opens within {horizon_days} days of {now_et:%Y-%m-%d} — "
        "the calendar is wrong, refusing to guess a schedule"
    )


def build_plist(
    *,
    repo: Path,
    schedule: Schedule,
    secrets_file: Path,
    db: str = "qts_v8/state/paper.db",
    interval_s: int = 300,
    log_dir: str = "qts_v8/state",
) -> dict[str, object]:
    """The plist contents. Pure — no filesystem, no clock."""
    # Dated log paths: the loop's stdout is the ONLY place the measured bar age
    # was ever written (A-06's other half), and the last unattended run left a
    # single-line log because nothing captured the session itself. %-substitution
    # does not exist in launchd, so the date is baked in when the agent is
    # generated and refreshed by regenerating it.
    stamp = schedule.session.isoformat().replace("-", "")
    return {
        "Label": LABEL,
        "ProgramArguments": [str(repo / "run_session.sh")],
        "WorkingDirectory": str(repo),
        "RunAtLoad": True,
        "StartCalendarInterval": {
            "Hour": schedule.local_hour,
            "Minute": schedule.local_minute,
        },
        # NO KeepAlive. See the module docstring: exit 0 is this script's normal
        # end-of-session outcome, so KeepAlive would relaunch it all night.
        "ThrottleInterval": 300,
        "ProcessType": "Background",
        "StandardOutPath": str(repo / log_dir / f"day_run_{stamp}.log"),
        "StandardErrorPath": str(repo / log_dir / f"day_run_{stamp}.err.log"),
        "EnvironmentVariables": {
            # A LaunchAgent inherits nothing from the login shell.
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "QTS_SECRETS_FILE": str(secrets_file),
            "DB": db,
            "INTERVAL": str(interval_s),
            "WAIT_FOR_OPEN": "1",
            "REPORT": str(repo / log_dir / "session_report.md"),
        },
    }


def render(plist: dict[str, object]) -> str:
    return plistlib.dumps(plist, sort_keys=True).decode()


def install(plist: dict[str, object], path: Path | None = None) -> Path:
    """Write the plist. Does NOT load it — that is a separate owner command.

    `path=None` and a lookup at CALL time, not `path: Path = PLIST_PATH`.
    A default argument is evaluated once when the function is defined, so the
    earlier signature captured the real ~/Library/LaunchAgents path forever and
    ignored every attempt to redirect it. A test that patched
    `agent.PLIST_PATH` therefore wrote a live LaunchAgent into my own home
    directory — the one action this whole module is careful to leave to the
    owner. It was never loaded and was removed, but the lesson is structural:
    for a module-level path that names a side effect outside the repo,
    late binding is a safety property, not a style preference.
    """
    target = PLIST_PATH if path is None else path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(plist, sort_keys=True))
    return target
