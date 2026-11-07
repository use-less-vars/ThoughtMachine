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

from thoughtmachine.container_record import (
    ContainerRecordError,
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    RECORD_LABEL_KEY,
    find_by_docker_label,
)

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

    The lifecycle-class exclusion is folded into this predicate (rather than
    living in the teardown loop) so the ownership gate and the teardown
    sweep share ONE seam: a container is reclaimable only when it is BOTH
    worker-labelled AND resolves to an ephemeral lifecycle class. FAIL-CLOSED
    on an unresolvable class (recordless container): it resolves to
    ``LIFECYCLE_PERSISTENT`` and is therefore NOT reclaimable — see
    ``is_worker_teardown_excluded_container``.
    """
    labels = getattr(container, "labels", None)
    if labels is None and isinstance(container, dict):
        labels = container.get("labels")
    if not labels:
        return False
    if labels.get(_WORKER_CONTAINER_LABEL) != owner_identity:
        return False
    return not is_worker_teardown_excluded_container(container)


def _container_lifecycle_class(container: Any) -> str:
    """Resolve lifecycle class via the canonical RECORD (monkeypatchable seam).

    Mirrors ``infra.container_manager._container_lifecycle_class``: reads the
    ``RECORD_LABEL_KEY`` label -> ``find_by_docker_label`` ->
    ``record.lifecycle_class``. FAIL-CLOSED: an unresolvable class (no label /
    no record / record-store error / empty class) resolves to
    ``LIFECYCLE_PERSISTENT`` so the caller never reaps a container whose
    lifecycle class cannot be established.

    Kept as a LOCAL twin rather than importing the canonical helper because
    ``worker_container`` sits on an import CYCLE below
    ``infra.container_manager`` (container_manager -> infra.container_create ->
    agent.config.defaults -> agent.config.models -> tools ->
    tools.workspace.worker -> tools.workspace.worker_thread ->
    tools.workspace.worker_container), so the canonical helper cannot be
    imported here at module load. This body MUST stay in sync with the
    canonical fallback (it also preserves the monkeypatchable seam that tests
    patch via ``worker_container.find_by_docker_label``).
    """
    labels = getattr(container, "labels", None)
    if labels is None and isinstance(container, dict):
        labels = container.get("labels")
    if not labels:
        return LIFECYCLE_PERSISTENT
    record_id = labels.get(RECORD_LABEL_KEY)
    if not record_id:
        return LIFECYCLE_PERSISTENT
    try:
        record = find_by_docker_label(record_id)
    except ContainerRecordError:
        return LIFECYCLE_PERSISTENT
    except Exception:
        return LIFECYCLE_PERSISTENT
    if record is None:
        return LIFECYCLE_PERSISTENT
    return str(getattr(record, "lifecycle_class", "") or "") or LIFECYCLE_PERSISTENT


def is_worker_teardown_excluded_container(container: Any) -> bool:
    """True when worker teardown must SKIP ``container`` by lifecycle class.

    Orthogonal to ownership: even a container carrying this worker's
    ``thoughtmachine.worker`` label must not be reaped when its lifecycle
    class is not ephemeral. FAIL-CLOSED on an unresolved class: an
    unresolvable lifecycle class resolves to ``LIFECYCLE_PERSISTENT`` (see
    ``_container_lifecycle_class``), which is not ephemeral, so such a
    container is EXCLUDED and is never reclaimed. There is no "unknown class"
    escape hatch.
    """
    cls = _container_lifecycle_class(container)
    return cls != LIFECYCLE_EPHEMERAL


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
