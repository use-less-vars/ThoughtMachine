"""Container identity environment variables.

Phase-2 identity injection. Every container created by the workspace
managers — the interactive workspace containers (``ContainerManager``) AND
the hidden git resource containers (``ResourceContainerManager`` /
``ContainerRegistry``) — and every ``exec`` run inside them receives the
owning session id and workspace id as environment variables, so in-container
code can attribute its work without the caller passing them by hand.

Constants + one pure merge helper. This module is a leaf: stdlib only, it
imports nothing from the rest of the repo tree, so both container managers
can import it without import cycles and without pulling in agent logging or
the docker SDK. (``container_registry.py`` stays standalone by contract — the
managers merge BEFORE handing the registry a ready-made env dict, so the
registry itself never needs this module.)
"""

SESSION_ID_ENV = "THOUGHTMACHINE_SESSION_ID"
WORKSPACE_ID_ENV = "THOUGHTMACHINE_WORKSPACE_ID"


def merge_container_identity_env(env=None, *, session_id=None, workspace_id=None):
    """Merge session/workspace identity variables into a container env dict.

    Accepts docker's two env shapes — a ``dict`` or a list of ``"K=V"``
    strings — and always returns a plain ``dict`` (which the docker SDK
    accepts natively).

    Injection rules:
      * only truthy identity values are injected (``None``/``""`` omit the
        variable);
      * existing keys are NEVER clobbered — a caller-provided value for
        ``THOUGHTMACHINE_SESSION_ID`` wins over the identity injection;
      * identity values are coerced with ``str()``.

    Args:
        env: base environment (dict, list of ``"K=V"``, or ``None``).
        session_id: value for ``THOUGHTMACHINE_SESSION_ID``.
        workspace_id: value for ``THOUGHTMACHINE_WORKSPACE_ID``.

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
    return out
