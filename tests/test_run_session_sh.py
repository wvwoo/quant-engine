"""run_session.sh exit-code contract (N-09).

The old gate read `if ! $PY -c "..."` — EVERY non-zero exit meant "outside the
session". A broken venv (127), an ImportError, a corrupt calendar bundle: all
printed a confident, WRONG "market closed" and exited 0, so no session ran all
day and nothing escalated. These tests drive the script with stub interpreters;
no network, no real market data, and the stubs never reach qts_core.live.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "run_session.sh"


def _stub(tmp_path: Path, body: str) -> Path:
    """A fake $PY: a shell script that decides purely by exit code."""
    py = tmp_path / "fake_py"
    py.write_text(f"#!/bin/sh\n{body}\n")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    return py


def _run(py: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PY=str(py), INTERVAL="0")
    return subprocess.run(
        ["/bin/sh", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=30
    )


class TestGateExitContract:
    def test_market_closed_is_a_clean_stop(self, tmp_path: Path) -> None:
        r = _run(_stub(tmp_path, "exit 3"))
        assert r.returncode == 0
        assert "outside the session — stopping" in r.stdout

    def test_gate_crash_aborts_loudly_and_claims_nothing(self, tmp_path: Path) -> None:
        """An ImportError-style failure must NOT read as 'market closed'."""
        r = _run(_stub(tmp_path, "exit 1"))
        assert r.returncode == 1, "a broken gate exited 0 as if the day were over"
        assert "outside the session" not in r.stdout
        assert "gate FAILED" in r.stderr
        assert "NOT claiming the market is closed" in r.stderr

    def test_missing_interpreter_aborts_loudly(self, tmp_path: Path) -> None:
        env = dict(os.environ, PY=str(tmp_path / "does_not_exist"), INTERVAL="0")
        r = subprocess.run(
            ["/bin/sh", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=30
        )
        assert r.returncode != 0
        assert "outside the session" not in r.stdout

    def test_interrupted_step_stops_instead_of_continuing(self, tmp_path: Path) -> None:
        """Ctrl-C during the step used to be swallowed by `|| echo`; the loop
        then fired ANOTHER live step. 130 must stop the loop."""
        # Review finding F3: matching on "$*" was defeated by the GATE
        # source itself containing the literal 'seconds_to_open', so the
        # in-session branch was dead and one branch did double duty by luck.
        # Discriminate on the LAST argument (the gate mode) instead.
        body = (
            "for a; do last=$a; done\n"
            'case "$last" in\n'
            "  seconds_to_open) echo 0; exit 0 ;;\n"
            "  in_session) exit 0 ;;\n"
            "  *) exit 130 ;;\n"  # the live step: interrupted
            "esac"
        )
        r = _run(_stub(tmp_path, body))
        assert r.returncode == 130
        assert "interrupted — stopping" in r.stdout
        assert "continuing" not in r.stdout


class TestSecondsToOpen:
    """WAIT_FOR_OPEN computed TODAY's open only: launched Tuesday evening it
    printed 0 (the bell already rang), the loop started, the in-session gate
    said 'outside' and the overnight launch exited within a second — silently
    defeating the whole point. It now finds the NEXT session's open."""

    def test_prints_a_nonnegative_integer_whenever_invoked(self, tmp_path: Path) -> None:
        # Extract the embedded gate verbatim from the script — testing a copy
        # would let the two drift.
        text = SCRIPT.read_text()
        gate = text.split("GATE='")[1].split("\n'\n")[0]
        r = subprocess.run(
            [str(REPO / ".venv/bin/python"), "-c", gate, "seconds_to_open"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=REPO,
        )
        assert r.returncode == 0, r.stderr
        secs = int(r.stdout.strip())
        assert secs >= 0
        # Never more than ~15 days: beyond that the gate must fail (exit 4),
        # because a calendar that can't find a session in two weeks is broken.
        assert secs <= 15 * 24 * 3600
