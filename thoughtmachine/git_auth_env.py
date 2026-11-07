"""Build the askpass environment that carries a git credential to the child.

W3 ("env plumbing for git auth", ``design_credential_resource.md`` L63) requires
git to be authenticated through the ENVIRONMENT -- ``GIT_ASKPASS`` plus
``GIT_CONFIG_COUNT`` -- and *never* through ``-c http.extraheader``, because
argv is ``/proc``-visible: an ``-c key=value`` argument would publish the
credential to every process on the host.  ``GIT_CONFIG_COUNT`` /
``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n`` achieve the same config override
from the environment, which is not visible in ``/proc/<pid>/cmdline``.

The secret itself is NOT handed to git directly.  It goes into a single env var,
:data:`ASKPASS_SECRET_ENV`, which the askpass program named by ``GIT_ASKPASS``
reads when git asks for a username/password.  ``credential.helper`` is cleared
so no ambient helper (a keychain helper, a ``store`` file) answers first and the
askpass program actually runs; ``GIT_TERMINAL_PROMPT=0`` stops git from blocking
on a terminal prompt that, on a host with no interactive user, nobody can answer.

This module is deliberately PURE: it builds and returns a ``dict`` of ``str``
to ``str``, touches no filesystem, spawns no process and imports no
``subprocess`` (``thoughtmachine/host_port.py`` is the only module allowed to
do that -- see ``tests/test_tool_import_declaration_match.py``).  Writing the
askpass script, choosing the grain and calling ``host_run`` belong to the
caller; this function only answers "what should the child's environment be".

The environment names an askpass program but does not supply one;
:func:`askpass_path` returns the path of the script that ships in this package
(``thoughtmachine/git_askpass.sh``) for callers that have no better candidate.
It is a path computation only -- no file is opened or created here.

Resolution is not done here either (design A5): the caller resolves the
credential with ``agent.credentials.injector.CredentialInjector`` and must
UNWRAP the returned ``Secret`` first, because ``Secret`` is a ``str`` subclass
whose ``str()`` is a mask.  Passing a ``Secret`` straight in is refused below
rather than silently handing git the mask.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

__all__ = ["ASKPASS_SECRET_ENV", "askpass_path", "build_git_auth_env"]


#: Environment variable the askpass program reads the credential from.  One
#: carrier, so there is exactly one key to scrub from audit records and logs.
ASKPASS_SECRET_ENV = "TM_GIT_ASKPASS_SECRET"

#: Config keys that must never be emitted.  ``http.extraheader`` is the
#: ``-c``-shaped form the design explicitly forbids (L63); it is refused even
#: via ``extra_config`` so the invariant cannot be re-introduced by a caller.
FORBIDDEN_CONFIG_KEYS: Tuple[str, ...] = ("http.extraheader",)


def askpass_path() -> str:
    """Return the path of the askpass script shipped inside this package.

    The script sits next to this module and is installed as package data
    (``pyproject.toml`` ``[tool.setuptools.package-data]``) and, for the frozen
    build, as a ``DATA_FILES`` entry in ``thoughtmachine.spec``.

    Only the path is computed: the file is never opened, created or executed
    here, so this module keeps the "pure" promise made in the module docstring
    and the path is correct for an installed wheel as well as for the source
    tree.
    """
    import thoughtmachine

    return str(Path(thoughtmachine.__file__).parent / "git_askpass.sh")


def build_git_auth_env(
    token: str,
    *,
    askpass_path: str,
    username: Optional[str] = None,
    extra_config: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Return the env additions that make ``git`` authenticate via askpass.

    Args:
        token: The credential value, already unwrapped from any ``Secret``.
            Must be a plain non-empty ``str``; a ``Secret`` (or any other
            non-``str``) raises ``TypeError`` -- see the module docstring.
        askpass_path: Absolute path of the askpass program git should run.
            Only referenced, never created or executed here.
        username: Optional fixed username, emitted as ``credential.username``.
            Useful for token auth schemes that want ``x-access-token`` rather
            than the login git would otherwise ask for.
        extra_config: Optional extra ``key -> value`` config overrides appended
            after the credential entries.  ``http.extraheader`` is refused.

    Returns:
        A new ``dict`` of ``str`` to ``str`` holding ``GIT_ASKPASS``,
        ``GIT_TERMINAL_PROMPT``, the ``GIT_CONFIG_COUNT`` triple-set and
        :data:`ASKPASS_SECRET_ENV`.  The token appears in exactly one value.

    Raises:
        TypeError: If ``token``, ``askpass_path``, ``username`` or a config
            entry is not a plain ``str`` (``Secret`` included).
        ValueError: If ``token`` or ``askpass_path`` is empty, or if
            ``extra_config`` asks for a forbidden config key.
    """
    _require_plain_str(token, "token")
    if not token.strip():
        raise ValueError(
            "token must be a non-empty credential value; an empty token makes "
            "git fall back to an interactive prompt that no host caller can "
            "answer"
        )
    _require_plain_str(askpass_path, "askpass_path")
    if not askpass_path.strip():
        raise ValueError(
            "askpass_path must be a non-empty path to the askpass program; "
            "GIT_ASKPASS cannot be empty"
        )

    config: List[Tuple[str, str]] = [("credential.helper", "")]
    if username is not None:
        _require_plain_str(username, "username")
        config.append(("credential.username", username))

    for key, value in (extra_config or {}).items():
        _require_plain_str(key, "extra_config key")
        _require_plain_str(value, f"extra_config value for {key!r}")
        if key.strip().lower() in FORBIDDEN_CONFIG_KEYS:
            raise ValueError(
                f"refusing config key {key!r}: git auth travels through "
                "GIT_ASKPASS, never through http.extraheader (argv and the git "
                "config it implies are visible to other processes)"
            )
        config.append((key, value))

    env: Dict[str, str] = {
        "GIT_ASKPASS": askpass_path,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": str(len(config)),
    }
    for index, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env[ASKPASS_SECRET_ENV] = token
    return env


def _require_plain_str(value: object, label: str) -> None:
    """Raise ``TypeError`` unless ``value`` is an exact ``str`` (not a subclass).

    ``isinstance(value, str)`` is not enough: ``Secret`` is a ``str`` subclass
    whose ``__str__`` returns a mask, so an ``isinstance`` check would let the
    mask through to the askpass program.
    """
    if type(value) is str:
        return
    raise TypeError(
        f"{label} must be a plain str; got {type(value).__name__}. A Secret is "
        "a str subclass that stringifies to a mask, so the askpass program "
        "would receive the mask instead of the credential -- unwrap it at the "
        "call site first."
    )
