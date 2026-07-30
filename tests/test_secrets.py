"""Secret loading, shape validation and redaction.

The load-bearing requirement is NOT convenience: a LaunchAgent does not inherit
the login shell environment, so keys exported in ~/.zshrc or sourced from
~/.config/secrets.env are invisible to an unattended runner. Without an
explicit loader, autonomous operation cannot authenticate at all. That is why
this module exists and why the file path is a first-class input.

The second requirement is honesty about what CANNOT be validated: Alpaca
documents no key prefix, length or character set anywhere, so the shape check
is explicitly heuristic (structural sanity only) and must never claim to know
the vendor's format.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from qts_core import secrets as sec

MARKED = "PKZZTESTKEY0000000001"  # unique dummy; must never appear in output


def _write_secrets(path: Path, body: str, mode: int = 0o600) -> Path:
    path.write_text(body)
    path.chmod(mode)
    return path


class TestFilePath:
    def test_default_is_the_documented_config_location(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("QTS_SECRETS_FILE", raising=False)
        assert sec.secrets_file_path() == Path.home() / ".config" / "secrets.env"

    def test_env_override_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "alt.env"
        monkeypatch.setenv("QTS_SECRETS_FILE", str(target))
        assert sec.secrets_file_path() == target


class TestFileMode:
    def test_missing_file_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("QTS_SECRETS_FILE", str(tmp_path / "nope.env"))
        assert sec.load_file() == {}

    def test_mode_600_is_read(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        p = _write_secrets(tmp_path / "s.env", f"APCA_API_KEY_ID={MARKED}\n")
        monkeypatch.setenv("QTS_SECRETS_FILE", str(p))
        assert sec.load_file() == {"APCA_API_KEY_ID": MARKED}

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o700])
    def test_any_mode_other_than_600_is_refused_with_the_fix_in_the_message(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: int
    ) -> None:
        """A world/group-readable secrets file is never read silently."""
        p = _write_secrets(tmp_path / "s.env", f"APCA_API_KEY_ID={MARKED}\n", mode=mode)
        monkeypatch.setenv("QTS_SECRETS_FILE", str(p))
        with pytest.raises(sec.SecretsFileInsecure) as exc:
            sec.load_file()
        msg = str(exc.value)
        assert "chmod 600" in msg
        assert str(p) in msg
        assert MARKED not in msg  # the refusal must not echo the contents

    def test_refusal_message_names_the_actual_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        p = _write_secrets(tmp_path / "s.env", "A=b\n", mode=0o644)
        monkeypatch.setenv("QTS_SECRETS_FILE", str(p))
        with pytest.raises(sec.SecretsFileInsecure) as exc:
            sec.load_file()
        assert "644" in str(exc.value)


class TestFileParsing:
    def test_comments_blanks_export_prefix_and_quotes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        body = (
            "# a comment\n"
            "\n"
            "   \n"
            "export APCA_API_KEY_ID=plain\n"
            'APCA_API_SECRET_KEY="double"\n'
            "OTHER='single'\n"
            "  SPACED  =  padded  \n"
            "NOT_A_PAIR\n"
        )
        p = _write_secrets(tmp_path / "s.env", body)
        monkeypatch.setenv("QTS_SECRETS_FILE", str(p))
        assert sec.load_file() == {
            "APCA_API_KEY_ID": "plain",
            "APCA_API_SECRET_KEY": "double",
            "OTHER": "single",
            "SPACED": "padded",
        }

    def test_a_value_containing_an_equals_sign_survives(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        p = _write_secrets(tmp_path / "s.env", "K=a=b=c\n")
        monkeypatch.setenv("QTS_SECRETS_FILE", str(p))
        assert sec.load_file()["K"] == "a=b=c"


class TestPrecedence:
    def test_environment_wins_over_the_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        p = _write_secrets(tmp_path / "s.env", "APCA_API_KEY_ID=from_file\n")
        monkeypatch.setenv("QTS_SECRETS_FILE", str(p))
        monkeypatch.setenv("APCA_API_KEY_ID", "from_env")
        assert sec.get("APCA_API_KEY_ID") == "from_env"

    def test_file_is_the_fallback_when_env_is_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        p = _write_secrets(tmp_path / "s.env", f"APCA_API_KEY_ID={MARKED}\n")
        monkeypatch.setenv("QTS_SECRETS_FILE", str(p))
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        assert sec.get("APCA_API_KEY_ID") == MARKED

    def test_an_empty_env_value_is_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An exported-but-empty var must not shadow a real file value."""
        p = _write_secrets(tmp_path / "s.env", f"APCA_API_KEY_ID={MARKED}\n")
        monkeypatch.setenv("QTS_SECRETS_FILE", str(p))
        monkeypatch.setenv("APCA_API_KEY_ID", "")
        assert sec.get("APCA_API_KEY_ID") == MARKED


class TestMissingIsActionable:
    def test_missing_required_secret_names_the_var_the_file_and_the_next_step(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("QTS_SECRETS_FILE", str(tmp_path / "absent.env"))
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        with pytest.raises(sec.SecretMissing) as exc:
            sec.get("APCA_API_KEY_ID")
        msg = str(exc.value)
        assert "APCA_API_KEY_ID" in msg
        assert str(tmp_path / "absent.env") in msg
        assert "KEY_ACQUISITION.md" in msg  # tells the owner where the steps are

    def test_optional_secret_returns_empty_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("QTS_SECRETS_FILE", str(tmp_path / "absent.env"))
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        assert sec.get("APCA_API_KEY_ID", required=False) == ""

    def test_an_insecure_file_does_not_mask_a_present_env_var(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Env is checked first, so a badly-permissioned file is never touched."""
        p = _write_secrets(tmp_path / "s.env", "APCA_API_KEY_ID=x\n", mode=0o644)
        monkeypatch.setenv("QTS_SECRETS_FILE", str(p))
        monkeypatch.setenv("APCA_API_KEY_ID", "from_env")
        assert sec.get("APCA_API_KEY_ID") == "from_env"


class TestShapeIsHeuristicOnly:
    """Alpaca documents NO key format. The check is structural sanity, and it
    says so — it must not pretend to validate a vendor spec."""

    def test_docstring_declares_the_check_heuristic(self) -> None:
        doc = (sec.looks_plausible.__doc__ or "").lower()
        assert "heuristic" in doc

    @pytest.mark.parametrize("good", ["PKTEST123456", "AKFAKE0000000000", "abc123XYZ_-"])
    def test_structurally_sane_values_pass(self, good: str) -> None:
        ok, _ = sec.looks_plausible(good)
        assert ok

    @pytest.mark.parametrize(
        ("bad", "expect"),
        [
            ("", "empty"),
            ("   ", "empty"),
            ("has space", "whitespace"),
            ("tab\tinside", "whitespace"),
            ("trailing\n", "whitespace"),
            ('"quoted"', "quote"),
            ("'quoted'", "quote"),
            ("shrt", "short"),
            ("nonascii-é", "non-printable"),
        ],
    )
    def test_structurally_broken_values_fail_with_the_reason(self, bad: str, expect: str) -> None:
        ok, reason = sec.looks_plausible(bad)
        assert not ok
        assert expect in reason.lower()

    def test_the_reason_never_contains_the_value(self) -> None:
        ok, reason = sec.looks_plausible(f"{MARKED} with space")
        assert not ok
        assert MARKED not in reason


class TestRedaction:
    def test_a_fetched_secret_is_redacted_from_arbitrary_text(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("QTS_SECRETS_FILE", str(tmp_path / "absent.env"))
        monkeypatch.setenv("APCA_API_KEY_ID", MARKED)
        sec.reset_registry()
        sec.get("APCA_API_KEY_ID")
        out = sec.redact(f"HTTP 401 for key {MARKED} rejected")
        assert MARKED not in out
        assert "***REDACTED***" in out

    def test_file_values_are_registered_even_if_never_fetched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A stray dump of the environment must not leak an unused key."""
        p = _write_secrets(tmp_path / "s.env", f"APCA_API_SECRET_KEY={MARKED}\n")
        monkeypatch.setenv("QTS_SECRETS_FILE", str(p))
        sec.reset_registry()
        sec.load_file()
        assert MARKED not in sec.redact(f"leaked {MARKED}")

    def test_redaction_is_a_noop_on_clean_text(self) -> None:
        sec.reset_registry()
        assert sec.redact("nothing to hide") == "nothing to hide"

    def test_short_values_are_never_registered(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Redacting a 1-char value would mangle every line of output."""
        p = _write_secrets(tmp_path / "s.env", "K=a\n")
        monkeypatch.setenv("QTS_SECRETS_FILE", str(p))
        sec.reset_registry()
        sec.load_file()
        assert sec.redact("a banana") == "a banana"

    def test_redact_handles_none_and_non_strings(self) -> None:
        sec.reset_registry()
        assert sec.redact(None) == "None"
        assert sec.redact(42) == "42"


class TestNoEmbeddedSecrets:
    def test_no_module_level_literal_looks_like_credential_material(self) -> None:
        """Zero embedded values, asserted over the AST — not promised in prose.

        ADR-009 exists because a live-format NVIDIA key sat in source as an
        os.environ.get default. A grep for one vendor's prefix would not have
        caught a different vendor's, so the rule is structural: no module-level
        string constant may look like an opaque credential.
        """
        import ast

        tree = ast.parse(Path(sec.__file__).read_text())
        suspect = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and len(node.value) >= 16
            # opaque: key charset, no spaces
            and re.fullmatch(r"[A-Za-z0-9_\-]+", node.value)
            # ...but an ENV_VAR_NAME is a legitimate identifier, not material.
            # Real credentials are mixed-case or punctuated; SCREAMING_SNAKE is a name.
            and not re.fullmatch(r"[A-Z][A-Z0-9_]*", node.value)
        ]
        assert suspect == [], f"credential-shaped literal(s) in source: {suspect}"

    def test_the_guard_would_actually_catch_a_planted_key(self, tmp_path: Path) -> None:
        """Proves the AST rule above has teeth rather than passing vacuously."""
        import ast

        planted = tmp_path / "planted.py"
        planted.write_text('KEY = "nvapi-abcdef0123456789xyz"\nNAME = "APCA_API_KEY_ID"\n')
        tree = ast.parse(planted.read_text())
        suspect = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and len(node.value) >= 16
            and re.fullmatch(r"[A-Za-z0-9_\-]+", node.value)
            and not re.fullmatch(r"[A-Z][A-Z0-9_]*", node.value)
        ]
        assert suspect == ["nvapi-abcdef0123456789xyz"]  # caught the key, not the name


class TestRealFileIsUntouched:
    def test_import_does_not_read_the_secrets_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Import must have no side effects.

        Proof by construction: point the loader at a mode-644 file, then
        re-import. Reading it during import would raise SecretsFileInsecure,
        so a clean reload is evidence that import touches no file.
        """
        import importlib

        p = _write_secrets(tmp_path / "s.env", f"APCA_API_KEY_ID={MARKED}\n", mode=0o644)
        monkeypatch.setenv("QTS_SECRETS_FILE", str(p))
        reloaded = importlib.reload(sec)
        assert reloaded.load_file  # imported fine; nothing was read
        with pytest.raises(reloaded.SecretsFileInsecure):
            reloaded.load_file()  # ...and it still refuses when actually asked


def test_environment_is_left_unmodified(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The loader must not export file values into os.environ.

    Exporting would leak secrets to every child process this system spawns
    (run_session.sh runs live.py as a subprocess on every step)."""
    p = _write_secrets(tmp_path / "s.env", f"BRAND_NEW_VAR={MARKED}\n")
    monkeypatch.setenv("QTS_SECRETS_FILE", str(p))
    sec.load_file()
    assert "BRAND_NEW_VAR" not in os.environ


def test_file_mode_helper_agrees_with_stat(tmp_path: Path) -> None:
    p = _write_secrets(tmp_path / "s.env", "A=bbbbbbbbbbbb\n", mode=0o600)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
