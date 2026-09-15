"""Hermetic tests for exec-path drift admission (ContainerManager.exec).

When the LIVE container's network/workspace isolation is MORE PERMISSIVE than
the session policy desires, ``exec()`` DENIES the command (exit 126, cmd never
invoked).  When it differs but is NOT more permissive it WARNs and runs.  A
clean match runs exactly as before.  A drift EVENT + WARNING + audit fire once
per distinct signature (module-level memoised).

Fakes mirror tests/test_container_env_injection.py; the manager is built via
``ContainerManager.__new__`` so the real Docker client is never touched.
"""

import pytest

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager
from thoughtmachine.container_record import RECORD_LABEL_KEY


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeImageRef:
    def __init__(self, image_id="sha256:deadbeef"):
        self.id = image_id

    def __str__(self):
        return self.id


class _FakeContainer:
    def __init__(self, container_id, name=None, labels=None, attrs=None,
                 exec_result=None):
        self.id = container_id
        self.name = name or container_id
        self.image = _FakeImageRef()
        self.status = "running"
        self.labels = dict(labels or {})
        self.attrs = attrs if attrs is not None else {"State": {"Status": "running"}}
        self.removed = []
        self.exec_calls = []
        self._exec_result = exec_result

    def remove(self, **kwargs):
        self.removed.append(kwargs)

    def reload(self):
        pass

    def exec_run(self, **kwargs):
        self.exec_calls.append(kwargs)
        if self._exec_result is not None:
            return self._exec_result
        return (0, (b"out", b"err"))


class _RaisingAttrsContainer(_FakeContainer):
    @property
    def attrs(self):
        raise RuntimeError("attrs unavailable")

    def __init__(self, container_id, name=None):
        # Deliberately avoid ``self.attrs = ...`` (the property has no setter).
        self.id = container_id
        self.name = name or container_id
        self.image = _FakeImageRef()
        self.status = "running"
        # Carry a container RECORD label like the other fakes so the refusal
        # path can record its event against a real record id.
        self.labels = {RECORD_LABEL_KEY: "rec-1"}
        self.removed = []
        self.exec_calls = []
        self._exec_result = None


class _FakeContainers:
    def __init__(self, containers):
        self.containers = list(containers)

    def get(self, container_id):
        for c in self.containers:
            if c.id == container_id or c.name == container_id:
                return c
        raise LookupError(container_id)


class _FakeDockerClient:
    def __init__(self, containers):
        self.containers = _FakeContainers(containers)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cm(client, want=("none", "ro"), workspace_id="w1"):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/ws-test"
    cm.session_id = "s1"
    cm.workspace_id = workspace_id
    cm.session_permissions = {}
    cm.client = client
    cm.workspace_config = {"disk_quota_mb": 0}
    cm._compute_config = lambda *a, **k: want
    return cm


def _command_ran(ctr, command="echo hi"):
    """True if ``command`` was actually exec'd (ignores the usage-log write)."""
    return any(call.get("cmd") == ["/bin/sh", "-c", command]
               for call in ctr.exec_calls)


def _attrs(network, workspace_rw):
    return {
        "State": {"Status": "running"},
        "HostConfig": {"NetworkMode": network},
        "Mounts": [{"Destination": "/workspace", "RW": workspace_rw}],
    }


@pytest.fixture(autouse=True)
def _clear_memo():
    container_manager._EXEC_DRIFT_SEEN.clear()
    yield
    container_manager._EXEC_DRIFT_SEEN.clear()


@pytest.fixture
def events(monkeypatch):
    """Capture record events via a patched thoughtmachine.container_record."""
    import thoughtmachine.container_record as cr

    recorded = []

    def _fake_append(workspace_id, record_id, event_type, actor,
                     vault_root=None, **payload):
        recorded.append({
            "workspace_id": workspace_id,
            "record_id": record_id,
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
        })

    monkeypatch.setattr(cr, "append_event", _fake_append, raising=True)
    return recorded


@pytest.fixture
def warnings(monkeypatch):
    """Capture ``log(...)`` calls emitted by ``infra.container_manager``.

    Same idiom as the ``events`` fixture: patch the module-level logging entry
    point so the emitted (level, component, message) triples are recorded and
    can be asserted on.
    """
    calls = []

    def _fake_log(level, component, message, *args, **kwargs):
        calls.append((level, component, message))

    monkeypatch.setattr(container_manager, "log", _fake_log, raising=True)
    return calls


# ---------------------------------------------------------------------------
# (a) clean match -> runs exactly as today, no drift key, no event
# ---------------------------------------------------------------------------


def test_clean_match_runs_without_drift(events):
    ctr = _FakeContainer(
        "c1", labels={RECORD_LABEL_KEY: "rec-1"},
        attrs=_attrs("none", False),  # policy ("none","ro")
    )
    cm = _make_cm(_FakeDockerClient([ctr]))
    result = cm.exec("c1", "echo hi")

    assert result["exit_code"] == 0
    assert result["stdout"] == "out"
    assert "drift" not in result
    assert _command_ran(ctr)
    assert events == []


def test_clean_match_rw_policy_runs(events):
    ctr = _FakeContainer("c2", attrs=_attrs("bridge", True))
    cm = _make_cm(_FakeDockerClient([ctr]), want=("bridge", "rw"))
    result = cm.exec("c2", "echo hi")

    assert result["exit_code"] == 0
    assert "drift" not in result
    assert _command_ran(ctr)
    assert events == []


# ---------------------------------------------------------------------------
# (b) policy none/ro vs live bridge/RW -> DENY, cmd NOT run, exit 126
# ---------------------------------------------------------------------------


def test_more_permissive_network_denies_and_does_not_run(events):
    ctr = _FakeContainer(
        "c1", labels={RECORD_LABEL_KEY: "rec-1"},
        attrs=_attrs("bridge", True),  # policy is ("none","ro")
    )
    cm = _make_cm(_FakeDockerClient([ctr]))
    result = cm.exec("c1", "rm -rf /")

    assert result["exit_code"] == 126
    assert "network_more_permissive" in result["stderr"]
    assert result["drift"]["decision"] == "deny"
    assert result["drift"]["reason"] == "network_more_permissive"
    # Command was NEVER executed and the container was NOT torn down.
    assert ctr.exec_calls == []
    assert ctr.removed == []
    # Exactly one event, both deny + warn emit.
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "drift.exec_on_drifted_container"
    assert ev["actor"] == "infra.container_manager.exec"
    assert ev["record_id"] == "rec-1"
    assert ev["payload"]["decision"] == "deny"
    assert ev["payload"]["reason"] == "network_more_permissive"
    assert ev["payload"]["expected"] == {"network_mode": "none", "workspace_mode": "ro"}
    assert ev["payload"]["actual"] == {"network_mode": "bridge", "workspace_mode": "rw"}
    assert "detected_at" in ev["payload"]


def test_absent_workspace_mount_denies_as_workspace_more_permissive(events):
    attrs = {
        "State": {"Status": "running"},
        "HostConfig": {"NetworkMode": "none"},
        "Mounts": [],  # /workspace bind gone -> "absent" (more permissive)
    }
    ctr = _FakeContainer("c1", labels={RECORD_LABEL_KEY: "rec-1"}, attrs=attrs)
    cm = _make_cm(_FakeDockerClient([ctr]))
    result = cm.exec("c1", "echo hi")

    assert result["exit_code"] == 126
    assert result["drift"]["decision"] == "deny"
    assert result["drift"]["reason"] == "workspace_more_permissive"
    assert ctr.exec_calls == []
    assert len(events) == 1


# ---------------------------------------------------------------------------
# (c) differs but NOT more permissive -> WARN then RUN
# ---------------------------------------------------------------------------


def test_less_permissive_workspace_warns_then_runs(events):
    # policy rw, live network equal, workspace is ro (stricter) -> warn.
    ctr = _FakeContainer(
        "c1", labels={RECORD_LABEL_KEY: "rec-1"}, attrs=_attrs("bridge", False),
    )
    cm = _make_cm(_FakeDockerClient([ctr]), want=("bridge", "rw"))
    result = cm.exec("c1", "echo hi")

    assert result["exit_code"] == 0
    assert result["stdout"] == "out"
    assert _command_ran(ctr)
    assert result["drift"]["decision"] == "warn"
    assert result["drift"]["reason"] == "config_differs_not_more_permissive"
    assert len(events) == 1
    assert events[0]["payload"]["decision"] == "warn"


# ---------------------------------------------------------------------------
# (d) memoisation: one emission per distinct signature; decision still applies
# ---------------------------------------------------------------------------


def test_memoised_emission_and_decision_still_applies(events):
    ctr = _FakeContainer(
        "c1", labels={RECORD_LABEL_KEY: "rec-1"}, attrs=_attrs("bridge", True),
    )
    cm = _make_cm(_FakeDockerClient([ctr]))

    r1 = cm.exec("c1", "echo a")
    r2 = cm.exec("c1", "echo b")
    # Same signature -> still denied, but only ONE event emitted.
    assert r1["exit_code"] == 126 and r2["exit_code"] == 126
    assert ctr.exec_calls == []
    assert len(events) == 1

    # Live isolation changes (host) -> a NEW signature -> a SECOND event.
    ctr.attrs = _attrs("host", True)
    r3 = cm.exec("c1", "echo c")
    assert r3["exit_code"] == 126
    assert len(events) == 2
    assert events[1]["payload"]["actual"]["network_mode"] == "host"


# ---------------------------------------------------------------------------
# (e) fail-CLOSED: an UNREADABLE attrs (the read RAISES -> we cannot tell) is
#     REFUSED; LEGIBLE attrs with structurally-absent isolation fields stay a
#     deliberate non-jurisdiction RUN case.
# ---------------------------------------------------------------------------


def test_unreadable_attrs_refuses(events, warnings):
    """Unreadable attrs (the attrs read RAISES) -> REFUSE (fail CLOSED).

    The container's isolation cannot be READ at all, so we cannot tell what it
    is isolated to -> per the ruling we do not run what we cannot prove is
    allowed.  The command is NEVER run: refusal payload (exit 126, empty stdout,
    non-empty "refus*" stderr, NO "drift" key) + a WARNING + an audit event.
    """
    ctr = _RaisingAttrsContainer("c1")
    cm = _make_cm(_FakeDockerClient([ctr]))
    result = cm.exec("c1", "echo hi")

    # Refusal payload shape (deny-style, but with NO drift diff to report).
    assert result["exit_code"] == 126
    assert result["stdout"] == ""
    assert "refus" in result["stderr"].lower()
    assert "drift" not in result
    # Command was NEVER executed.
    assert not _command_ran(ctr)
    assert ctr.exec_calls == []
    # A WARNING was emitted when the container's isolation could not be read.
    assert any(lvl == "WARNING" for lvl, *_ in warnings)
    # An audit event was recorded for the refusal.
    assert events != []
    assert events[0]["workspace_id"] == "w1"
    assert events[0]["event_type"]


def test_outer_classifier_error_refuses(events, warnings, monkeypatch):
    """An error in the OUTER classifier body -> REFUSE (fail CLOSED), exit 126.

    The drift classifier raised before reaching a decision (here the helper the
    outer body calls, ``_config_matches``, is patched to re-raise).  Per the
    ruling we do not run what we cannot prove is allowed: the refusal payload
    (exit 126, empty stdout, non-empty "refus*" stderr, NO "drift" key) is
    returned with a WARNING + an audit event, and the command is NEVER run.
    """
    ctr = _FakeContainer("c1", labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("none", False))

    def _boom(*args, **kwargs):
        raise RuntimeError("classifier exploded")

    cm = _make_cm(_FakeDockerClient([ctr]))
    monkeypatch.setattr(cm, "_config_matches", _boom, raising=True)
    result = cm.exec("c1", "echo hi")

    # Refusal payload shape (deny-style, but with NO drift diff to report).
    assert result["exit_code"] == 126
    assert result["stdout"] == ""
    assert "refus" in result["stderr"].lower()
    assert "drift" not in result
    # Command was NEVER executed.
    assert not _command_ran(ctr)
    assert ctr.exec_calls == []
    # A WARNING was emitted when the classifier failed.
    assert any(lvl == "WARNING" for lvl, *_ in warnings)
    # An audit event was recorded for the refusal.
    assert events != []
    assert events[0]["workspace_id"] == "w1"
    assert events[0]["event_type"]


def test_attrs_none_degrades_to_run(events):
    ctr = _FakeContainer("c1", attrs=None)
    cm = _make_cm(_FakeDockerClient([ctr]))
    result = cm.exec("c1", "echo hi")

    assert result["exit_code"] == 0
    assert "drift" not in result
    assert _command_ran(ctr)
    assert events == []


def test_structurally_absent_attrs_run(events):
    # No HostConfig / Mounts keys at all -> cannot determine -> run.
    ctr = _FakeContainer("c1", attrs={"State": {"Status": "running"}})
    cm = _make_cm(_FakeDockerClient([ctr]))
    result = cm.exec("c1", "echo hi")

    assert result["exit_code"] == 0
    assert "drift" not in result
    assert _command_ran(ctr)
    assert events == []


# ---------------------------------------------------------------------------
# (f) UNRESOLVABLE session policy -> REFUSE (fail CLOSED), cmd NOT run, exit 126
# ---------------------------------------------------------------------------


def test_unresolved_policy_refuses(events, warnings, monkeypatch):
    """The policy SSOT is unavailable -> an unresolvable policy must REFUSE.

    RED-first (R1): production still returns ("run", None) here, so this test
    currently fails (e.g. expected exit_code 126, got 0).  After the fix the
    command is NEVER run: refusal payload (exit 126, empty stdout, non-empty
    "refus*" stderr, NO "drift" key) + a WARNING + an audit event.
    """
    import security.security_gate as sg

    def _boom(_workspace_id):
        raise RuntimeError("capabilities unavailable")

    monkeypatch.setattr(sg, "get_workspace_capabilities", _boom, raising=True)

    ctr = _FakeContainer("c1", labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("bridge", True))
    cm = _make_cm(_FakeDockerClient([ctr]))
    del cm._compute_config  # exercise the REAL resolver (strict=True path)
    result = cm.exec("c1", "echo hi")

    # Refusal payload shape (deny-style, but with NO drift diff to report).
    assert result["exit_code"] == 126
    assert result["stdout"] == ""
    assert "refus" in result["stderr"].lower()
    assert "drift" not in result
    # Command was NEVER executed.
    assert not _command_ran(ctr)
    assert ctr.exec_calls == []
    # A WARNING was emitted when the policy could not be resolved.
    assert any(lvl == "WARNING" for lvl, *_ in warnings)
    # An audit event was recorded for the refusal.
    assert events != []
    assert events[0]["workspace_id"] == "w1"
    assert events[0]["event_type"]


def test_unresolved_non_containerconfig_refuses(events, warnings, monkeypatch):
    """Resolver returns a non-ContainerConfig -> unresolved -> REFUSE (exit 126)."""
    import security.security_gate as sg

    monkeypatch.setattr(sg, "get_workspace_capabilities",
                        lambda _ws: object(), raising=True)
    monkeypatch.setattr(sg, "resolve_container_config",
                        lambda perms, caps, lifecycle: object(), raising=True)

    ctr = _FakeContainer("c1", labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("bridge", True))
    cm = _make_cm(_FakeDockerClient([ctr]))
    del cm._compute_config
    result = cm.exec("c1", "echo hi")

    assert result["exit_code"] == 126
    assert result["stdout"] == ""
    assert "refus" in result["stderr"].lower()
    assert "drift" not in result
    assert not _command_ran(ctr)
    assert ctr.exec_calls == []
    assert any(lvl == "WARNING" for lvl, *_ in warnings)
    assert events != []


# ---------------------------------------------------------------------------
# (g) genuinely RESOLVED restrictive policy (real resolver) -> STILL DENY
# ---------------------------------------------------------------------------


def test_resolved_restrictive_policy_still_denies(events, monkeypatch):
    """A RESOLVED ("none","ro") via the real resolver vs live bridge/RW -> deny."""
    import security.security_gate as sg

    monkeypatch.setattr(sg, "get_workspace_capabilities",
                        lambda _ws: None, raising=True)
    monkeypatch.setattr(
        sg, "resolve_container_config",
        lambda perms, caps, lifecycle: sg.ContainerConfig(
            network_mode="none", workspace_mode="ro",
            effective={}, lifecycle_class=lifecycle),
        raising=True,
    )

    ctr = _FakeContainer("c1", labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("bridge", True))
    cm = _make_cm(_FakeDockerClient([ctr]))
    del cm._compute_config  # exercise the REAL resolver (strict=True path)
    result = cm.exec("c1", "echo hi")

    assert result["exit_code"] == 126
    assert result["drift"]["decision"] == "deny"
    assert result["drift"]["reason"] == "network_more_permissive"
    assert ctr.exec_calls == []
    assert len(events) == 1
    assert events[0]["payload"]["expected"] == {
        "network_mode": "none", "workspace_mode": "ro"}


# ---------------------------------------------------------------------------
# (h) unknown/None WANT on an axis -> "no opinion": no raise, no deny
# ---------------------------------------------------------------------------


def test_none_want_axis_is_no_opinion_never_denies_or_raises(events):
    # (i) None network want; live bridge+rw; workspace want rw (matches).
    ctr_net = _FakeContainer("c1", labels={RECORD_LABEL_KEY: "rec-1"},
                             attrs=_attrs("bridge", True))
    cm_net = _make_cm(_FakeDockerClient([ctr_net]), want=(None, "rw"))
    r_net = cm_net.exec("c1", "echo hi")

    assert r_net["exit_code"] == 0
    assert _command_ran(ctr_net)
    assert r_net["drift"]["decision"] == "warn"
    assert r_net["drift"]["reason"] == "config_differs_not_more_permissive"

    # (ii) None workspace want; live network equal (none); workspace live rw.
    ctr_ws = _FakeContainer("c2", labels={RECORD_LABEL_KEY: "rec-2"},
                            attrs=_attrs("none", True))
    cm_ws = _make_cm(_FakeDockerClient([ctr_ws]), want=("none", None))
    r_ws = cm_ws.exec("c2", "echo hi")

    assert r_ws["exit_code"] == 0
    assert _command_ran(ctr_ws)
    assert r_ws["drift"]["decision"] == "warn"
    assert r_ws["drift"]["reason"] == "config_differs_not_more_permissive"

