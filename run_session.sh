#!/bin/sh
# Continuous paper session: steps the whole universe every N seconds while the
# XNYS session is open, until the session close passes. (There is no
# entry-window condition here on purpose: after 11:30 ET steps still run to
# manage exits and the force-flat rail; the checklist itself vetoes entries.)
#
#   ./run_session.sh                     # default 300s cadence
#   INTERVAL=60 ./run_session.sh         # tighter
#   WAIT_FOR_OPEN=1 ./run_session.sh     # launch tonight, start at the open
#
# Every step is independently crash-safe: the state store makes any number of
# kills and restarts safe, so this loop can be stopped and resumed freely.
#
# Exit-code contract with the gate script (N-09): the old `if !` treated EVERY
# non-zero exit as "outside the session" — a broken venv (127) or an
# ImportError printed a confident, WRONG "market closed" and exited 0, and no
# session ran all day. Codes now mean things:
#   0 = in session   3 = outside session (the only legitimate stop)
#   anything else = the gate itself failed -> abort LOUDLY, claim nothing.
set -eu
cd "$(dirname "$0")"
PY="${PY:-.venv/bin/python}"
DB="${DB:-qts_v8/state/paper.db}"
REPORT="${REPORT:-qts_v8/state/session_report.md}"
INTERVAL="${INTERVAL:-300}"

GATE='
import datetime as dt, zoneinfo, sys
sys.path.insert(0, ".")
from qts_core.clock import is_trading_day, session_close_et, session_open_et
ny = dt.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
d = ny.date()
mode = sys.argv[1] if len(sys.argv) > 1 else "in_session"
if not is_trading_day(d):
    sys.exit(3)
if mode == "seconds_to_open":
    delta = (session_open_et(d) - ny).total_seconds()
    print(max(0, int(delta)))
    sys.exit(0)
sys.exit(0 if ny < session_close_et(d) else 3)
'

if [ "${WAIT_FOR_OPEN:-0}" = "1" ]; then
    SECS=$("$PY" -c "$GATE" seconds_to_open) || {
        echo "[loop] open-time check FAILED (exit $?) — aborting, not guessing" >&2
        exit 70
    }
    if [ "$SECS" -gt 0 ]; then
        echo "[loop] waiting ${SECS}s until the XNYS open…"
        sleep "$SECS"
    fi
fi

echo "[loop] db=$DB interval=${INTERVAL}s — Ctrl-C to stop (state survives)"
while :; do
    GATE_RC=0
    "$PY" -c "$GATE" in_session || GATE_RC=$?
    case "$GATE_RC" in
        0) ;;
        3) echo "[loop] outside the session — stopping"; break ;;
        *) echo "[loop] session gate FAILED (exit $GATE_RC) — aborting." >&2
           echo "[loop] NOT claiming the market is closed; fix the environment." >&2
           exit "$GATE_RC" ;;
    esac
    STEP_RC=0
    "$PY" -m qts_core.live --db "$DB" --report "$REPORT" || STEP_RC=$?
    case "$STEP_RC" in
        0) ;;
        130|143) echo "[loop] interrupted — stopping (state survives)"; exit "$STEP_RC" ;;
        *) echo "[loop] step failed (exit $STEP_RC); continuing" ;;
    esac
    sleep "$INTERVAL"
done
echo "[loop] done — report at $REPORT"
