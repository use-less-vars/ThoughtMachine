"""Contract tests for ``thoughtmachine.git_auth_env`` -- the W3c askpass builder.

W3 ("env plumbing for git auth", ``design_credential_resource.md`` L63) says the
credential must reach git through the ENVIRONMENT: ``GIT_ASKPASS`` plus
``GIT_CONFIG_COUNT``, *never* ``-c http.extraheader``, because argv is
``/proc``-visible.  W3c asks for that construction to be a PURE FUNCTION so it
can be unit-tested without spawning anything, and these tests are that unit:
they assert on the returned dict only.

What is pinned here
-------------------
1. The env carries ``GIT_ASKPASS`` and a consistent ``GIT_CONFIG_COUNT`` /
   ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n`` triple-set.
2. ``GIT_TERMINAL_PROMPT=0``: git must fail rather than block a host with no
   interactive user behind it.
3. ``credential.helper`` is cleared, so no ambient helper (a keychain helper, a
   ``store`` file) answers first and the askpass script never runs.
4. The secret VALUE appears in exactly one place -- the env var the askpass
   script reads -- and in no ``GIT_CONFIG_*`` entry and no config KEY, i.e. it
   never reaches argv-shaped or filesystem-shaped surfaces.
5. ``http.extraheader`` is refused outright (design L63).
6. A ``Secret`` (a ``str`` subclass whose ``__str__`` is ``"***"``) is refused
   with ``TypeError``, so a caller cannot hand the askpass script the mask.

THIS FILE WAS DELIBERATELY RED FIRST.  ``thoughtmachine.git_auth_env`` did not
exist when it was written, so the module-level import below failed collection
with ``ModuleNotFoundError: No module named 'thoughtmachine.git_auth_env'`` --
that failure is the RED deliverable for W3c; the module is the GREEN step.

No subprocess, no network, no filesystem writes: the builder takes a *path* for
the askpass script and never creates it.
"""

from __future__ import annotations

import pytest

from thoughtmachine.git_auth_env import (
    ASKPASS_SECRET_ENV,
    build_git_auth_env,
)

from agent.credentials.injector import Secret


#: A syntactically plausible, obviously fake token.  Never a real credential.
FAKE_TOKEN_VALUE = "ghp_this_is_a_fake_value_1111"

#: Path handed to the builder in every test (never created, never executed).
ASKPASS_PATH = "/opt/thoughtmachine/libexec/tm_git_askpass.sh"


def _config_pairs(env):
    """Return ``[(key, value), ...]`` decoded from the ``GIT_CONFIG_*`` env."""
    count = int(env["GIT_CONFIG_COUNT"])
    return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)]


# ---------------------------------------------------------------------------
# 1: the env shape the design asks for
# ---------------------------------------------------------------------------


def test_build_git_auth_env_sets_askpass_and_config_count():
    env = build_git_auth_env(FAKE_TOKEN_VALUE, askpass_path=ASKPASS_PATH)

    assert env["GIT_ASKPASS"] == ASKPASS_PATH
    assert int(env["GIT_CONFIG_COUNT"]) == len(_config_pairs(env)) == 1


def test_build_git_auth_env_disables_terminal_prompt():
    env = build_git_auth_env(FAKE_TOKEN_VALUE, askpass_path=ASKPASS_PATH)

    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_build_git_auth_env_clears_credential_helper():
    env = build_git_auth_env(FAKE_TOKEN_VALUE, askpass_path=ASKPASS_PATH)

    assert _config_pairs(env) == [("credential.helper", "")]


def test_build_git_auth_env_values_are_plain_str():
    env = build_git_auth_env(FAKE_TOKEN_VALUE, askpass_path=ASKPASS_PATH)

    assert all(type(key) is str and type(value) is str for key, value in env.items())


# ---------------------------------------------------------------------------
# 2: the secret never leaves the environment
# ---------------------------------------------------------------------------


def test_build_git_auth_env_carries_secret_only_in_its_env_var():
    env = build_git_auth_env(FAKE_TOKEN_VALUE, askpass_path=ASKPASS_PATH)

    carriers = [key for key, value in env.items() if FAKE_TOKEN_VALUE in value]
    assert carriers == [ASKPASS_SECRET_ENV]
    assert env[ASKPASS_SECRET_ENV] == FAKE_TOKEN_VALUE


def test_build_git_auth_env_never_places_secret_in_git_config():
    env = build_git_auth_env(
        FAKE_TOKEN_VALUE,
        askpass_path=ASKPASS_PATH,
        username="x-access-token",
    )

    assert all(FAKE_TOKEN_VALUE not in key for key in env)
    assert all(FAKE_TOKEN_VALUE not in value for key, value in _config_pairs(env))


def test_build_git_auth_env_never_sets_extraheader_by_default():
    env = build_git_auth_env(FAKE_TOKEN_VALUE, askpass_path=ASKPASS_PATH)

    assert not [key for key in env if "extraheader" in key.lower()]
    assert not [key for key, _ in _config_pairs(env) if "extraheader" in key.lower()]


# ---------------------------------------------------------------------------
# 3: optional entries
# ---------------------------------------------------------------------------


def test_build_git_auth_env_adds_username_config_entry():
    env = build_git_auth_env(
        FAKE_TOKEN_VALUE, askpass_path=ASKPASS_PATH, username="x-access-token"
    )

    assert _config_pairs(env) == [
        ("credential.helper", ""),
        ("credential.username", "x-access-token"),
    ]
    assert env["GIT_CONFIG_COUNT"] == "2"


def test_build_git_auth_env_appends_extra_config_last():
    extra = {"http.sslVerify": "true", "core.askPass": ""}

    env = build_git_auth_env(
        FAKE_TOKEN_VALUE,
        askpass_path=ASKPASS_PATH,
        username="x-access-token",
        extra_config=extra,
    )

    assert _config_pairs(env) == [
        ("credential.helper", ""),
        ("credential.username", "x-access-token"),
        ("http.sslVerify", "true"),
        ("core.askPass", ""),
    ]
    assert env["GIT_CONFIG_COUNT"] == "4"


def test_build_git_auth_env_refuses_extraheader_in_extra_config():
    with pytest.raises(ValueError) as excinfo:
        build_git_auth_env(
            FAKE_TOKEN_VALUE,
            askpass_path=ASKPASS_PATH,
            extra_config={"http.extraheader": "AUTHORIZATION: basic x"},
        )

    assert "extraheader" in str(excinfo.value).lower()
    assert FAKE_TOKEN_VALUE not in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4: refusals -- the Secret trap (A5)
# ---------------------------------------------------------------------------


def test_build_git_auth_env_rejects_secret_token():
    with pytest.raises(TypeError) as excinfo:
        build_git_auth_env(Secret(FAKE_TOKEN_VALUE), askpass_path=ASKPASS_PATH)

    message = str(excinfo.value)
    assert "Secret" in message
    assert "plain str" in message
    # never echo the value, not even a masked one
    assert FAKE_TOKEN_VALUE not in message
    assert "***" not in message


@pytest.mark.parametrize("token", [None, 123, b"ghp_bytes", ["ghp_list"]])
def test_build_git_auth_env_rejects_non_str_token(token):
    with pytest.raises(TypeError):
        build_git_auth_env(token, askpass_path=ASKPASS_PATH)


def test_build_git_auth_env_rejects_empty_token():
    with pytest.raises(ValueError) as excinfo:
        build_git_auth_env("", askpass_path=ASKPASS_PATH)

    assert "token" in str(excinfo.value)


@pytest.mark.parametrize("askpass_path", ["", None, 7])
def test_build_git_auth_env_rejects_unusable_askpass_path(askpass_path):
    with pytest.raises((TypeError, ValueError)):
        build_git_auth_env(FAKE_TOKEN_VALUE, askpass_path=askpass_path)
