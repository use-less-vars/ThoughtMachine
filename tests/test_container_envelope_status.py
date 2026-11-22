"""The session-envelope VERDICT: every row states within / outside / unknown.

Hermetic (no Docker daemon, no network).  The three decision boundaries are
patched individually, exactly as ``test_list_containers_session_scoping.py``
does: lifecycle classification, live isolation (the module-level
``_exec_live_isolation`` seam) and this session's wanted policy (the manager's
``_compute_config``).

The defect these tests pin: the old listing published NOTHING when the verdict
could not be computed.  "Silence" was indistinguishable from "inside the
session's grant", so a row that could not be PROVEN compliant rendered as
compliant.  ``envelope_status`` is now always present and ``unknown`` is a third,
explicit state -- never a second spelling of ``within``.
"""

import json

import infra.container_manager as cm_mod
import security.security_gate as security_gate
import web_ui.backend.server as server_module
from infra.container_manager import (
    ContainerManager,
    _ComputedContainerConfig,
)
from thoughtmachine.container_record import (
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
)
from tools.container_control import ContainerListTool


# ------------------------------------------------------------------ fakes


class _FakeImageRef:
    def __init__(self, tags=None):
        self.tags = list(tags or [])


class _FakeContainer:
    def __init__(self, container_id, name=None, attrs=None,
                 lifecycle_class=LIFECYCLE_PERSISTENT):
        self.id = container_id
        self.name = name or container_id
        self.labels = {"thoughtmachine.workspace_id": "ws-1"}
        self.image = _FakeImageRef(["agent-executor:latest"])
        self.status = "running"
        self.attrs = attrs if attrs is not None else {"State": {}}
        self.lifecycle_class = lifecycle_class


class _FakeContainers:
    def __init__(self, containers=None):
        self._containers = list(containers or [])

    def list(self, all=True, filters=None):
        return list(self._containers)


class _FakeClient:
    def __init__(self, containers=None):
        self.containers = _FakeContainers(containers)


# --------------------------------------------------------------- builders


def _make_manager(containers=None, session_id="sessA"):
    manager = ContainerManager.__new__(ContainerManager)
    manager.client = _FakeClient(containers)
    manager.image = "agent-executor"
    manager.workspace_id = "ws-1"
    manager.workspace_path = "/tmp/tm-envelope-ws"
    manager.session_id = session_id
    manager.session_permissions = {"container": True}
    manager.container_notes = {}
    return manager


def _patch_lifecycle(monkeypatch):
    monkeypatch.setattr(
        cm_mod,
        "_container_lifecycle_class",
        lambda c: getattr(c, "lifecycle_class", LIFECYCLE_PERSISTENT),
    )


def _patch_live(monkeypatch, live):
    """Patch the module-level reader with the value it should return."""
    monkeypatch.setattr(cm_mod, "_exec_live_isolation", lambda *a, **k: live)


def _patch_live_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("attrs unreadable")

    monkeypatch.setattr(cm_mod, "_exec_live_isolation", _boom)


def _patch_wanted(manager, wanted):
    manager._compute_config = lambda *a, **k: _ComputedContainerConfig(
        wanted[0], wanted[1]
    )


def _persistent(container_id="p1"):
    return _FakeContainer(container_id, lifecycle_class=LIFECYCLE_PERSISTENT)


def _ephemeral(container_id="e1"):
    return _FakeContainer(
        container_id,
        name="agent-exec-ephemeral-0123456789ab-deadbeef-deadbeef",
        lifecycle_class=LIFECYCLE_EPHEMERAL,
    )


def _row(monkeypatch, *, live, wanted, containers=None, session_id="sessA"):
    manager = _make_manager(containers or [_persistent()], session_id=session_id)
    _patch_lifecycle(monkeypatch)
    _patch_live(monkeypatch, live)
    _patch_wanted(manager, wanted)
    return manager.list_containers()[0]


# ─────────────────────────── (i) within the grant ───────────────────────────


def test_exactly_matching_isolation_is_within_with_no_reason(monkeypatch):
    """(i) live == wanted on both axes -> ("within", None), no reason key."""
    manager = _make_manager([_persistent()], session_id="sessA")
    _patch_lifecycle(monkeypatch)
    _patch_live(monkeypatch, ("none", "ro"))
    _patch_wanted(manager, ("none", "ro"))

    assert manager.envelope_verdict(_persistent()) == ("within", None)

    row = manager.list_containers()[0]
    assert row["envelope_status"] == "within"
    assert "unavailable_reason" not in row


def test_more_restrictive_than_the_grant_is_still_within(monkeypatch):
    """(i-b) live NARROWER than the grant is compliant, not drift."""
    manager = _make_manager([_persistent()], session_id="sessA")
    _patch_lifecycle(monkeypatch)
    _patch_live(monkeypatch, ("none", "ro"))
    _patch_wanted(manager, ("bridge", "rw"))

    assert manager.envelope_verdict(_persistent()) == ("within", None)


# ─────────────────────── (ii) positively-detected violation ──────────────────


def test_network_violation_is_outside_with_the_enumerated_reason(monkeypatch):
    """(ii) live opens the network the session was denied."""
    manager = _make_manager([_persistent()], session_id="sessA")
    _patch_lifecycle(monkeypatch)
    _patch_live(monkeypatch, ("bridge", "ro"))
    _patch_wanted(manager, ("none", "ro"))

    status, reason = manager.envelope_verdict(_persistent())

    assert status == "outside"
    assert reason == "needs network:write; session has network:none"

    row = manager.list_containers()[0]
    assert row["envelope_status"] == "outside"
    assert row["unavailable_reason"] == "needs network:write; session has network:none"


def test_workspace_violation_is_outside_with_the_enumerated_reason(monkeypatch):
    """(ii-b) live mounts the workspace writable the session may only read."""
    manager = _make_manager([_persistent()], session_id="sessA")
    _patch_lifecycle(monkeypatch)
    _patch_live(monkeypatch, ("none", "rw"))
    _patch_wanted(manager, ("none", "ro"))

    status, reason = manager.envelope_verdict(_persistent())

    assert status == "outside"
    assert reason == "needs filesystem:write; session has filesystem:read"


# ─────────────────────────── (iii) policy unresolvable ───────────────────────


def test_unresolvable_policy_is_unknown_not_within(monkeypatch):
    """(iii) config resolution fails -> "unknown", never a silent "within".

    Runs the REAL ``_compute_config`` so the pre-condition is asserted too: a
    resolution failure yields the plain ``("none", "ro")`` sentinel, which is
    NOT a ``_ComputedContainerConfig`` and therefore proves no grant.  Reading
    that sentinel as "the session wants none/ro" would flip every row to
    "within" -- the exact over-claim this verdict exists to prevent.
    """

    def _raise(*a, **k):
        raise RuntimeError("policy store unreadable")

    monkeypatch.setattr(security_gate, "get_workspace_capabilities", lambda ws: {})
    monkeypatch.setattr(security_gate, "resolve_container_config", _raise)

    manager = _make_manager([_persistent()], session_id="sessA")
    _patch_lifecycle(monkeypatch)
    _patch_live(monkeypatch, ("none", "ro"))

    cfg = manager._compute_config(
        manager.workspace_path, manager.workspace_id,
        manager.session_permissions, LIFECYCLE_PERSISTENT,
    )
    assert not isinstance(cfg, _ComputedContainerConfig)
    assert tuple(cfg) == ("none", "ro")

    assert manager.envelope_verdict(_persistent()) == ("unknown", None)
    row = manager.list_containers()[0]
    assert row["envelope_status"] == "unknown"
    assert "unavailable_reason" not in row


def test_compute_config_raising_is_unknown(monkeypatch):
    """(iii-b) a raising policy resolution degrades to "unknown"."""
    manager = _make_manager([_persistent()], session_id="sessA")
    _patch_lifecycle(monkeypatch)
    _patch_live(monkeypatch, ("none", "ro"))

    def _raise(*a, **k):
        raise RuntimeError("resolver exploded")

    manager._compute_config = _raise

    assert manager.envelope_verdict(_persistent()) == ("unknown", None)
    assert manager.list_containers()[0]["envelope_status"] == "unknown"


# ───────────────────────── (iv) isolation attrs unreadable ───────────────────


def test_live_isolation_raising_is_unknown(monkeypatch):
    """(iv) the attrs read raising is "cannot tell", not "fine"."""
    manager = _make_manager([_persistent()], session_id="sessA")
    _patch_lifecycle(monkeypatch)
    _patch_live_raises(monkeypatch)
    _patch_wanted(manager, ("none", "ro"))

    assert manager.envelope_verdict(_persistent()) == ("unknown", None)
    row = manager.list_containers()[0]
    assert row["envelope_status"] == "unknown"
    assert "unavailable_reason" not in row


def test_real_reader_over_absent_host_config_is_unknown(monkeypatch):
    """(iv-b) structurally absent HostConfig/Mounts -> (None, None) -> unknown."""
    manager = _make_manager([_persistent()], session_id="sessA")
    _patch_lifecycle(monkeypatch)
    _patch_wanted(manager, ("none", "ro"))

    container = _FakeContainer("p1", attrs={"State": {}})
    assert cm_mod._exec_live_isolation(container) == (None, None)

    assert manager.envelope_verdict(container) == ("unknown", None)


# ────────────────────── (v) the key can never be defaulted ───────────────────


def test_tool_never_defaults_a_missing_envelope_status(monkeypatch):
    """(v) a row WITHOUT the key must still lack it after the tool wrapper.

    This is the guard against the tool inventing a verdict: if
    ``ContainerListTool`` ever filled in a default, a row produced by an older
    layer would start claiming a compliance nobody computed.
    """
    rows = [
        {"container_id": "a", "envelope_status": "unknown"},
        {"container_id": "b"},
    ]

    class _Stub:
        def list_containers(self):
            return rows

    tool = ContainerListTool(
        workspace_path="/tmp/test_ws",
        session_permissions={"container": True},
    )
    monkeypatch.setattr(
        ContainerListTool, "_make_manager",
        lambda self, workspace_id=None: _Stub(),
    )

    payload = json.loads(tool.execute())

    assert payload["success"] is True
    assert payload["containers"][0]["envelope_status"] == "unknown"
    assert "envelope_status" not in payload["containers"][1]


def test_tool_docstring_documents_the_three_valued_verdict():
    """The published schema names the verdict and all three of its values."""
    doc = ContainerListTool.__doc__ or ""
    for token in ("envelope_status", "within", "outside", "unknown"):
        assert token in doc


# ──────────────────── (vi) a session-less manager never passes ───────────────


def test_sessionless_manager_reports_unknown_on_every_row(monkeypatch):
    """(vi) no session id -> "unknown" on persistent AND ephemeral rows.

    A manager with no session has no grant to compare against.  Calling that
    "within" (or omitting the key) would render the dashboard's rows as
    compliant on the strength of a comparison that never happened.
    """
    manager = _make_manager([_persistent(), _ephemeral()], session_id=None)
    _patch_lifecycle(monkeypatch)
    _patch_live(monkeypatch, ("none", "ro"))
    _patch_wanted(manager, ("none", "ro"))

    rows = manager.list_containers()

    assert sorted(row["container_id"] for row in rows) == ["e1", "p1"]
    for row in rows:
        assert row["envelope_status"] == "unknown"
        assert "unavailable_reason" not in row


# ──────────────────── view rows + the route session binding ─────────────────


class _ViewManager:
    """Minimal manager surface ``_recordless_view_entry`` touches."""

    def __init__(self, verdict=("unknown", None), raises=False):
        self._verdict = verdict
        self._raises = raises

    def class_of(self, container):
        return LIFECYCLE_PERSISTENT

    def status(self, container_id):
        raise RuntimeError("no daemon")

    def envelope_verdict(self, container, lifecycle_class=None):
        if self._raises:
            raise RuntimeError("verdict exploded")
        return self._verdict


def test_recordless_view_entry_carries_an_outside_verdict():
    """A view row surfaces the enumerated reason when the verdict is outside."""
    entry = server_module._recordless_view_entry(
        _FakeContainer("c1", name="n1"), _ViewManager(("outside", "needs x")), "ws-1")

    assert entry["envelope_status"] == "outside"
    assert entry["unavailable_reason"] == "needs x"


def test_view_entry_never_raises_on_a_failing_verdict():
    """A raising manager degrades to "unknown" with NO reason key."""
    entry = server_module._recordless_view_entry(
        _FakeContainer("c1", name="n1"), _ViewManager(raises=True), "ws-1")

    assert entry["envelope_status"] == "unknown"
    assert "unavailable_reason" not in entry


def test_view_entry_unknown_verdict_is_never_upgraded():
    """An "unknown" verdict stays unknown and carries no reason."""
    entry = server_module._recordless_view_entry(
        _FakeContainer("c1", name="n1"), _ViewManager(("unknown", "stray")), "ws-1")

    assert entry["envelope_status"] == "unknown"
    assert "unavailable_reason" not in entry


class _FakeRequest:
    """Just the request surface ``_request_session_id`` reads (``.headers``)."""

    def __init__(self, headers=None):
        self.headers = headers or {}


def test_containers_route_binds_the_request_session(monkeypatch):
    """The route resolves the session from the query param, then the header.

    Called directly rather than over HTTP: the credential boundary refuses an
    unauthenticated ASGI GET, and this test is about which session id is BOUND,
    not about the boundary.
    """
    captured = {}

    class _StubManager:
        max_containers = 6
        workspace_id = "ws-1"

        def list_containers(self):
            return []

    def _factory(workspace_id, workspace_path="", session_id=None):
        captured["session_id"] = session_id
        return _StubManager()

    monkeypatch.setattr(server_module, "_make_container_manager", _factory)
    monkeypatch.setattr(
        server_module, "_build_container_view_entries", lambda manager, ws: [])

    # Query parameter wins.
    body = server_module.workspace_containers(
        _FakeRequest(), "ws-1", session_id="sessA")
    assert captured["session_id"] == "sessA"
    assert body["containers"] == []

    # Header is the fallback.
    server_module.workspace_containers(
        _FakeRequest({"X-Session-Id": "sessB"}), "ws-1")
    assert captured["session_id"] == "sessB"

    # Neither present: no session is BOUND -- the route must not invent one.
    server_module.workspace_containers(_FakeRequest(), "ws-1")
    assert captured["session_id"] is None
