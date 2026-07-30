"""Suite-wide isolation.

The secrets loader falls back to ~/.config/secrets.env. Without this fixture a
test that merely unsets APCA_API_KEY_ID would silently consult the developer's
REAL secrets file, so the suite's result would depend on whose machine it runs
on — and it would start failing the day the owner legitimately adds Alpaca keys
to that file. Every test therefore gets a path that does not exist unless it
opts in by setting QTS_SECRETS_FILE itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qts_core import secrets as _secrets


@pytest.fixture(autouse=True)
def _isolate_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QTS_SECRETS_FILE", str(tmp_path / "no-secrets-file.env"))
    _secrets.reset_registry()
