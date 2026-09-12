"""Container Record — two-phase create/attach context manager (§7).

A small orchestration layer over the two-phase primitives in
:mod:`thoughtmachine.container_record.api`.  Call sites that create a Docker
container use :func:`record_creation` to:

1. persist a pre-run intent stub (``state="creating"``) *before* the container
   exists — so a crash mid-``run`` never leaves an unrecorded container, and
2. inject the single record-owned Docker label
   (``thoughtmachine.container_id`` = the record's ``id``) into the caller's
   ``labels`` dict, so the created container is named by the record it belongs
   to (§7).

The Docker id is unknown until after ``containers.run`` returns, so the caller
calls :meth:`_RecordHandle.attach` immediately afterwards to bind
``docker_id``/``state`` to the record.

Failure semantics (rollback on any exception raised inside the ``with`` body):

* If the body raises *before* ``attach`` succeeded, the freshly created
  container (if any) is torn down and the intent stub is deleted — the record
  system never keeps a record for a container that failed to come up.
* If the body raises *after* ``attach`` succeeded, the record is left in place
  (the container exists and is bound; teardown is the caller's responsibility).

The intent stub is persisted *before* ``containers.run`` and this seam fails
closed: if ``begin_record`` raises, the exception propagates out of
``record_creation`` and no container is ever created — a container must never
exist without a record (the record subsystem is the source of truth for
ownership/attribution).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any, Iterator

from .api import (
    attach_container,
    begin_record,
    delete_record,
    record_label,
)
from .models import OWNER_WORKSPACE
from .snapshot import snapshot_from_attrs

log = logging.getLogger("infra.container_record.hook")

#: Seconds to wait for a graceful stop before force-removing a rolled-back
#: container.
_STOP_TIMEOUT = 5

#: Snapshot keys whose truthiness counts as "the inspect payload told us
#: something".  A snapshot carrying none of these (and an empty ``hardening``
#: mapping) is all-empty and must not overwrite a natively-authored snapshot.
_EVIDENCE_KEYS: tuple[str, ...] = (
    "network_mode",
    "workspace_mode",
    "mem_limit",
    "cpu_quota",
    "oom_score_adj",
    "image_ref",
    "image_hash",
)


def _snapshot_has_evidence(snapshot: Any) -> bool:
    """Return True when *snapshot* carries any recoverable intent evidence.

    A snapshot is evidence-bearing when any scalar key in
    :data:`_EVIDENCE_KEYS` is truthy, or when the nested ``hardening`` mapping
    is non-empty.  Non-mappings return False.
    """
    if not isinstance(snapshot, Mapping):
        return False
    for key in _EVIDENCE_KEYS:
        if snapshot.get(key):
            return True
    return bool(snapshot.get("hardening"))


class _RecordHandle:
    """Handle yielded by :func:`record_creation`.

    Carries the (possibly ``None``) record, the workspace/vault context needed
    to mutate it, and the container once :meth:`attach` has seen it.

    Attributes:
        record: the current :class:`~thoughtmachine.container_record.models.Record`
            for the intent stub.
        workspace_id: the owning workspace id.
        vault_root: optional vault root override (``None`` → default resolution).
    """

    __slots__ = ("record", "workspace_id", "vault_root", "_container", "_attached")

    def __init__(
        self,
        record: Any,
        workspace_id: str,
        vault_root: str | Any | None,
    ) -> None:
        self.record = record
        self.workspace_id = workspace_id
        self.vault_root = vault_root
        self._container: Any | None = None
        self._attached = False

    @property
    def id(self) -> str | None:
        """The record id, or ``None`` when there is no record."""
        return self.record.id if self.record is not None else None

    @property
    def labels(self) -> dict[str, str]:
        """The record-owned Docker label dict, or ``{}`` with no record."""
        if self.record is None:
            return {}
        return record_label(self.record.id)

    def attach(self, container: Any, state: str | None = None) -> Any:
        """Bind *container*'s Docker id to the record (completion step).

        Called by the call site immediately after ``containers.run`` returns.
        Records the container so a later rollback can tear it down.  Returns the
        updated record (or ``None`` when there is no record).
        """
        self._container = container
        if self.record is None:
            return None
        docker_id = getattr(container, "id", "") or ""
        snapshot: dict | None = None
        # Capture the intent snapshot at attach time only when the record does
        # not already carry one (a natively authored snapshot wins).  Deriving
        # it from the live container's inspect ``attrs`` is best-effort: any
        # failure is logged and the attach proceeds without a snapshot.
        if not self.record.intent_snapshot:
            try:
                attrs = getattr(container, "attrs", None)
                candidate = snapshot_from_attrs(attrs)
                if _snapshot_has_evidence(candidate):
                    snapshot = candidate
            except Exception:  # noqa: BLE001 - snapshot capture must never block attach
                log.warning(
                    "record attach: failed to derive intent snapshot for %s",
                    self.record.id,
                    exc_info=True,
                )
        self.record = attach_container(
            self.workspace_id,
            self.record.id,
            docker_id,
            state,
            intent_snapshot=snapshot,
            vault_root=self.vault_root,
        )
        self._attached = True
        return self.record


def _teardown_container(container: Any) -> None:
    """Best-effort stop + force-remove of a rolled-back container."""
    try:
        container.stop(timeout=_STOP_TIMEOUT)
    except Exception:  # noqa: BLE001 - teardown must never mask the original error
        pass
    try:
        container.remove(force=True)
    except Exception:  # noqa: BLE001
        pass


def _rollback(handle: _RecordHandle) -> None:
    """Undo a partially completed creation.

    Only runs when ``attach`` never succeeded.  Tears down the container (if
    one was created) and deletes the intent stub so no orphan record survives a
    failed creation.
    """
    if handle._attached:
        # Container exists and is bound to the record; leave both in place.
        return
    if handle._container is not None:
        _teardown_container(handle._container)
    if handle.record is not None:
        try:
            delete_record(handle.workspace_id, handle.record.id, vault_root=handle.vault_root)
        except Exception:  # noqa: BLE001 - rollback is best-effort
            log.warning(
                "record_creation rollback: failed to delete stub record %s",
                handle.record.id,
                exc_info=True,
            )


@contextmanager
def record_creation(
    *,
    workspace_id: str,
    lifecycle_class: str,
    owner: str = OWNER_WORKSPACE,
    purpose: str = "",
    intent_snapshot: dict | None = None,
    labels: dict[str, str] | None = None,
    vault_root: str | Any | None = None,
) -> Iterator[_RecordHandle]:
    """Two-phase record context manager for a fresh container creation.

    On entry, persists an intent stub (:func:`begin_record`, ``state="creating"``)
    and — when *labels* is provided — injects the record-owned label into that
    same dict (mutated in place, so the caller's ``labels`` reference carries it
    into ``containers.run``).  Yields a :class:`_RecordHandle`.

    Inside the body the caller creates the container and calls
    :meth:`_RecordHandle.attach` to complete the record.  Any exception raised
    in the body before ``attach`` succeeds triggers a rollback (tear down the
    container, delete the stub).  After a successful ``attach`` the record and
    container are left in place.

    Args:
        workspace_id: owning workspace id (required).
        lifecycle_class: one of ``LIFECYCLE_*`` (validated by ``begin_record``).
        owner: record owner (defaults to ``OWNER_WORKSPACE``).
        purpose: free-text purpose for the record.
        intent_snapshot: optional intent snapshot dict.
        labels: optional Docker label dict to augment with the record label.
        vault_root: optional vault root override.
    """
    # Fail closed: if the intent stub cannot be persisted, propagate and let the
    # caller abort *before* it calls ``containers.run``.  A container must never
    # be created without a record.
    record = begin_record(
        workspace_id,
        lifecycle_class,
        owner,
        purpose,
        intent_snapshot,
        vault_root=vault_root,
    )

    handle = _RecordHandle(record, workspace_id, vault_root)

    if labels is not None:
        labels.update(record_label(record.id))

    try:
        yield handle
    except BaseException:
        _rollback(handle)
        raise


__all__ = ["record_creation"]
