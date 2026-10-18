"""Shared ``CONTAINER_DESTROY`` destroy-audit formatting.

Every site that actually DESTROYS a Docker container emits exactly ONE
``CONTAINER_DESTROY`` audit event, so the destroy trail is reconstructible
independently of the ``CONTAINER_LIFECYCLE`` decision event (which records the
DECISION, not the destruction).

The *format* lives here and nowhere else.  Each destroy site passes its own
audit sink (``audit_fn``) rather than this module importing one, so the
per-module ``_audit`` seams -- and the monkeypatch seams in
``tests/test_container_destroy_audit.py`` -- keep working.

This module must NOT import :mod:`infra.container_manager`: container_manager
(and, through the lifecycle funnel, most other destroy sites) imports *this*
module at load time, so an import back would cycle.  For the same reason the
single constant it needs from :mod:`thoughtmachine.container_record` is
imported lazily: that package's ``__init__`` imports the record hook, which
emits destroy audits through this module.
"""


def _record_label_key():
    """Return the container-record label key (imported lazily, see above)."""
    from thoughtmachine.container_record.api import RECORD_LABEL_KEY
    return RECORD_LABEL_KEY

#: Audit event name carried by every destroy event.
CONTAINER_DESTROY = "CONTAINER_DESTROY"


def record_id_from_container(container):
    """Return the record id carried by *container*'s labels, or None.

    Module-level twin of :meth:`ContainerManager._record_id_for`, for removal
    sites without a manager instance.  Never raises.
    """
    if container is None:
        return None
    labels = getattr(container, "labels", None)
    if not labels:
        try:
            attrs = getattr(container, "attrs", None) or {}
            labels = (attrs.get("Config") or {}).get("Labels")
        except Exception:
            labels = None
    try:
        value = (labels or {}).get(_record_label_key())
    except Exception:
        return None
    if not value:
        return None
    return str(value)


def container_state_for_audit(container):
    """Return ``running``/``exited``/``absent`` for the destroy audit trail.

    Sampled from the docker handle BEFORE the stop, so the audit records what
    was actually destroyed rather than the post-stop shell.  ``absent`` means
    no handle existed at all (the destroy was decided against a vanished
    container).  Falls back to ``"exited"`` when the state is unreadable --
    the destroy still happened, so a state must be reported.  Never raises.
    """
    if container is None:
        return "absent"
    try:
        attrs = getattr(container, "attrs", None) or {}
        state = attrs.get("State") if isinstance(attrs, dict) else None
        if isinstance(state, dict) and state:
            if state.get("Running") or state.get("Status") == "running":
                return "running"
            if state.get("Status"):
                return "exited"
        status = getattr(container, "status", None)
        if status == "running":
            return "running"
        if status:
            return "exited"
    except Exception:
        pass
    return "exited"


def emit_destroy_audit(audit_fn, source, container, *, reason,
                       container_id=None, detail=""):
    """Emit ONE ``CONTAINER_DESTROY`` audit event for a destroy that proceeds.

    ``audit_fn`` is the calling module's audit sink (its own ``_audit`` seam or
    :func:`thoughtmachine.audit_logger.audit_event`); it is a parameter, not an
    import, so no module has to import another just to log.  ``source`` names
    the destroy site (``container_manager.remove`` / ``executor.reap`` /
    ``executor.close``...) and ``reason`` the lifecycle intent that justified
    it.  ``container_id`` lets a site that has already lost its handle still
    report the id it was asked to destroy.  ``detail`` appends a free-form
    ``detail="..."`` field AFTER the six required fields so a REFUSED destroy
    can record the decision that refused it.

    Best-effort: never raises and never alters control flow -- destroy
    telemetry must never fail (or change the result of) a destroy.
    """
    try:
        cid = container_id or getattr(container, "id", None) or "unknown"
        rid = record_id_from_container(container) or "unknown"
        name = getattr(container, "name", None) or "unknown"
        state = container_state_for_audit(container)
        message = (f"reason={reason} source={source} container_id={cid} "
                   f"record_id={rid} name={name} state={state}")
        if detail:
            message += f' detail="{detail}"'
        audit_fn(CONTAINER_DESTROY, message)
    except Exception:
        pass
