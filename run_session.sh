#!/bin/sh
# Continuous paper session: steps the whole universe every N seconds while the
# entry window is open, then keeps stepping (to manage exits + force-flat)
# until the session's force-flat time passes.
#
#   ./run_session.sh                 # default 300s cadence
#   INTERVAL=60 ./run_session.sh     # tighter
#
# Every step is independently crash-safe: the state store makes any number of
# kills and restarts safe, so this loop can be stopped and resumed freely.
set -eu
cd "$(dirname "$0")"
PY=.venv/bin/python
DB="${DB:-qts_v8/state/paper.db}"
REPORT="${REPORT:-qts_v8/state/session_report.md}"
INTERVAL="${INTERVAL:-300}"

echo "[loop] db=$DB interval=${INTERVAL}s — Ctrl-C to stop (state survives)"
while :; do
    if ! $PY -c "
import datetime as dt, zoneinfo, sys
sys.path.insert(0, '.')
from qts_core.clock import is_trading_day, session_close_et
ny = dt.datetime.now(zoneinfo.ZoneInfo('America/New_York'))
d = ny.date()
if not is_trading_day(d):
    sys.exit(1)
sys.exit(0 if ny < session_close_et(d) else 1)
"; then
        echo "[loop] outside the session — stopping"
        break
    fi
    $PY -m qts_core.live --db "$DB" --report "$REPORT" || echo "[loop] step failed; continuing"
    sleep "$INTERVAL"
done
echo "[loop] done — report at $REPORT"
