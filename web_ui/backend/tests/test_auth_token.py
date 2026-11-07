"""Storage policy for the web UI shared secret (``auth_token.py``).

The boundary decides *who may in*; this module owns the *secret it compares
against*, and its policy is small enough to pin exhaustively:

* a missing token is minted exactly once, 256 bits, hex, ``0o600`` in a
  ``0o700`` directory;
* an existing token is **verified, never rewritten** -- re-running
  ``ensure_token`` must not invalidate cookies already handed out;
* a mode wider than ``0o600`` is reported (``chmod`` in the message) but not
  silently corrected, because this module did not create that file;
* a token file that exists but cannot be used fails **closed** with a
  ``RuntimeError`` naming the path, rather than minting a replacement.

Everything here runs against ``tmp_path``: no test touches the real vault.
"""

from __future__ import annotations

import logging
import os
import stat

import pytest

from web_ui.backend import auth_token

#: Same shape ``auth_token`` mints: 64 hex characters (32 bytes).
KNOWN_TOKEN = "ab" * 32

#: POSIX permission bits are emulated on Windows, so the mode assertions are
#: POSIX-only -- the same reason ``auth_token._AUDIT_MODES`` is.
posix_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits are not meaningful here"
)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def token_file(tmp_path):
    """The path this module would use, rooted in a throw-away directory."""
    return tmp_path / auth_token.STATE_DIRNAME / auth_token.TOKEN_FILENAME


# ── 1. minting ──────────────────────────────────────────────────────────────


def test_missing_token_is_minted_with_the_documented_shape(token_file):
    token = auth_token.ensure_token(path=token_file)

    assert len(token) == 64
    assert token == token.lower()
    assert bytes.fromhex(token)  # decodes as hex...
    assert len(bytes.fromhex(token)) == auth_token.TOKEN_BYTES == 32
    assert token_file.read_text(encoding="ascii").strip() == token


@posix_only
def test_minted_file_and_directory_are_owner_only(token_file):
    auth_token.ensure_token(path=token_file)

    assert _mode(token_file) == auth_token.TOKEN_FILE_MODE == 0o600
    assert _mode(token_file.parent) == auth_token.STATE_DIR_MODE == 0o700


# ── 2. verify, don't rewrite ────────────────────────────────────────────────


def test_second_call_returns_the_same_token_without_touching_the_file(token_file):
    first = auth_token.ensure_token(path=token_file)
    content_before = token_file.read_bytes()
    mtime_before = token_file.stat().st_mtime_ns

    second = auth_token.ensure_token(path=token_file)

    assert second == first
    assert token_file.read_bytes() == content_before
    assert token_file.stat().st_mtime_ns == mtime_before


@posix_only
def test_a_wide_mode_is_reported_but_not_rewritten(token_file, caplog):
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(KNOWN_TOKEN + "\n", encoding="ascii")
    os.chmod(token_file, 0o644)

    with caplog.at_level(logging.WARNING, logger=auth_token.__name__):
        token = auth_token.ensure_token(path=token_file)

    assert token == KNOWN_TOKEN
    # Not silently repaired: this module did not set that mode.
    assert _mode(token_file) == 0o644
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert warnings, "a world-readable token must be reported"
    assert any("chmod" in record.getMessage() for record in warnings)
    assert any(str(token_file) in record.getMessage() for record in warnings)


# ── 3. randomness ───────────────────────────────────────────────────────────


def test_two_fresh_directories_get_different_tokens(tmp_path_factory):
    first = auth_token.ensure_token(path=tmp_path_factory.mktemp("a") / "token")
    second = auth_token.ensure_token(path=tmp_path_factory.mktemp("b") / "token")

    assert first != second


# ── 4. fail closed ──────────────────────────────────────────────────────────


def test_read_token_on_a_missing_file_is_none_not_an_error(tmp_path):
    assert auth_token.read_token(path=tmp_path / "absent") is None


def test_an_empty_token_file_fails_closed_and_names_the_path(token_file):
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("   \n", encoding="ascii")

    with pytest.raises(RuntimeError) as excinfo:
        auth_token.ensure_token(path=token_file)

    assert str(token_file) in str(excinfo.value)
    # A broken store must not be "fixed" by minting a replacement.
    assert token_file.read_text(encoding="ascii") == "   \n"


# ── 5. the lost-creation race ───────────────────────────────────────────────


def test_a_file_created_by_a_racing_process_is_not_clobbered(token_file, monkeypatch):
    """``O_EXCL`` loser keeps the winner's token instead of overwriting it."""
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(KNOWN_TOKEN + "\n", encoding="ascii")

    real_read = auth_token.read_token
    calls = {"n": 0}

    def read_missing_the_first_time(path=None):
        # Model the race: the check saw no file, the create then found one.
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_read(path)

    monkeypatch.setattr(auth_token, "read_token", read_missing_the_first_time)

    token = auth_token.ensure_token(path=token_file)

    assert token == KNOWN_TOKEN
    assert token_file.read_text(encoding="ascii").strip() == KNOWN_TOKEN
