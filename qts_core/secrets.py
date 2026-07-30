"""The one legal place a key enters this system.

WHY THIS EXISTS (it is not convenience). A macOS LaunchAgent does not inherit
the login shell environment. Keys exported in ~/.zshrc, or sourced from
~/.config/secrets.env by an interactive shell, are simply absent from an
unattended run. Before this module the only reader was
`os.environ.get("APCA_API_KEY_ID")` (broker_alpaca.py), so the autonomous
runner could not authenticate at all — not "would fail obscurely", could not.
An explicit file loader is therefore a precondition of unattended operation.

LOAD ORDER (explicit, documented, tested):
  1. the process environment  — wins, always
  2. ~/.config/secrets.env    — ONLY if its mode is exactly 0600

There are ZERO embedded values here, asserted over this file's own AST by
tests/test_secrets.py. ADR-009 exists because a live-format vendor key once sat
in source as an `os.environ.get` default; the rule that replaced it is
structural, not a grep for one vendor's prefix.

A file whose mode is not 0600 is REFUSED, loudly, with the chmod that fixes it.
It is not read "just this once with a warning": a warning that is printed into
a log nobody reads is indistinguishable from silence, and the whole point of
the file is that it holds material an attacker wants.

Values are NEVER exported into os.environ. run_session.sh spawns live.py as a
fresh subprocess on every step, so exporting would hand every secret to every
child process for the lifetime of the session.

WHAT THIS MODULE CANNOT DO — stated because the alternative is a lie. Alpaca
documents no key id/secret format anywhere: no prefix, no length, no character
set (verified against docs.alpaca.markets 2026-07-30). `looks_plausible` is
therefore a HEURISTIC structural check. It separates "you pasted the value
wrong" from "the service rejected your key"; it cannot and does not verify that
a well-formed string is a real credential. Only the network can do that, which
is `qts doctor`'s job.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

# Values registered for redaction. Populated by get()/load_file(); never
# pre-seeded, so an unused module is an inert module.
_REGISTRY: set[str] = set()

# Below this length a "secret" is not distinguishable from ordinary prose, and
# redacting it would corrupt every line of output it happens to appear in.
_MIN_REDACTABLE = 8

REDACTED = "***REDACTED***"

_ACQUISITION_DOC = "KEY_ACQUISITION.md"


class SecretMissing(RuntimeError):
    """A required secret is in neither the environment nor the secrets file."""


class SecretsFileInsecure(RuntimeError):
    """The secrets file exists but its permissions expose it to other users."""


def secrets_file_path() -> Path:
    """Where secrets live. QTS_SECRETS_FILE overrides (tests, launchd)."""
    override = os.environ.get("QTS_SECRETS_FILE")
    if override:
        return Path(override)
    return Path.home() / ".config" / "secrets.env"


def _register(value: str) -> None:
    if len(value) >= _MIN_REDACTABLE:
        _REGISTRY.add(value)


def reset_registry() -> None:
    """Forget every registered value. For tests and long-lived processes."""
    _REGISTRY.clear()


def load_file(path: Path | None = None) -> dict[str, str]:
    """Parse the secrets file into a dict. Absent file -> {}.

    Raises SecretsFileInsecure if the mode is anything other than 0600. Every
    value found is registered for redaction even if no caller asks for it, so
    an accidental dump cannot leak a key this process never used.
    """
    target = secrets_file_path() if path is None else path
    if not target.exists():
        return {}
    mode = stat.S_IMODE(target.stat().st_mode)
    if mode != 0o600:
        raise SecretsFileInsecure(
            f"refusing to read {target}: mode is {mode:04o}, must be 0600 "
            f"(other users can read it). Fix with:  chmod 600 {target}"
        )
    out: dict[str, str] = {}
    for raw in target.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not name:
            continue
        out[name] = value
        _register(value)
    return out


def get(name: str, *, required: bool = True) -> str:
    """Resolve one secret: environment first, then the secrets file.

    An exported-but-EMPTY variable counts as absent — otherwise a stray
    `export APCA_API_KEY_ID=` in a shell profile would permanently shadow a
    perfectly good file value, and the resulting failure would point at the
    file, which is the one place that was actually correct.

    A missing required secret raises with the variable name, the file that was
    consulted, and where the acquisition steps live. "missing key" is not a
    diagnosis; the owner needs to know what to do next.
    """
    from_env = os.environ.get(name, "")
    if from_env:
        _register(from_env)
        return from_env
    # Only now is the file consulted, so a badly-permissioned file can never
    # block a run whose keys are already in the environment.
    value = load_file().get(name, "")
    if value:
        return value
    if required:
        raise SecretMissing(
            f"{name} is not set. Export it, or add it to {secrets_file_path()} "
            f"(mode 0600) as a line `{name}=<value>`. "
            f"Step-by-step acquisition: {_ACQUISITION_DOC}"
        )
    return ""


def looks_plausible(value: str) -> tuple[bool, str]:
    """HEURISTIC structural check. Returns (ok, reason).

    This is deliberately NOT a format validator: Alpaca publishes no key
    prefix, length or character set, so any claim to validate one would be
    invented. What is checked is only what cannot be right under any format —
    emptiness, embedded whitespace, wrapping quotes left over from a paste,
    implausible shortness, non-printable bytes.

    The reason string NEVER contains the value.
    """
    if not value or not value.strip():
        return False, "value is empty"
    if any(ch.isspace() for ch in value):
        return False, "value contains whitespace (a stray newline or a padded paste?)"
    if value[0] in "\"'" or value[-1] in "\"'":
        return False, "value is wrapped in quote characters — paste it unquoted"
    if not all(32 <= ord(ch) < 127 for ch in value):
        return False, "value contains non-printable or non-ASCII characters"
    if len(value) < 8:
        return False, f"value is only {len(value)} characters — implausibly short for a key"
    return True, "structurally plausible (heuristic only; not a vendor format check)"


def redact(text: object) -> str:
    """Replace every registered secret value in `text` with a marker.

    Applied at output boundaries. Non-strings are stringified first so that a
    caller can wrap an exception or a dict without thinking about types — the
    one thing worse than an ugly log line is a leaked key.
    """
    out = str(text)
    # Longest first: a key that contains another registered value as a prefix
    # must not be partially replaced, leaving a readable tail behind.
    for value in sorted(_REGISTRY, key=len, reverse=True):
        if value in out:
            out = out.replace(value, REDACTED)
    return out
