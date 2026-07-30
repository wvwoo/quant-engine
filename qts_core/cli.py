"""`qts` — one entry point for every command.

The old surface was four unrelated invocations that had to be memorised:
`python -m qts_core.live`, `./run_session.sh`, `python -m qts_core.run_backtest`,
`uvicorn qts_core.api:app`. EVERY ONE OF THEM STILL WORKS, verbatim, and a test
asserts it: this is a front door, not a replacement, and breaking the documented
commands to add a nicer one would be a poor trade.

Dispatch is deliberately thin — argv is forwarded to the module that already owns
the arguments, so there is exactly one definition of `--db`, `--symbols`,
`--broker` and `--provider`, and `qts run --help` shows the same flags as
`python -m qts_core.live --help`. Re-declaring them here would create two
sources of truth that drift.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

COMMANDS = {
    "doctor": "check whether this machine can trade paper today (exit 0/1/2)",
    "run": "one paper step over the universe",
    "loop": "continuous session until the close (run_session.sh)",
    "backtest": "MODELED backtest over real bars",
    "dashboard": "serve the read-only dashboard on :8787",
    "report": "render the session report for a date",
    "status": "one-line summary of the current session",
    "install-agent": "generate the launchd plist (prints it; --install to write)",
}


def _usage() -> str:
    width = max(len(c) for c in COMMANDS)
    lines = ["usage: qts <command> [options]", "", "commands:"]
    lines += [f"  {name.ljust(width)}  {desc}" for name, desc in COMMANDS.items()]
    lines += [
        "",
        "Every legacy invocation still works unchanged:",
        "  python -m qts_core.live      ./run_session.sh",
        "  python -m qts_core.run_backtest",
        "  uvicorn qts_core.api:app --port 8787",
    ]
    return "\n".join(lines)


def _install_agent(argv: list[str]) -> int:
    from qts_core import agent
    from qts_core import secrets as sec
    from qts_core.clock import TradingClock

    ap = argparse.ArgumentParser(prog="qts install-agent")
    ap.add_argument(
        "--install",
        action="store_true",
        help="actually write the plist (default: print it and change nothing)",
    )
    ap.add_argument("--db", default="qts_v8/state/paper.db")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--margin-min", type=int, default=agent.DEFAULT_MARGIN_MIN)
    args = ap.parse_args(argv)

    schedule = agent.next_session_schedule(
        TradingClock.system().now_utc(), margin_min=args.margin_min
    )
    plist = agent.build_plist(
        repo=REPO,
        schedule=schedule,
        secrets_file=sec.secrets_file_path(),
        db=args.db,
        interval_s=args.interval,
    )
    print(f"# {schedule.explain()}")
    print(f"# target: {agent.PLIST_PATH}")
    print(agent.render(plist))
    if not args.install:
        print("# DRY RUN — nothing was written. Re-run with --install to write this file.")
        print("# Installing a scheduled trading agent is an owner action.")
        return 0
    path = agent.install(plist)
    print(f"# WROTE {path}")
    print(f"# It is NOT loaded yet. To activate:  launchctl load {path}")
    print("# To check afterwards:  qts doctor")
    return 0


def _status(argv: list[str]) -> int:
    from qts_core.clock import TradingClock
    from qts_core.store import StateStore

    ap = argparse.ArgumentParser(prog="qts status")
    ap.add_argument("--db", default="qts_v8/state/paper.db")
    args = ap.parse_args(argv)
    if not Path(args.db).exists():
        print(f"[status] no state db at {args.db} — nothing has run yet")
        return 0
    store = StateStore(args.db)
    try:
        session = TradingClock.system().session_date()
        orders = store.orders_for_session(session)
        decisions = store.decisions_for_session(session)
        ages = [a for _, _, a in store.data_age_samples(session)]
        realized = store.realized_pnl_today(session)
        print(f"[status] session {session}  backend={store.backend()}")
        print(f"[status] decisions={len(decisions)}  orders={len(orders)}")
        print(f"[status] realized P&L today: {realized}c")
        if ages:
            print(
                f"[status] data age n={len(ages)} "
                f"min={min(ages):.1f} median={sorted(ages)[len(ages) // 2]:.1f} "
                f"max={max(ages):.1f} min"
            )
        else:
            print("[status] data age: no samples recorded for this session")
        open_pos = store.open_positions(as_of=session)
        print(f"[status] open positions: {sorted(open_pos) if open_pos else 'none'}")
    finally:
        store.close()
    return 0


def _report(argv: list[str]) -> int:
    from qts_core.clock import TradingClock
    from qts_core.config import StrategyConfig
    from qts_core.report import render_session_report
    from qts_core.store import StateStore

    ap = argparse.ArgumentParser(prog="qts report")
    ap.add_argument("--db", default="qts_v8/state/paper.db")
    ap.add_argument("--out", default=None, help="write here instead of stdout")
    args = ap.parse_args(argv)
    if not Path(args.db).exists():
        print(f"[report] no state db at {args.db}")
        return 2
    store = StateStore(args.db)
    try:
        text = render_session_report(store, StrategyConfig(), TradingClock.system().session_date())
    finally:
        store.close()
    if args.out:
        Path(args.out).write_text(text)
        print(f"[report] written to {args.out}")
    else:
        print(text)
    return 0


def _shell(cmd: list[str]) -> int:
    """Run a sibling script/server, forwarding its exit code verbatim."""
    return subprocess.run(cmd, cwd=REPO, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0
    command, rest = args[0], args[1:]

    if command == "doctor":
        from qts_core import doctor

        return doctor.main(rest)
    if command == "run":
        from qts_core import live

        return live.main(rest)
    if command == "backtest":
        from qts_core import run_backtest

        return run_backtest.main(rest)
    if command == "status":
        return _status(rest)
    if command == "report":
        return _report(rest)
    if command == "install-agent":
        return _install_agent(rest)
    if command == "loop":
        return _shell([str(REPO / "run_session.sh"), *rest])
    if command == "dashboard":
        port = "8787"
        if "--port" in rest:
            port = rest[rest.index("--port") + 1]
        return _shell([sys.executable, "-m", "uvicorn", "qts_core.api:app", "--port", port])

    print(f"qts: unknown command {command!r}\n", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - module entry
    sys.exit(main())
