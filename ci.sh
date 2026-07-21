#!/bin/sh
# Local CI gate — the whole truth in one command. Any failure fails the build.
#   ./ci.sh          full gate
#   ./ci.sh fast     tests only
set -eu
cd "$(dirname "$0")"
PY=.venv/bin/python

echo "== pytest (network disabled, warnings-as-errors per pyproject) =="
$PY -m pytest

if [ "${1:-}" = "fast" ]; then exit 0; fi

# A repo-wide percentage can look healthy while an entire module sits at 0% —
# api.py, live.py and run_backtest.py did exactly that (500 statements, 14% of
# the package, asserted only by their own docstrings). The per-file floor is
# the part that actually guards; the repo floor stops slow erosion.
echo "== coverage (repo floor ${COV_MIN:-95}%, per-file floor ${COV_FILE_MIN:-85}%) =="
$PY -m coverage run --source=qts_core -m pytest -q > /dev/null
$PY -m coverage report --precision=1 --fail-under="${COV_MIN:-95}"
$PY -m coverage json -o - | COV_FILE_MIN="${COV_FILE_MIN:-85}" $PY -c '
import json, os, sys
floor = float(os.environ["COV_FILE_MIN"])
data = json.load(sys.stdin)["files"]
under = [
    (name, f["summary"]["percent_covered"])
    for name, f in sorted(data.items())
    if f["summary"]["num_statements"] > 0 and f["summary"]["percent_covered"] < floor
]
for name, pct in under:
    print(f"  {name}: {pct:.1f}% < {floor:.0f}% floor")
if under:
    print(f"per-file coverage floor FAILED for {len(under)} file(s)")
    sys.exit(1)
print(f"per-file floor OK: every module >= {floor:.0f}%")
'

echo "== ruff lint (incl. DTZ naive-datetime ban) =="
$PY -m ruff check qts_core tests

echo "== ruff format check =="
$PY -m ruff format --check qts_core tests

echo "== mypy =="
$PY -m mypy

echo "== pip-audit (runtime deps) =="
# NOTE: no pipe here — POSIX sh has no pipefail, and piping to tail would
# swallow a non-zero exit and turn the security gate into decoration.
AUDIT_OUT=$($PY -m pip_audit --local 2>&1) || {
  echo "$AUDIT_OUT" | tail -20
  echo "pip-audit FAILED"
  exit 1
}
echo "$AUDIT_OUT" | tail -3

echo "== ALL GATES GREEN =="
