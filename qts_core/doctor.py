"""Readiness gate: is this machine able to trade paper today, and what is missing?

Exit codes, because a loop needs a decision and not prose:
    0  READY   — every check passed
    1  WARN    — something is off but a session can still run honestly
    2  BLOCK   — do not trade; the run would be meaningless or unsafe

DESIGN RULE, and the reason this file exists at all: every line is MEASURED.
There is no check here that reports a config value as if it were a fact about
the world. `staleness_gate_enabled = False` is a setting; "the newest SPY bar is
4.2 minutes old" is a measurement; the first is printed as configuration and the
second as evidence, and they are never mixed. A green dashboard that asserts
something nobody measured is the exact failure mode this project keeps finding
in its own history.

OFFLINE IS A RESULT, NOT AN ERROR. With no network the provider checks report
SKIP and say why, and the overall verdict is at worst WARN. A readiness tool
that cannot run on a train is a readiness tool nobody runs.

NEVER PRINTS A SECRET. Key checks print present/absent, plausible/implausible,
accepted/rejected — never a value, not even a prefix. Every message that could
carry venue output goes through secrets.redact().

READS, NEVER WRITES. It measures the machine (`pmset`, `launchctl`, disk, the
db) and changes nothing: no power settings, no agent loading, no schema
migration. Fixing is the owner's act; this tool's job is to make the next step
obvious.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from qts_core import secrets
from qts_core.clock import NY, TradingClock, is_trading_day, session_close_et, session_open_et
from qts_core.config import PROVENANCE, StrategyConfig
from qts_core.providers import registry
from qts_core.providers.yfinance_source import InvalidRequestInterval, request_interval_s
from qts_core.store import StateStore

Status = Literal["OK", "WARN", "BLOCK", "SKIP", "INFO"]

_RANK: dict[Status, int] = {"OK": 0, "INFO": 0, "SKIP": 1, "WARN": 1, "BLOCK": 2}

PLIST_LABEL = "sa.com.execlogic.qts"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"

# Below this, a full trading day of logs and WAL churn is a real risk.
MIN_FREE_DISK_GB = 2.0
# A clock this far from the exchange's own would move force-flat evaluation.
MAX_CLOCK_DRIFT_S = 60.0


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: Status, detail: str, fix: str = "") -> None:
        self.checks.append(Check(name, status, secrets.redact(detail), fix))

    @property
    def exit_code(self) -> int:
        return max((_RANK[c.status] for c in self.checks), default=0)

    @property
    def verdict(self) -> str:
        return {0: "READY", 1: "WARN", 2: "BLOCK"}[self.exit_code]

    def to_json(self) -> str:
        return json.dumps(
            {
                "verdict": self.verdict,
                "exit_code": self.exit_code,
                "checks": [
                    {"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix}
                    for c in self.checks
                ],
            },
            indent=2,
            ensure_ascii=False,
        )

    def render(self) -> str:
        glyph = {"OK": "✓", "WARN": "!", "BLOCK": "✗", "SKIP": "-", "INFO": "·"}
        width = max((len(c.name) for c in self.checks), default=10)
        lines = ["", "  QTS DOCTOR", "  " + "─" * (width + 60)]
        for c in self.checks:
            lines.append(f"  {glyph[c.status]} {c.name.ljust(width)}  {c.detail}")
            if c.fix and c.status in ("WARN", "BLOCK"):
                lines.append(f"    {' ' * width}  -> {c.fix}")
        lines += [
            "  " + "─" * (width + 60),
            f"  VERDICT: {self.verdict}  (exit {self.exit_code})",
            "",
        ]
        return "\n".join(lines)


def _run(cmd: list[str]) -> str:
    """Run a read-only system command; '' if it is unavailable or fails."""
    if shutil.which(cmd[0]) is None:
        return ""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        return out.stdout
    except (subprocess.SubprocessError, OSError):
        return ""


# ------------------------------------------------------------------ sections
def check_environment(rep: Report, cfg: StrategyConfig) -> None:
    rep.add("python", "OK", f"{sys.version.split()[0]} at {sys.executable}")

    venv = Path(sys.prefix)
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    rep.add(
        "venv",
        "OK" if in_venv else "WARN",
        f"{venv}" if in_venv else "running against the system interpreter",
        "create one: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt",
    )

    usage = shutil.disk_usage(Path.cwd())
    free_gb = usage.free / 1024**3
    pct_used = 100.0 * usage.used / usage.total
    rep.add(
        "disk",
        "OK" if free_gb >= MIN_FREE_DISK_GB else "BLOCK",
        f"{free_gb:.1f} GiB free ({pct_used:.0f}% used)",
        f"free at least {MIN_FREE_DISK_GB:.0f} GiB: a full session writes logs, WAL and reports",
    )

    try:
        interval = request_interval_s(cfg)
        rep.add("provider throttle", "OK", f"{interval}s between provider calls")
    except InvalidRequestInterval as exc:
        rep.add("provider throttle", "BLOCK", str(exc), "unset QTS_MIN_REQUEST_INTERVAL_S")


def check_secrets_file(rep: Report) -> None:
    path = secrets.secrets_file_path()
    if not path.exists():
        rep.add(
            "secrets file",
            "INFO",
            f"{path} does not exist (keys may still come from the environment)",
        )
        return
    mode = path.stat().st_mode & 0o777
    if mode == 0o600:
        rep.add("secrets file", "OK", f"{path} mode 0600")
    else:
        rep.add(
            "secrets file",
            "BLOCK",
            f"{path} mode {mode:04o} — other users can read it, so it is REFUSED",
            f"chmod 600 {path}",
        )


def check_clock_drift(rep: Report, *, offline: bool, authority: Any = None) -> None:
    """A-09: the wall clock is otherwise a TRUSTED, UNVERIFIED input.

    Nothing in this system checks the machine's clock against anything. A Mac
    that sleeps and fails to re-sync NTP can wake minutes off, and every
    time-based rail — the 09:46-11:30 entry window, and force-flat at 15:30 —
    is evaluated against that clock. On a half-day, where force-flat pulls
    forward to 12:30, a drift of tens of minutes moves a SAFETY rail.

    `authority` is injected (a callable returning an aware datetime) so the
    check is testable offline. With no authority available this SKIPs and says
    so, rather than reporting a drift of zero it never measured — an unchecked
    clock must not read as a checked one.
    """
    if offline or authority is None:
        rep.add(
            "clock drift",
            "SKIP",
            "no external time authority consulted — the machine clock is unverified",
            "an unverified clock moves the entry window and the force-flat rail",
        )
        return
    try:
        theirs = authority()
    except Exception as exc:
        rep.add("clock drift", "WARN", f"time authority unreachable: {type(exc).__name__}: {exc}")
        return
    drift = abs((theirs - TradingClock.system().now_utc()).total_seconds())
    rep.add(
        "clock drift",
        "OK" if drift <= MAX_CLOCK_DRIFT_S else "BLOCK",
        f"{drift:.1f}s from the authority (limit {MAX_CLOCK_DRIFT_S:.0f}s)",
        "resync the system clock: a drifting clock moves force-flat and the entry window",
    )


def check_time(rep: Report, clock: TradingClock | None = None) -> None:
    clock = clock or TradingClock.system()
    now_utc = clock.now_utc()
    now_et = now_utc.astimezone(NY)
    local = now_utc.astimezone()
    offset = (local.utcoffset() or dt.timedelta()).total_seconds() - (
        now_et.utcoffset() or dt.timedelta()
    ).total_seconds()
    rep.add(
        "clock",
        "OK",
        f"local {local:%Y-%m-%d %H:%M %Z} = ET {now_et:%Y-%m-%d %H:%M %Z} "
        f"(offset {offset / 3600:+.0f}h)",
    )

    session = clock.session_date()
    if not is_trading_day(session):
        rep.add(
            "session",
            "WARN",
            f"{session} is not an XNYS session — nothing to trade today",
            "this is informational; the runner exits 0 on non-trading days",
        )
        return

    close = session_close_et(session)
    open_et = session_open_et(session)
    half = close.hour < 15
    rep.add(
        "session",
        "OK",
        f"{session} is a{'  HALF-DAY' if half else ' full'} XNYS session, "
        f"{open_et:%H:%M}-{close:%H:%M} ET",
    )
    if now_et < open_et:
        secs = int((open_et - now_et).total_seconds())
        rep.add("next bell", "OK", f"{secs}s until the open ({secs / 3600:.1f}h)")
    elif now_et <= close:
        rep.add(
            "next bell", "OK", f"market OPEN, {int((close - now_et).total_seconds())}s to close"
        )
    else:
        rep.add("next bell", "INFO", "today's session has closed")


def check_flags(rep: Report, cfg: StrategyConfig) -> None:
    """Print each flag's value AND its one-line effect. A value alone is not
    information: 'staleness_gate_enabled=False' tells an operator nothing about
    what will happen to a stale bar."""
    effects = {
        "live_trading": "real-money path; refused even when acknowledged (no live broker exists)",
        "staleness_gate_enabled": "OFF = bar age is measured and reported but never blocks entry",
        "backtest_artifacts": "OFF = backtest results stay on stdout, never written to a file",
        "broker_backend": "'model' = in-process modeled fills; 'alpaca_paper' = real paper venue",
    }
    for name, effect in effects.items():
        value = getattr(cfg, name)
        tier = PROVENANCE[name][0].value
        rep.add(f"flag {name}", "INFO", f"{value!r} [{tier}] — {effect}")


def check_keys(rep: Report, *, offline: bool, verify: Any = None) -> None:
    """Presence, then local shape, then — only if asked — live acceptance.

    `verify` is injected so every branch is testable with no network. It is
    called with no arguments and must return the decoded /v2/account body.
    """
    present: dict[str, bool] = {}
    for name in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        value = secrets.get(name, required=False)
        present[name] = bool(value)
        if not value:
            rep.add(
                f"key {name}",
                "INFO",
                "absent — needed only for --broker alpaca_paper or --provider alpaca",
                f"see KEY_ACQUISITION.md, then add {name} to {secrets.secrets_file_path()}",
            )
            continue
        ok, reason = secrets.looks_plausible(value)
        rep.add(
            f"key {name}",
            "OK" if ok else "WARN",
            f"present, {reason}",
            "" if ok else "re-copy the value from the Alpaca PAPER dashboard",
        )

    if not all(present.values()):
        rep.add("alpaca account", "SKIP", "no key pair present, so nothing to verify")
        return
    if offline:
        rep.add("alpaca account", "SKIP", "--offline requested; live acceptance NOT checked")
        return
    if verify is None:
        from qts_core.broker_alpaca import AlpacaPaperBroker

        broker = AlpacaPaperBroker()

        def verify() -> dict[str, Any]:
            status, body = broker._request("GET", "/v2/account", None)
            if status != 200:
                raise RuntimeError(f"HTTP {status}: {body}")
            return body

    try:
        body = verify()
    except Exception as exc:  # every failure mode is the operator's problem
        rep.add(
            "alpaca account",
            "BLOCK",
            f"{type(exc).__name__}: {exc}",
            "check the key came from the PAPER dashboard and has not been regenerated",
        )
        return

    # Is it really a paper account? Refuse to look ready if we cannot tell.
    number = str(body.get("account_number", "?"))
    masked = f"…{number[-4:]}" if len(number) > 4 else "(short)"
    looks_paper = number.upper().startswith("PA")
    rep.add(
        "alpaca account",
        "OK" if looks_paper else "WARN",
        f"reachable, account {masked}, status {body.get('status', '?')}"
        + ("" if looks_paper else " — account number does not look like a paper account"),
        "" if looks_paper else "confirm you generated the keys on app.alpaca.markets/paper/...",
    )

    approved = body.get("options_approved_level")
    effective = body.get("options_trading_level")
    if approved is None and effective is None:
        rep.add(
            "options level",
            "WARN",
            "the account payload did not report an options level",
            "the strategy BUYS CALLS, which needs level 2 or higher",
        )
        return
    level = int(effective if effective is not None else approved)
    rep.add(
        "options level",
        "OK" if level >= 2 else "BLOCK",
        f"approved={approved} effective={effective} (buying calls needs >= 2)",
        "enable options level 2+ on the paper account: Account > Configure",
    )


def check_provider(rep: Report, cfg: StrategyConfig, *, offline: bool, name: str | None) -> None:
    try:
        provider = registry.resolve_name(name)
    except registry.UnknownProvider as exc:
        rep.add("provider", "BLOCK", str(exc), f"pick one of: {', '.join(registry.available())}")
        return
    meta = registry.declared_meta(provider, "SPY")
    rep.add(
        "provider",
        "OK",
        f"{provider} (feed={meta['feed']}, delayed={meta['delayed']})",
    )
    if offline:
        rep.add("provider fetch", "SKIP", "--offline requested; no live fetch attempted")
        rep.add("0DTE availability", "SKIP", "--offline requested (gate B4 unchecked)")
        return

    clock = TradingClock.system()
    now, session = clock.now_utc(), clock.session_date()
    ages: list[str] = []
    for symbol in cfg.tickers:
        try:
            source = registry.make_source(symbol, name=provider, cfg=cfg)
            bars = source.fetch_bars(now=now)
            if not bars:
                rep.add(f"bars {symbol}", "WARN", "provider returned no completed bars")
                continue
            age = (now - bars[-1].ts_close).total_seconds() / 60.0
            over = age > cfg.max_bar_age_min
            ages.append(f"{symbol} {age:.1f}m")
            rep.add(
                f"bars {symbol}",
                "WARN" if over else "OK",
                f"{len(bars)} bars, newest {age:.1f} min old (limit {cfg.max_bar_age_min}m)",
                "data is staler than the configured limit; entries would be refused if the "
                "staleness gate were enabled"
                if over
                else "",
            )
            has0 = source.has_same_day_expiry(session)
            rep.add(
                f"0DTE {symbol}",
                "OK" if has0 else "INFO",
                f"same-day expiry {'listed' if has0 else 'NOT listed'} for {session} (gate B4)",
            )
        except Exception as exc:
            rep.add(
                f"provider {symbol}",
                "WARN",
                f"{type(exc).__name__}: {exc}",
                "a provider failure costs this symbol, not the session",
            )
    if ages:
        rep.add("data age", "INFO", "measured now: " + ", ".join(ages))


def check_state(rep: Report, db: str, cfg: StrategyConfig) -> None:
    path = Path(db)
    if not path.exists():
        rep.add("state db", "INFO", f"{db} does not exist yet; it will be created on first run")
        return
    try:
        store = StateStore(db)
    except Exception as exc:
        rep.add(
            "state db",
            "BLOCK",
            f"{db} is unreadable: {type(exc).__name__}: {exc}",
            "point --db at a real SQLite database, or remove the file",
        )
        return
    try:
        backend = store.backend()
        matches = backend is None or backend == cfg.broker_backend
        rep.add(
            "state backend",
            "OK" if matches else "BLOCK",
            f"db claims {backend!r}, this config wants {cfg.broker_backend!r}",
            "one db = one backend; use a fresh --db when switching",
        )
        session = TradingClock.system().session_date()
        expired = store.expired_positions(as_of=session)
        rep.add(
            "stranded positions",
            "OK" if not expired else "WARN",
            "none" if not expired else f"{len(expired)} expired UNSETTLED: {sorted(expired)}",
            "owner action: settle these before relying on realized P&L",
        )
        samples = store.data_age_samples(session)
        if samples:
            vals = sorted(a for _, _, a in samples)
            mid = vals[len(vals) // 2]
            rep.add(
                "data age (recorded)",
                "INFO",
                f"n={len(vals)} min={vals[0]:.1f} median={mid:.1f} max={vals[-1]:.1f} min "
                f"for {session}",
            )
        else:
            rep.add("data age (recorded)", "INFO", f"no samples recorded yet for {session}")
    finally:
        store.close()


def check_autonomy(rep: Report) -> None:
    """Is an unattended day actually possible? Measured, never changed."""
    if PLIST_PATH.exists():
        loaded = PLIST_LABEL in _run(["launchctl", "list"])
        rep.add(
            "launchd agent",
            "OK" if loaded else "WARN",
            f"{PLIST_PATH.name} exists and is {'LOADED' if loaded else 'NOT loaded'}",
            f"launchctl load {PLIST_PATH}",
        )
    else:
        rep.add(
            "launchd agent",
            "INFO",
            "not installed — a hands-off day needs one",
            "generate it with: qts install-agent   (writes nothing without --install)",
        )

    pm = _run(["pmset", "-g"])
    if not pm:
        rep.add("sleep settings", "SKIP", "pmset unavailable (not macOS?)")
        return
    values: dict[str, str] = {}
    for line in pm.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("sleep", "disksleep", "powernap", "womp"):
            values[parts[0]] = parts[1]
    sleep = values.get("sleep", "?")
    sleeps = sleep.isdigit() and int(sleep) > 0
    rep.add(
        "sleep settings",
        "WARN" if sleeps else "OK",
        f"pmset sleep={sleep} disksleep={values.get('disksleep', '?')}",
        "the machine sleeping mid-session pauses the loop; the wait-for-open path "
        "recomputes from the wall clock but a sleeping Mac cannot trade. Changing "
        "power settings is an OWNER action — this tool only reports it.",
    )


# --------------------------------------------------------------------- entry
def run(
    *,
    db: str = "qts_v8/state/paper.db",
    provider: str | None = None,
    offline: bool = False,
    cfg: StrategyConfig | None = None,
    verify: Any = None,
) -> Report:
    cfg = cfg if cfg is not None else StrategyConfig()
    rep = Report()
    check_environment(rep, cfg)
    check_secrets_file(rep)
    check_time(rep)
    check_clock_drift(rep, offline=offline)
    check_keys(rep, offline=offline, verify=verify)
    check_provider(rep, cfg, offline=offline, name=provider)
    check_state(rep, db, cfg)
    check_flags(rep, cfg)
    check_autonomy(rep)
    return rep


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="qts doctor", description="Is this machine ready to trade paper today?"
    )
    ap.add_argument("--db", default="qts_v8/state/paper.db")
    ap.add_argument("--provider", choices=list(registry.available()), default=None)
    ap.add_argument(
        "--offline",
        action="store_true",
        help="skip every network check and say so, instead of failing",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    rep = run(db=args.db, provider=args.provider, offline=args.offline)
    print(rep.to_json() if args.json else rep.render())
    return rep.exit_code


if __name__ == "__main__":  # pragma: no cover - module entry
    sys.exit(main())
