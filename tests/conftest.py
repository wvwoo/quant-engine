"""Suite-wide isolation.

The secrets loader falls back to ~/.config/secrets.env. Without this fixture a
test that merely unsets APCA_API_KEY_ID would silently consult the developer's
REAL secrets file, so the suite's result would depend on whose machine it runs
on — and it would start failing the day the owner legitimately adds Alpaca keys
to that file. Every test therefore gets a path that does not exist unless it
opts in by setting QTS_SECRETS_FILE itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from qts_core import secrets as _secrets
from qts_core.providers import yfinance_source as _yfs


@pytest.fixture(autouse=True)
def _isolate_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QTS_SECRETS_FILE", str(tmp_path / "no-secrets-file.env"))
    _secrets.reset_registry()


@pytest.fixture(autouse=True)
def _no_real_throttling(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Zero the shared provider rate limiter for tests.

    The limiter is a process-wide singleton with a 1.0s default interval, so a
    provider test suite driving a fake transport still slept once per call —
    36 seconds for 70 tests, paid on every ci.sh run. Nothing leaves the process
    (pytest-socket blocks AF_INET), so there is nothing to throttle. Reset after
    each test too, otherwise the memoised limiter leaks across tests and whether
    one sleeps depends on execution order.
    """
    monkeypatch.setenv("QTS_MIN_REQUEST_INTERVAL_S", "0")
    _yfs.reset_limiter()
    yield
    _yfs.reset_limiter()
