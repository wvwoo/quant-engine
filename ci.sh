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
