"""Hermetic regression tests: the container name is an envelope-keyed pool key.

The container NAME is the only pool key (there is no shared in-memory registry;
containers are shared through the durable record store plus ``thoughtmachine.*``
labels keyed by name).  The fix folds the isolation envelope
``(network_mode, workspace_mode)`` into that name via ``env_hash`` so a session
whose network / workspace grant changes can never silently reuse a container
built under the old envelope.

These tests are deliberately HERMETIC (no ``@needs_docker``): they exercise the
pure name builder and route the three naming sites through it without a daemon.

Covered
-------
1. ``build_container_name`` - lifecycle-class distinction, session
   distinguishability (ephemeral), envelope separation, canonical workspace
   normalisation (trailing slash), and the ``_safe_session_tag`` non-empty
   contract (GAP-1).
2. ``verify_container_integrity`` (site 3) and ``DockerExecutor._ensure_container``
   (site 2) resolve their container name via the one builder.
3. Transport round-trip: ``ContainerManager.start`` computes the name from the
   session's CURRENT envelope, so a changed network grant yields a NEW container
   name (lookup miss -> fresh create) rather than the stale name.
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

import docker_executor  # noqa: E402
from docker_executor import (  # noqa: E402
    _safe_session_tag,
    build_container_name,
    normalize_workspace_path,
)
from thoughtmachine.container_record import (  # noqa: E402
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
)

WS = "/tmp/tm-envelope-keying-ws"


# ── 1. the pure builder ────────────────────────────────────────────────────
class TestBuildContainerName:
    def test_persistent_and_ephemeral_are_class_distinguishable(self):
        p = build_container_name(WS, LIFECYCLE_PERSISTENT, network_mode="none",
                                 workspace_mode="ro")
        e = build_container_name(WS, LIFECYCLE_EPHEMERAL, session_id="s1",
                                 network_mode="none", workspace_mode="ro")
        assert p.startswith("agent-exec-persistent-")
        assert e.startswith("agent-exec-ephemeral-")
        assert p != e

    def test_ephemeral_sessions_are_distinguishable(self):
        a = build_container_name(WS, LIFECYCLE_EPHEMERAL, session_id="s1",
                                 network_mode="bridge", workspace_mode="rw")
        b = build_container_name(WS, LIFECYCLE_EPHEMERAL, session_id="s2",
                                 network_mode="bridge", workspace_mode="rw")
        assert a != b
        assert "s1" in a and "s2" in b

    def test_envelope_is_part_of_the_name(self):
        keys = {
            build_container_name(WS, LIFECYCLE_PERSISTENT,
                                 network_mode=nm, workspace_mode=wm)
            for nm, wm in (("none", "ro"), ("bridge", "rw"),
                           ("none", "rw"), ("bridge", "ro"))
        }
        # Every distinct envelope maps to a distinct pool key.
        assert len(keys) == 4

    def test_normalisation_is_canonical(self):
        a = build_container_name(WS, LIFECYCLE_PERSISTENT,
                                 network_mode="none", workspace_mode="ro")
        b = build_container_name(WS + "/", LIFECYCLE_PERSISTENT,
                                 network_mode="none", workspace_mode="ro")
        assert a == b
        assert normalize_workspace_path(WS + "/") == WS

    def test_safe_session_tag_is_never_empty(self):  # GAP-1
        for sid in (None, "", "!!!", "weird/../id", "norm-sess"):
            tag = _safe_session_tag(sid)
            assert tag and str(tag).strip()
        # An empty tag would let two same-workspace ephemerals collapse.
        assert build_container_name(
            WS, LIFECYCLE_EPHEMERAL, session_id="",
            network_mode="none", workspace_mode="ro",
        ).split("-")[3]  # the session segment is non-empty


# ── 2. the two docker_executor sites route through the builder ─────────────
class TestSitesRouteThroughBuilder:
    def test_verify_container_integrity_site3(self, monkeypatch):
        forced_network, forced_mode = "bridge", "rw"
        monkeypatch.setattr(
            docker_executor, "_resolve_container_config_via_gate",
            lambda workspace_id, perms: (forced_network, forced_mode),
        )
        # Force the early "docker unavailable" return deterministically.
        monkeypatch.setattr("docker.from_env", lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("no daemon")))
        result = docker_executor.verify_container_integrity(WS, {})
        expected = build_container_name(
            WS, LIFECYCLE_PERSISTENT,
            network_mode=forced_network, workspace_mode=forced_mode,
        )
        assert result["container_name"] == expected
        assert expected.endswith(
            build_container_name(WS, LIFECYCLE_PERSISTENT,
                                 network_mode="bridge", workspace_mode="rw")[-8:]
        )

    def test_docker_executor_ensure_container_site2(self, monkeypatch):
        from docker_executor import DockerExecutor

        forced_network, forced_mode = "bridge", "rw"
        executor = object.__new__(DockerExecutor)
        executor.workspace_path = WS
        executor.image = "agent-executor"
        executor.network = "none"
        executor.mem_limit = "1g"
        executor.cpu_quota = 100000
        executor.force_rebuild = False
        executor.idle_timeout = 0
        executor.session_permissions = {}
        executor.workspace_id = "ws-envelope-test"
        executor.container = None
        executor.last_used = 0
        executor._timeout_warning_printed = False

        monkeypatch.setattr(executor, "_ensure_image", lambda: None)
        monkeypatch.setattr(executor, "_compute_container_config",
                            lambda: (forced_network, forced_mode))

        seen = {}

        class _Containers:
            def get(self, name):
                seen["name"] = name
                import docker
                raise docker.errors.NotFound(name)

        class _Client:
            containers = _Containers()

        executor.client = _Client()

        # _ensure_container reaches the name + lookup; NotFound -> fresh build.
        try:
            executor._ensure_container()
        except Exception:
            pass
        expected = build_container_name(
            WS, LIFECYCLE_PERSISTENT,
            network_mode=forced_network, workspace_mode=forced_mode,
        )
        assert seen.get("name") == expected
        # The envelope appears (regression guard: no bare agent-exec-{hash}).
        assert seen["name"].count("-") >= 3


# ── 3. transport round-trip: a changed grant -> a NEW pool key ─────────────
class _Env:
    """Minimal ``_ComputedContainerConfig`` stand-in (unpackable + .effective)."""

    def __init__(self, network, workspace):
        self._pair = (network, workspace)
        self.effective = {}

    def __iter__(self):
        return iter(self._pair)

    def __getitem__(self, i):
        return self._pair[i]


class TestStartEnvelopeRoundTrip:
    def _bare_manager(self, envelope):
        from infra.container_manager import ContainerManager

        mgr = object.__new__(ContainerManager)
        mgr.workspace_path = WS
        mgr.workspace_id = "ws-envelope-test"
        mgr.session_id = "sess-envelope"
        mgr.session_permissions = {}
        mgr.image = "agent-executor"
        mgr._containers = {}
        mgr._session_config = None
        mgr.workspace_config = {"max_containers": 10}
        mgr.max_containers = 10

        state = {"envelope": envelope}
        registry = []  # durable "workspace label" view of existing containers
        fresh = []

        def _fresh_start(**kwargs):
            fresh.append(kwargs["name"])
            registry.append({"name": kwargs["name"],
                             "container_id": "cid-" + kwargs["name"],
                             "status": "running", "note": ""})
            return {"id": "cid-" + kwargs["name"], "name": kwargs["name"],
                    "status": "created", "note": kwargs.get("note")}

        mgr._compute_config = lambda *a, **k: _Env(*state["envelope"])
        mgr._migrate_legacy_notes_once = lambda: None
        mgr._ensure_name_index = lambda: None
        mgr._migrate_records_v3_once = lambda: None
        mgr._name_collision = lambda name: False
        mgr._record_for_name = lambda name: None
        mgr._find_by_labels = lambda name: None
        mgr.list_containers = lambda: list(registry)
        mgr._get_max_containers = lambda: 10
        mgr._active_containers = lambda entries: []
        mgr._fresh_start = _fresh_start
        return mgr, state, registry, fresh

    def test_changed_network_grant_yields_new_key(self, monkeypatch):
        from infra import container_manager

        monkeypatch.setattr(container_manager, "_host_ids", lambda: (1000, 1000))

        mgr, state, registry, fresh = self._bare_manager(("none", "ro"))

        # Grant A: locks down (none/ro) -> fresh create.
        r1 = mgr.start(lifecycle_class=LIFECYCLE_PERSISTENT)
        name_a = r1["name"]
        assert fresh == [name_a]

        # Same grant again: the SAME key is reused (no second create).
        state["envelope"] = ("none", "ro")
        r2 = mgr.start(lifecycle_class=LIFECYCLE_PERSISTENT)
        assert r2["name"] == name_a
        assert r2.get("status") == "reused"
        assert fresh == [name_a]

        # Grant B: the session's network grant is RAISED to bridge/rw. The
        # envelope changes -> the pool key changes -> lookup MISSS the stale
        # container -> a NEW container is created (never the stale one).
        state["envelope"] = ("bridge", "rw")
        r3 = mgr.start(lifecycle_class=LIFECYCLE_PERSISTENT)
        name_b = r3["name"]
        assert name_b != name_a
        assert r3.get("status") == "created"
        assert fresh == [name_a, name_b]

    def test_changed_workspace_mode_also_changes_key(self, monkeypatch):
        from infra import container_manager

        monkeypatch.setattr(container_manager, "_host_ids", lambda: (1000, 1000))
        mgr, state, _registry, fresh = self._bare_manager(("bridge", "ro"))
        r1 = mgr.start(lifecycle_class=LIFECYCLE_PERSISTENT)
        state["envelope"] = ("bridge", "rw")
        r2 = mgr.start(lifecycle_class=LIFECYCLE_PERSISTENT)
        assert r1["name"] != r2["name"]
        assert len(fresh) == 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
