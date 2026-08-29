"""Worker container ownership helpers.

Ownership of containers created inside worker tool calls is established by
the EXACT VALUE of the ``thoughtmachine.worker`` label: a container belongs
to a worker only when the label value EQUALS the worker's owner identity
``<session_id or 'unknown'>:<worker_name>`` (the identity the worker
container bridge stamped when the tool call created it). Containers with a
stale or mismatched value are ignored - they may belong to a sibling worker
or a previous session. Resource containers (``thoughtmachine.resource``
label, ``tm-res-*`` names, or the ``tm-resource-git`` image) are shared
workspace infrastructure managed by the workspace lifecycle manager and are
never touched during worker teardown.

Extracted from ``tools.workspace.worker`` so the ownership predicates and
the teardown sweep can be reused without importing the full worker runtime.
"""

from __future__ import annotations

from typing import Any, Dict

# Ownership label stamped on containers created inside worker tool calls by
# the worker container bridge. Ownership is established by the EXACT VALUE:
# the label must equal the owning worker's owner identity
# ("<session_id or 'unknown'>:<worker_name>") for teardown to reclaim the
# container. Stale values (a sibling worker's identity, a bare worker name,
# a previous session's identity) are deliberately ignored so a worker never
# stops a container it does not own.
_WORKER_CONTAINER_LABEL = "thoughtmachine.worker"
# Label marking shared resource containers (git checkouts, tooling images)
# managed by the workspace lifecycle manager — always excluded from
# worker teardown.
_RESOURCE_CONTAINER_LABEL = "thoughtmachine.resource"


def worker_owner_label(owner_identity: str) -> Dict[str, str]:
    """Return the label dict marking a container as owned by ``owner_identity``.

    ``owner_identity`` is ``"<session_id or 'unknown'>:<worker_name>"`` — the
    value the worker container bridge stamps on containers created inside
    worker tool calls.
    """
    return {_WORKER_CONTAINER_LABEL: owner_identity}


def is_resource_container(container: Any) -> bool:
    """Return True when ``container`` is shared workspace infrastructure.

    Resource containers are managed by the workspace lifecycle manager and
    must never be stopped or removed during worker teardown. Handles both
    the object shape (docker container objects / ``SimpleNamespace`` with
    ``labels``, ``name``, ``image`` attributes) and the dict shape returned
    by ``ContainerManager.list_containers()`` (``container_id``, ``name``,
    ``image`` keys).
    """
    labels = getattr(container, "labels", None)
    if labels is None and isinstance(container, dict):
        labels = container.get("labels")
    if labels and labels.get(_RESOURCE_CONTAINER_LABEL):
        return True
    name = getattr(container, "name", None)
    if name is None and isinstance(container, dict):
        name = container.get("name")
    if name and str(name).startswith("tm-res-"):
        return True
    image = getattr(container, "image", None)
    if image is None and isinstance(container, dict):
        image = container.get("image")
    if image == "tm-resource-git":
        return True
    return False


def is_worker_owned_container(container: Any, owner_identity: str) -> bool:
    """Return True when ``container`` belongs to the given owner identity.

    Ownership is established by an EXACT match: the
    ``thoughtmachine.worker`` label value must equal ``owner_identity``
    (``<session_id or 'unknown'>:<worker_name>`` — see module docstring).
    Stale/mismatched values (sibling workers, bare names, previous sessions)
    are ignored.
    """
    labels = getattr(container, "labels", None)
    if labels is None and isinstance(container, dict):
        labels = container.get("labels")
    if not labels:
        return False
    return labels.get(_WORKER_CONTAINER_LABEL) == owner_identity


def cleanup_worker_containers(container_manager: Any, owner_identity: str) -> None:
    """Stop and remove containers owned by ``owner_identity`` (best-effort).

    Module-level equivalent of ``WorkerThread._cleanup_worker_containers``,
    minus the per-thread ``_containers_cleaned`` idempotency guard (that is
    instance state). Never raises, so it is safe to call from every teardown
    path. Only containers carrying the worker-ownership label matching
    ``owner_identity`` are touched; resource containers are always excluded
    (see ``is_resource_container``).
    """
    if container_manager is None:
        return
    try:
        listed = container_manager.list_containers()
    except Exception:
        return
    if listed is None:
        return
    if not isinstance(listed, (list, tuple)):
        try:
            listed = list(listed)
        except TypeError:
            return
    for container in listed:
        try:
            if is_resource_container(container):
                continue
            if not is_worker_owned_container(container, owner_identity):
                continue
            if isinstance(container, dict):
                target = container.get("container_id") or container.get("name")
            else:
                target = container
            if target is None:
                continue
            try:
                container_manager.stop(target)
            except Exception:
                pass
            try:
                container_manager.remove(target)
            except Exception:
                pass
        except Exception:
            continue
