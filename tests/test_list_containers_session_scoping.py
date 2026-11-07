"""Session-scoping + envelope-tag tests for ``ContainerManager.list_containers``.

Hermetic unit tests (no Docker daemon, no network).  They isolate the three
decision boundaries the listing depends on, instead of emulating raw docker
attrs:

* lifecycle classification -> patch ``infra.container_manager._container_lifecycle_class``
  to read a ``.lifecycle_class`` attribute off the fake container (this is what
  the real ``class_of`` delegates to);
* live isolation -> patch ``infra.container_manager._exec_live_isolation`` (the
  same module-level seam the real method calls);
* the calling session's *wanted* envelope -> patch the manager's ``_compute_config``
  to return a real ``_ComputedContainerConfig`` so the real
  ``_session_envelope_unavailable_reason`` body still runs (isinstance gate,
  ``cfg[0]``/``cfg[1]``, ``_exec_drift_decision`` and the reason mapping).

Behaviour under test (``infra/container_manager.py``):
* resource-lifecycle rows stay hidden;
* ephemeral rows are visible only to the owning session (matched by NAME);
  a session-less manager applies no filter;
* persistent rows are always visible and gain ``unavailable_reason`` only when
  the live isolation is MORE permissive than the session's grant.
"""

import infra.container_manager as cm_mod
from infra.container_manager import (
    ContainerManager,
    _ComputedContainerConfig,
    _safe_session_tag,
)
from thoughtmachine.container_record import (
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_RESOURCE,
)


# ------------------------------------------------------------------ fakes


class _FakeImageRef:
    """``container.image`` fake exposing the ``.tags`` list the manager probes."""

    def __init__(self, tags=None):
        self.tags = list(tags or [])


class _FakeContainer:
    """Minimal container fake.

    ``lifecycle_class`` is carried as a plain attribute so the patched
    ``_container_lifecycle_class`` can classify it without touching docker.
    """

    def __init__(
        self,
        container_id,
        name=None,
        labels=None,
        image_tags=None,
        status="running",
        attrs=None,
        lifecycle_class=LIFECYCLE_PERSISTENT,
    ):
        self.id = container_id
        self.name = name or container_id
        self.labels = labels or {}
        self.image = _FakeImageRef(image_tags)
        self.status = status
        self.attrs = attrs or {}
        self.lifecycle_class = lifecycle_class


class _FakeContainers:
    """``client.containers`` fake: ``list(all=True, filters=...)`` + ``get``."""

    def __init__(self, containers=None):
        self._containers = list(containers or [])

    def list(self, all=True, filters=None):
        return list(self._containers)

    def get(self, container_id):
        for c in self._containers:
            if c.id == container_id or c.name == container_id:
                return c
        raise KeyError(container_id)


class _FakeClient:
    def __init__(self, containers=None):
        self.containers = (
            containers
            if isinstance(containers, _FakeContainers)
            else _FakeContainers(containers)
        )


# ------------------------------------------------------------------ builders


def _make_manager(containers=None, session_id="sessA"):
    """``ContainerManager`` instance that skips ``docker.from_env()``."""
    manager = ContainerManager.__new__(ContainerManager)
    manager.client = _FakeClient(containers=containers or [])
    manager.image = "agent-executor"
    manager._session_config = None
    manager.workspace_id = "ws-1"
    manager.workspace_path = "/tmp/tm-list-containers-ws"
    manager.session_id = session_id
    manager.session_permissions = {"container": True}
    manager.container_notes = {}
    return manager


def _patch_boundaries(monkeypatch, manager, *, live, wanted):
    """Install the three hermetic seams for one listing call."""
    monkeypatch.setattr(cm_mod, "_exec_live_isolation", lambda c: live)
    monkeypatch.setattr(
        cm_mod,
        "_container_lifecycle_class",
        lambda c: getattr(c, "lifecycle_class", LIFECYCLE_PERSISTENT),
    )
    # Real, indexable _ComputedContainerConfig, so the isinstance gate + the
    # cfg[0]/cfg[1] unpacking + the drift decision all run for real.
    manager._compute_config = lambda *a, **k: _ComputedContainerConfig(
        wanted[0], wanted[1]
    )


def _persistent(container_id, name="agent-exec-1"):
    return _FakeContainer(
        container_id,
        name=name,
        labels={"thoughtmachine.workspace_id": "ws-1"},
        image_tags=["agent-executor:latest"],
        attrs={"State": {}},
        lifecycle_class=LIFECYCLE_PERSISTENT,
    )


def _ephemeral_name(session_tag):
    """Build ``agent-exec-ephemeral-<12hex>-<tag>-<8hex>`` (the sole ownership key)."""
    return f"agent-exec-ephemeral-0123456789ab-{session_tag}-deadbeef"


def _ephemeral(container_id, name):
    return _FakeContainer(
        container_id,
        name=name,
        labels={"thoughtmachine.workspace_id": "ws-1"},
        image_tags=["agent-executor:latest"],
        attrs={"State": {}},
        lifecycle_class=LIFECYCLE_EPHEMERAL,
    )


def _resource(container_id="res-ctr"):
    return _FakeContainer(
        container_id,
        name="tm-res-abc123-git",
        labels={
            "thoughtmachine.workspace_id": "ws-1",
            "thoughtmachine.resource": "git",
        },
        image_tags=["tm-resource-git"],
        attrs={"State": {}},
        lifecycle_class=LIFECYCLE_RESOURCE,
    )


# ─────────────────────────── 1-3: persistent envelope tag ───────────────────────────


def test_persistent_network_more_permissive_is_tagged(monkeypatch):
    """Case 1: persistent row MORE permissive on NETWORK than the session."""
    manager = _make_manager(containers=[_persistent("p1")], session_id="sessA")
    _patch_boundaries(monkeypatch, manager, live=("bridge", "ro"), wanted=("none", "ro"))

    result = manager.list_containers()

    assert [row["container_id"] for row in result] == ["p1"]
    assert (
        result[0]["unavailable_reason"]
        == "needs network:write; session has network:none"
    )


def test_persistent_matching_session_has_no_reason_key(monkeypatch):
    """Case 2: persistent row matching the session exactly -> key ABSENT."""
    manager = _make_manager(containers=[_persistent("p1")], session_id="sessA")
    _patch_boundaries(monkeypatch, manager, live=("none", "ro"), wanted=("none", "ro"))

    result = manager.list_containers()

    assert [row["container_id"] for row in result] == ["p1"]
    assert "unavailable_reason" not in result[0]


def test_persistent_workspace_more_permissive_is_tagged(monkeypatch):
    """Case 3: persistent row MORE permissive on WORKSPACE than the session."""
    manager = _make_manager(containers=[_persistent("p1")], session_id="sessA")
    _patch_boundaries(monkeypatch, manager, live=("none", "rw"), wanted=("none", "ro"))

    result = manager.list_containers()

    assert [row["container_id"] for row in result] == ["p1"]
    assert (
        result[0]["unavailable_reason"]
        == "needs filesystem:write; session has filesystem:read"
    )


# ─────────────────────────── 4-7: ephemeral session scoping ───────────────────────────


def test_ephemeral_owned_by_this_session_is_visible(monkeypatch):
    """Case 4: an ephemeral row owned by THIS session is present."""
    name = _ephemeral_name(_safe_session_tag("sessA"))
    manager = _make_manager(containers=[_ephemeral("e1", name)], session_id="sessA")
    _patch_boundaries(monkeypatch, manager, live=("none", "ro"), wanted=("none", "ro"))

    ids = [row["container_id"] for row in manager.list_containers()]

    assert ids == ["e1"]


def test_ephemeral_owned_by_another_session_is_hidden(monkeypatch):
    """Case 5: an ephemeral row owned by ANOTHER session is NOT in the result."""
    name = _ephemeral_name(_safe_session_tag("sessOTHER"))
    manager = _make_manager(containers=[_ephemeral("e2", name)], session_id="sessA")
    _patch_boundaries(monkeypatch, manager, live=("none", "ro"), wanted=("none", "ro"))

    ids = [row["container_id"] for row in manager.list_containers()]

    assert ids == []


def test_ephemeral_anon_tag_hidden_from_session_manager(monkeypatch):
    """Case 6: an ``anon``-tagged ephemeral row (created with session_id=None).

    OBSERVED BEHAVIOUR: for a session-BEARING manager the anon-tagged row is
    EXCLUDED, because the ownership match is by NAME against
    ``_safe_session_tag(self.session_id)`` and ``_safe_session_tag(None)`` is
    the literal ``"anon"`` -- which only equals a manager whose own session tag
    is also ``"anon"`` (i.e. a session-less manager, which applies no filter at
    all, see case 7). This is the intended fail-closed scoping.
    """
    assert _safe_session_tag(None) == "anon"
    manager = _make_manager(
        containers=[_ephemeral("e-anon", _ephemeral_name("anon"))],
        session_id="sessA",
    )
    _patch_boundaries(monkeypatch, manager, live=("none", "ro"), wanted=("none", "ro"))

    ids = [row["container_id"] for row in manager.list_containers()]

    assert ids == []


def test_sessionless_manager_applies_no_filter(monkeypatch):
    """Case 7: manager with session_id=None -> ephemeral rows of every session."""
    containers = [
        _ephemeral("e-a", _ephemeral_name(_safe_session_tag("sessA"))),
        _ephemeral("e-b", _ephemeral_name(_safe_session_tag("sessB"))),
        _ephemeral("e-anon", _ephemeral_name("anon")),
    ]
    manager = _make_manager(containers=containers, session_id=None)
    _patch_boundaries(monkeypatch, manager, live=("none", "ro"), wanted=("none", "ro"))

    ids = sorted(row["container_id"] for row in manager.list_containers())

    assert ids == ["e-a", "e-anon", "e-b"]
    for row in manager.list_containers():
        assert "unavailable_reason" not in row


# ─────────────────────────── 8-9: robustness + regression ───────────────────────────


def test_unreadable_isolation_no_reason_and_no_exception(monkeypatch):
    """Case 8: unreadable/absent isolation attrs -> no raise, no reason key."""
    manager = _make_manager(containers=[_persistent("p1")], session_id="sessA")
    _patch_boundaries(monkeypatch, manager, live=(None, None), wanted=("none", "ro"))

    result = manager.list_containers()

    assert [row["container_id"] for row in result] == ["p1"]
    assert "unavailable_reason" not in result[0]


def test_resource_container_still_hidden(monkeypatch):
    """Case 9: a resource-lifecycle container is still invisible (regression)."""
    manager = _make_manager(
        containers=[_resource(), _persistent("p1")], session_id="sessA"
    )
    _patch_boundaries(monkeypatch, manager, live=("none", "ro"), wanted=("none", "ro"))

    ids = [row["container_id"] for row in manager.list_containers()]

    assert ids == ["p1"]
