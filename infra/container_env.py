"""Container identity environment variables.

Phase-2 identity injection. Every container created by the workspace
managers — the interactive workspace containers (``ContainerManager``) AND
the hidden git resource containers (``ResourceContainerManager`` /
``ContainerRegistry``) — and every ``exec`` run inside them receives the
owning session id and workspace id as environment variables, so in-container
code can attribute its work without the caller passing them by hand.

Phase-3 (W10) extends the same injection to the workspace git identity: the
``git_identity`` pair from ``<vault>/workspaces/<id>/config.json`` is exported
as ``GIT_AUTHOR_NAME`` / ``GIT_AUTHOR_EMAIL`` / ``GIT_COMMITTER_NAME`` /
``GIT_COMMITTER_EMAIL``. Git honours those natively and they outrank every
config file, so a ``git commit`` run inside the container — from a shell
command, a hook, or a script, not only from ``git_write_tool`` — attributes
the commit to the identity the workspace config names instead of whatever the
image baked into ``.gitconfig``.

Constants + pure helpers plus ONE vault read (``workspace_git_identity``).
This module is a leaf: stdlib only, it imports nothing from the rest of the
repo tree, so both container managers can import it without import cycles and
without pulling in agent logging or the docker SDK. (``container_registry.py``
stays standalone by contract — the managers merge BEFORE handing the registry
a ready-made env dict, so the registry itself never needs this module.)
"""

import json
import os

SESSION_ID_ENV = "THOUGHTMACHINE_SESSION_ID"
WORKSPACE_ID_ENV = "THOUGHTMACHINE_WORKSPACE_ID"

# Git identity labels, in the order they are validated, and the environment
# variables each one feeds. These are git's OWN documented identity sources
# (``GIT_AUTHOR_*`` for the author header, ``GIT_COMMITTER_*`` for the
# committer header) and they outrank every config file.
GIT_IDENTITY_ENV = {
    "name": ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"),
    "email": ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"),
}

# Flattened names, for callers that only need to know what may be set.
GIT_IDENTITY_ENV_NAMES = tuple(
    name for names in GIT_IDENTITY_ENV.values() for name in names
)

# Characters git rejects (or silently mangles) inside an ident. Kept in step
# with ``tools/git_write_tool.py``'s ``_IDENTITY_FORBIDDEN_CHARS``.
_IDENTITY_FORBIDDEN_CHARS = "\r\n\x00<>"


def _resolve_vault_root():
    """Return the vault root: ``THOUGHTMACHINE_VAULT_ROOT`` env or ~/.thoughtmachine.

    Mirrors ``thoughtmachine.vault.vault_root()`` (and
    ``infra.resource_container_manager._resolve_vault_root``) without importing
    that module, so this file stays a leaf.
    """
    override = os.environ.get("THOUGHTMACHINE_VAULT_ROOT")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".thoughtmachine")


def workspace_git_identity(workspace_id, *, vault_root=None):
    """Return the usable workspace ``git_identity`` pair, or ``None``.

    Reads ``<vault_root>/workspaces/<workspace_id>/config.json`` and returns
    ``{"name": str, "email": str}`` only when the pair is COMPLETE and usable;
    every other state returns ``None`` (nothing is injected, and git falls back
    to its own resolution exactly as before):

      * no workspace id, missing file, unreadable/invalid JSON, non-dict body;
      * ``git_identity`` absent, or not an object;
      * a label ABSENT from the object, blank/whitespace-only, not a ``str``,
        or carrying an ident-forbidden character (newline, NUL, ``<``, ``>``).

    The blank counts as absent rule is deliberate and matches
    ``tools/git_write_tool.py::_workspace_git_identity``: registration seeds
    every workspace with ``{"name": "", "email": ""}`` so the UI has a key to
    edit, and that seed must never export an empty ``GIT_AUTHOR_NAME`` (git
    rejects an empty ident, which would break commits that work today). Half a
    pair is likewise never injected — a name without an email is exactly the
    wrong attribution this guards against.

    This function NEVER raises: it runs on the container create path, which must
    not fail because a config file is unreadable.

    Args:
        workspace_id: the workspace whose config is read (falsy -> ``None``).
        vault_root: vault root override; defaults to the process-wide default
            (``THOUGHTMACHINE_VAULT_ROOT`` else ``~/.thoughtmachine``), which is
            the SAME root ``git_write_tool`` reads, so the env injection and the
            ``-c`` identity pins always come from one config.

    Returns:
        dict or None: ``{"name": ..., "email": ...}`` when usable.
    """
    if not workspace_id:
        return None
    root = vault_root or _resolve_vault_root()
    config_path = os.path.join(
        root, "workspaces", str(workspace_id), "config.json"
    )
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("git_identity")
    if not isinstance(raw, dict):
        return None
    identity = {}
    for label in GIT_IDENTITY_ENV:
        if label not in raw:
            # Incomplete pair -> "not configured", never "empty ident".
            return None
        value = raw[label]
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value or any(ch in value for ch in _IDENTITY_FORBIDDEN_CHARS):
            return None
        identity[label] = value
    return identity


def git_identity_env(git_identity):
    """Map a ``{"name", "email"}`` pair onto git's identity env variables.

    Returns the four ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` variables for a
    usable pair and ``{}`` otherwise. Usability is the SAME rule
    ``workspace_git_identity`` applies (both strict string labels, non-blank,
    no ident-forbidden characters), so a caller that passes an unvalidated
    mapping cannot smuggle an empty or malformed ident into a container.

    Args:
        git_identity: mapping (or anything else; non-mappings yield ``{}``).

    Returns:
        dict: env-var name -> value (empty when nothing usable).
    """
    if not isinstance(git_identity, dict):
        return {}
    values = {}
    for label in GIT_IDENTITY_ENV:
        if label not in git_identity:
            return {}
        value = git_identity[label]
        if not isinstance(value, str):
            return {}
        value = value.strip()
        if not value or any(ch in value for ch in _IDENTITY_FORBIDDEN_CHARS):
            return {}
        values[label] = value
    out = {}
    for label, names in GIT_IDENTITY_ENV.items():
        for name in names:
            out[name] = values[label]
    return out


def merge_container_identity_env(
    env=None, *, session_id=None, workspace_id=None, git_identity=None
):
    """Merge session/workspace/git identity variables into a container env dict.

    Accepts docker's two env shapes — a ``dict`` or a list of ``"K=V"``
    strings — and always returns a plain ``dict`` (which the docker SDK
    accepts natively).

    Injection rules:
      * only truthy identity values are injected (``None``/``""`` omit the
        variable);
      * existing keys are NEVER clobbered — a caller-provided value for
        ``THOUGHTMACHINE_SESSION_ID`` (or for any ``GIT_*`` identity variable)
        wins over the identity injection;
      * identity values are coerced with ``str()``;
      * ``git_identity`` is injected only when it is a usable pair (see
        :func:`git_identity_env`); ``None``/blank/partial/malformed injects
        nothing at all, so the pre-existing git behaviour is unchanged.

    Args:
        env: base environment (dict, list of ``"K=V"``, or ``None``).
        session_id: value for ``THOUGHTMACHINE_SESSION_ID``.
        workspace_id: value for ``THOUGHTMACHINE_WORKSPACE_ID``.
        git_identity: ``{"name", "email"}`` pair to export as the four git
            identity variables (see :func:`workspace_git_identity`).

    Returns:
        dict: a copy of ``env`` plus the identity variables (the caller's
        object is never mutated).
    """
    out = {}
    if env:
        if isinstance(env, dict):
            out.update(env)
        else:
            # docker list form: ["K=V", ...] — defensive: skip malformed items.
            for item in env:
                key, sep, value = str(item).partition("=")
                if sep and key:
                    out[key.strip()] = value
    if session_id and SESSION_ID_ENV not in out:
        out[SESSION_ID_ENV] = str(session_id)
    if workspace_id and WORKSPACE_ID_ENV not in out:
        out[WORKSPACE_ID_ENV] = str(workspace_id)
    for key, value in git_identity_env(git_identity).items():
        if key not in out:
            out[key] = value
    return out
