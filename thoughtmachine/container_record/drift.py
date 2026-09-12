"""Read-only container drift detection (container-record subsystem).

This module compares a record's *recorded intent* (its ``intent_snapshot``)
plus the *policy* resolved from the caller's permissions/capabilities against
a live Docker container, and reports the differences as
:class:`DriftFinding` values.  It is deliberately **read-only**: it never
mutates Docker or the vault beyond appending drift events to the record's own
event log via the sanctioned :func:`api.append_event` helper.

Layering
--------
* :func:`classify_drift` -- **pure**, total and side-effect free.  Given a
  record, an optional ``live`` mapping and an optional ``config``, it returns
  the list of findings.  It performs no IO and never raises.
* :func:`detect_record_drift` -- inspects a duck-typed Docker client to build
  the ``live`` mapping, resolves the policy config (fail-closed) and delegates
  to :func:`classify_drift`.  Finding a container is a read; nothing else is
  touched.
* :func:`emit_drift_findings` -- appends a drift event per (deduplicated)
  finding to the record's event log.
* :func:`scan_record` -- the detect-then-emit convenience.

Axis semantics
--------------
Every axis is only compared when its *expected* value is present **and**
non-empty in the snapshot, so an empty/unrecorded axis is never reported as
drift.  Axes are emitted in a fixed order: identity, policy, runtime, image,
hardening.

* **identity** -- the record's ``docker_id`` vs. the live container id.
* **policy** -- the desired ``config`` network/workspace mode vs. the recorded
  snapshot value.  Evaluated even when the live container could not be
  inspected (it depends only on the snapshot and the resolved config).
* **runtime / image / hardening** -- the recorded snapshot value vs. the live
  container's current value.  These require a live container; when the live
  inspection failed they are skipped.

Read-only guarantee
-------------------
This module performs no Docker mutation and imports no ``docker`` package; the
client is accepted duck-typed, so a caller can supply a non-mutating fake.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from security.security_gate import (
    ContainerConfig,
    ContainerConfigError,
    resolve_container_config,
)

from .api import RECORD_LABEL_KEY, append_event, read_event_log
from .models import iso_now
from .snapshot import snapshot_from_attrs

logger = logging.getLogger(__name__)

#: ``actor`` recorded on every drift event appended to a record's log.
ACTOR = "thoughtmachine.container_record.drift"

#: Drift classes.
CLASS_IDENTITY = "identity"
CLASS_POLICY = "policy"
CLASS_RUNTIME = "runtime"
CLASS_IMAGE = "image"
CLASS_HARDENING = "hardening"

#: Drift event types.
EVENT_IDENTITY_CHANGED = "drift.identity_changed"
EVENT_CONTAINER_ABSENT = "drift.container_absent"
EVENT_POLICY_CONFIG_CHANGED = "drift.policy_config_changed"
EVENT_RUNTIME_MISMATCH = "drift.runtime_mismatch"
EVENT_IMAGE_CHANGED = "drift.image_changed"
EVENT_HARDENING_LOST = "drift.hardening_lost"

#: Axis sets, in the order axes within a class are compared.
_POLICY_AXES = ("network_mode", "workspace_mode")
_RUNTIME_AXES = ("network_mode", "workspace_mode", "mem_limit", "cpu_quota", "oom_score_adj")
_IMAGE_AXES = ("image_ref", "image_hash")

#: Key under which the live container id is stored in the ``live`` mapping.
_LIVE_ID_KEY = "id"


@dataclass(frozen=True)
class DriftFinding:
    """A single, immutable drift observation.

    ``expected`` is the recorded/policy intent; ``actual`` is the live (or
    absent) value.  ``signature`` is a stable digest of ``(event_type,
    expected, actual)`` used to deduplicate repeated findings.
    """

    drift_class: str
    event_type: str
    expected: Any
    actual: Any
    signature: str


def signature_for(event_type: str, expected: Any, actual: Any) -> str:
    """Return a stable 16-char digest for an (event_type, expected, actual) triple.

    ``expected``/``actual`` are JSON-serialised with ``sort_keys=True`` and
    ``default=str`` so unusual values (ints, nested dicts, ...) never raise.
    """
    blob = json.dumps(
        {"event_type": event_type, "expected": expected, "actual": actual},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _nonempty(value: Any) -> bool:
    """Return True when *value* is present and not an empty container/string."""
    if value is None:
        return False
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (dict, list, tuple, set)):
        return len(value) > 0
    return True


def _lookup(obj: Any, name: str) -> Any:
    """Return ``obj[name]`` for a mapping or ``obj.name`` for an object.

    Returns ``None`` when neither is available; never raises.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _make_finding(
    drift_class: str, event_type: str, expected: Any, actual: Any
) -> DriftFinding:
    """Build a finding, computing its signature."""
    return DriftFinding(
        drift_class=drift_class,
        event_type=event_type,
        expected=expected,
        actual=actual,
        signature=signature_for(event_type, expected, actual),
    )


def classify_drift(record: Any, live: Any, config: Any) -> list[DriftFinding]:
    """Classify drift for *record* against the ``live`` state and *config*.

    Pure, total and side-effect free: it reads only its arguments and never
    raises (an unexpected failure degrades to the findings collected so far).

    Args:
        record: a :class:`~thoughtmachine.container_record.models.Record`-like
            object exposing ``docker_id``, ``id`` and ``intent_snapshot``.
        live: a mapping of live snapshot values (as built by
            :func:`detect_record_drift`), or ``None`` when the live container
            could not be inspected.  Axes needing live data are skipped when
            this is ``None``.
        config: a resolved
            :class:`~security.security_gate.ContainerConfig` (or a mapping with
            ``network_mode``/``workspace_mode``), or ``None`` when the policy
            could not be resolved.

    Returns:
        The list of :class:`DriftFinding` values, ordered identity, policy,
        runtime, image, hardening.
    """
    findings: list[DriftFinding] = []
    try:
        snapshot = getattr(record, "intent_snapshot", None)
        if not isinstance(snapshot, dict):
            snapshot = {}
        live_map = live if isinstance(live, dict) else {}
        live_present = isinstance(live, dict)

        # ── identity ────────────────────────────────────────────────────
        if live_present:
            expected_id = getattr(record, "docker_id", "") or ""
            actual_id = live_map.get(_LIVE_ID_KEY)
            if (
                _nonempty(expected_id)
                and _nonempty(actual_id)
                and expected_id != actual_id
            ):
                findings.append(
                    _make_finding(
                        CLASS_IDENTITY,
                        EVENT_IDENTITY_CHANGED,
                        expected_id,
                        actual_id,
                    )
                )

        # ── policy (snapshot + config only; no live container required) ──
        if config is not None:
            for axis in _POLICY_AXES:
                expected = _lookup(config, axis)
                actual = snapshot.get(axis)
                if _nonempty(expected) and _nonempty(actual) and expected != actual:
                    findings.append(
                        _make_finding(
                            CLASS_POLICY,
                            EVENT_POLICY_CONFIG_CHANGED,
                            expected,
                            actual,
                        )
                    )

        # ── runtime ─────────────────────────────────────────────────────
        if live_present:
            for axis in _RUNTIME_AXES:
                expected = snapshot.get(axis)
                actual = live_map.get(axis)
                if _nonempty(expected) and expected != actual:
                    findings.append(
                        _make_finding(
                            CLASS_RUNTIME,
                            EVENT_RUNTIME_MISMATCH,
                            expected,
                            actual,
                        )
                    )

        # ── image ───────────────────────────────────────────────────────
        if live_present:
            for axis in _IMAGE_AXES:
                expected = snapshot.get(axis)
                actual = live_map.get(axis)
                if _nonempty(expected) and expected != actual:
                    findings.append(
                        _make_finding(
                            CLASS_IMAGE,
                            EVENT_IMAGE_CHANGED,
                            expected,
                            actual,
                        )
                    )

        # ── hardening ───────────────────────────────────────────────────
        if live_present:
            expected = snapshot.get("hardening")
            actual = live_map.get("hardening")
            if isinstance(expected, dict) and expected and expected != actual:
                findings.append(
                    _make_finding(
                        CLASS_HARDENING,
                        EVENT_HARDENING_LOST,
                        expected,
                        actual,
                    )
                )
    except Exception:  # pragma: no cover - defensive; classify must never raise
        logger.warning("classify_drift encountered an unexpected error", exc_info=True)
    return findings


def _matches_record(record: Any, container: Any) -> bool:
    """Return True when *container* is the one attached to *record*."""
    docker_id = getattr(record, "docker_id", "") or ""
    record_id = getattr(record, "id", None)
    if _nonempty(docker_id) and getattr(container, "id", None) == docker_id:
        return True
    labels = getattr(container, "labels", None)
    if isinstance(labels, dict) and labels.get(RECORD_LABEL_KEY) == record_id:
        return True
    return False


def detect_record_drift(
    record: Any,
    containers: Any,
    *,
    permissions: Any = None,
    capabilities: Any = None,
) -> list[DriftFinding]:
    """Detect drift for *record* against the containers visible via *containers*.

    *containers* is a duck-typed Docker client exposing ``list(all=True)``.
    The record's container is matched by ``docker_id`` or by the record-owned
    label (:data:`api.RECORD_LABEL_KEY`).  A container that cannot be found is
    reported as :data:`EVENT_CONTAINER_ABSENT`.

    Policy resolution is fail-closed: a :class:`ContainerConfigError` (or any
    resolution failure) yields ``config=None`` and is logged; the live
    inspection is guarded so a failure yields ``live=None``.  This function
    never mutates anything.
    """
    # ── resolve policy config (fail-closed) ─────────────────────────────
    config: Any = None
    try:
        resolved = resolve_container_config(
            permissions, capabilities, getattr(record, "lifecycle_class", "")
        )
    except Exception:
        logger.warning("container config resolution raised; treating as unresolved")
        resolved = None
    if isinstance(resolved, ContainerConfigError):
        logger.warning(
            "container config unresolved (%s); policy drift suppressed",
            getattr(resolved, "code", "unknown"),
        )
        config = None
    elif isinstance(resolved, ContainerConfig):
        config = resolved
    else:
        config = None

    # ── locate the container (read-only) ────────────────────────────────
    container = None
    try:
        listed = containers.list(all=True)
    except Exception:
        logger.warning("container listing failed; assuming no container present")
        listed = []
    for candidate in listed or []:
        if _matches_record(record, candidate):
            container = candidate
            break

    if container is None:
        expected = getattr(record, "docker_id", "") or getattr(record, "id", "")
        return [
            _make_finding(
                CLASS_IDENTITY,
                EVENT_CONTAINER_ABSENT,
                expected,
                None,
            )
        ]

    # ── build the live mapping (guard the inspection) ───────────────────
    live: Any
    try:
        live = {
            **snapshot_from_attrs(getattr(container, "attrs")),
            _LIVE_ID_KEY: getattr(container, "id", None),
        }
    except Exception:
        logger.warning("live container inspection failed; live axes skipped")
        live = None

    return classify_drift(record, live, config)


def emit_drift_findings(
    workspace_id: str,
    record: Any,
    findings: list[DriftFinding],
    *,
    vault_root: Any = None,
) -> list[DriftFinding]:
    """Append a drift event per finding to *record*'s log; return those emitted.

    Inferred (migration-synthesised) records emit nothing.  A finding is
    skipped when the most recent existing event of the same ``event_type``
    carries the same ``signature`` -- so repeated scans do not duplicate
    entries.

    The appended event payload contains exactly: ``class``, ``expected``,
    ``actual``, ``signature`` and ``detected_at``.
    """
    if getattr(record, "inferred", False):
        return []

    record_id = getattr(record, "id", None)
    emitted: list[DriftFinding] = []
    for finding in findings or []:
        try:
            log = read_event_log(workspace_id, record_id, vault_root=vault_root)
        except Exception:
            logger.warning("could not read event log for drift dedup; emitting")
            log = []

        prior = [
            entry
            for entry in log
            if isinstance(entry, dict) and entry.get("event_type") == finding.event_type
        ]
        if prior:
            # ``read_event_log`` returns entries OLDEST-FIRST (verified
            # empirically for both the embedded log and the ``.events.jsonl``
            # sidecar), so ``prior[-1]`` is the MOST RECENT event of this type.
            # The spec rule is: skip only when the LAST event of that type
            # already carries the same signature.
            last_payload = prior[-1].get("payload")
            if (
                isinstance(last_payload, dict)
                and last_payload.get("signature") == finding.signature
            ):
                continue

        append_event(
            workspace_id,
            record_id,
            finding.event_type,
            ACTOR,
            vault_root=vault_root,
            **{
                "class": finding.drift_class,
                "expected": finding.expected,
                "actual": finding.actual,
                "signature": finding.signature,
                "detected_at": iso_now(),
            },
        )
        emitted.append(finding)
    return emitted


def scan_record(
    record: Any,
    containers: Any,
    *,
    workspace_id: str,
    permissions: Any = None,
    capabilities: Any = None,
    vault_root: Any = None,
) -> list[DriftFinding]:
    """Detect drift for *record*, then emit the findings; return those emitted."""
    findings = detect_record_drift(
        record,
        containers,
        permissions=permissions,
        capabilities=capabilities,
    )
    return emit_drift_findings(workspace_id, record, findings, vault_root=vault_root)


__all__ = [
    "ACTOR",
    "CLASS_IDENTITY",
    "CLASS_POLICY",
    "CLASS_RUNTIME",
    "CLASS_IMAGE",
    "CLASS_HARDENING",
    "EVENT_IDENTITY_CHANGED",
    "EVENT_CONTAINER_ABSENT",
    "EVENT_POLICY_CONFIG_CHANGED",
    "EVENT_RUNTIME_MISMATCH",
    "EVENT_IMAGE_CHANGED",
    "EVENT_HARDENING_LOST",
    "DriftFinding",
    "signature_for",
    "classify_drift",
    "detect_record_drift",
    "emit_drift_findings",
    "scan_record",
]
