"""Container-machine end-to-end smoke tests (G1-G9).

A single, self-contained "smoke" module that pins the nine behaviours that make
up the container machine: the fail-closed config resolver (G1), the
narrow-only admission gate (G2), the container limit (G3), the admission
*order* and host preconditions (G4), the container-record schema (G5), the
read-only record REST surface (G6), drift-as-events with no silent kills (G7),
the lifecycle -> restart-policy table (G8) and one *real* hardened-container
test (G9).

Only two fixtures are introduced here:

* ``smoke_env`` -- a hermetic vault rooted under ``tmp_path`` whose root is
  exported via ``THOUGHTMACHINE_VAULT_ROOT`` (mirrors the ``vault`` fixtures
  used by the focused container-record suites);
* ``require_docker`` -- self-skips when the docker daemon/package is
  unavailable.  It is requested **only** by G9 (never ``autouse``), so the
  other eight tests always run and G9 self-skips exactly once.

No production module is imported for side effects; nothing here (besides G9)
touches Docker.

Run:  python -m pytest tests/test_container_machine_smoke.py -q
"""

from __future__ import annotations

import pathlib
import random

import pytest

from agent.config.defaults import host_user
from security.admission_gate import (
    AdmissionRequest,
    Allow,
    ContainerSpec,
    Deny,
    REASON_CAPABILITIES_REQUIRED,
    REASON_CODES,
    REASON_CONTAINER_LIMIT_EXCEEDED,
    REASON_DAEMON_UNREACHABLE,
    REASON_IMAGE_NOT_ALLOWED,
    REASON_INSUFFICIENT_DISK,
    REASON_TRANSFORM_WIDEN_FORBIDDEN,
    REASON_UNKNOWN_CONTAINER_TYPE,
    REASON_WORKSPACE_ID_REQUIRED,
    Transform,
    admit,
)
from security.security_gate import (
    ContainerConfig,
    ContainerConfigError,
    resolve_container_config,
)
from thoughtmachine.container_record import (
    POLICY_BY_CLASS,
    UnknownLifecycleClass,
    api,
    drift,
    policy_for,
    storage,
)
from thoughtmachine.container_record.models import (
    LIFECYCLE_CLASSES,
    SCHEMA_FIELD_NAMES,
    SCHEMA_VERSION_CURRENT,
)
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

GiB = 1024 ** 3
WS = "ws-smoke"

# G9's hardened image is LOCAL-ONLY (infra.container_registry.DEFAULT_IMAGE).
# docker-py auto-PULLS on ImageNotFound, so G9 must probe for it explicitly.
HARDENED_IMAGE = "agent-executor"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def smoke_env(tmp_path, monkeypatch):
    """A hermetic vault root under ``tmp_path`` with a ``workspaces`` dir."""
    root = tmp_path / "vault"
    (root / "workspaces").mkdir(parents=True)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


@pytest.fixture
def require_docker():
    """Skip (never fail) unless a usable daemon + local image are available.

    Requested ONLY by G9 -- it must not be ``autouse`` so that the other eight
    tests run unconditionally and G9 is the single self-skipping test.
    """
    try:
        import docker
    except Exception as exc:  # pragma: no cover - host dependent
        pytest.skip(f"docker package unavailable: {exc}")
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:  # pragma: no cover - host dependent
        pytest.skip(f"docker daemon unavailable: {exc}")
    # The image is LOCAL-ONLY and docker-py auto-PULLS on ImageNotFound;
    # images.get never pulls, so probe it and skip rather than pull implicitly.
    try:
        client.images.get(HARDENED_IMAGE)
    except docker.errors.ImageNotFound:
        pytest.skip(f"image {HARDENED_IMAGE!r} not present locally")
    except Exception as exc:  # pragma: no cover - host dependent
        pytest.skip(f"image {HARDENED_IMAGE!r} unavailable: {exc}")


# ── Test doubles / builders ─────────────────────────────────────────────────


class FakeProbes:
    """Deterministic admission-probe double (disk / daemon / count)."""

    def __init__(
        self,
        *,
        total=100 * GiB,
        used=10 * GiB,
        free=90 * GiB,
        docker_ok=True,
        count=0,
    ):
        self._total = total
        self._used = used
        self._free = free
        self._docker_ok = docker_ok
        self._count = count

    def disk_usage(self, path):
        return (self._total, self._used, self._free)

    def docker_reachable(self):
        return self._docker_ok

    def workspace_container_count(self, workspace_id):
        return self._count

    def now(self):
        return 0.0


PERMISSIVE = WorkspaceCapabilities.default()
NO_NETWORK = WorkspaceCapabilities(allow_network=False)
WRITE_PERMS = {"network": "write", "filesystem": "write", "container": True}


def _spec(**overrides):
    base = dict(
        container_type="user",
        lifecycle_class="persistent",
        workspace_id=WS,
        image=None,
        network_mode="none",
    )
    base.update(overrides)
    return ContainerSpec(**base)


def _request(spec=None, permissions=WRITE_PERMS, capabilities=PERMISSIVE):
    return AdmissionRequest(
        spec=_spec() if spec is None else spec,
        permissions=permissions,
        capabilities=capabilities,
    )


def _snapshot(root):
    """Map every regular file under *root* to its ``(mtime_ns, size)``."""
    snap = {}
    for path in sorted(pathlib.Path(root).rglob("*")):
        if path.is_file():
            st = path.stat()
            snap[str(path.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    return snap


# ── G1: resolve_container_config is fail-closed + pure + total ───────────────


def test_g1_resolve_container_config_fail_closed():
    caps = WorkspaceCapabilities.default()
    perms = dict(WRITE_PERMS)

    # capabilities=None fails CLOSED to the "capabilities_required" error value.
    err = resolve_container_config(perms, None, "persistent")
    assert isinstance(err, ContainerConfigError)
    assert err.code == "capabilities_required"

    # A valid request yields a resolved ContainerConfig.
    cfg = resolve_container_config(perms, caps, "persistent")
    assert isinstance(cfg, ContainerConfig)
    assert cfg.lifecycle_class == "persistent"
    assert cfg.network_mode == "bridge"  # network "write" -> bridge
    assert cfg.workspace_mode == "rw"  # filesystem "write" -> rw

    # Purity: repeat calls are equal and the inputs are never mutated.
    perms_before = dict(perms)
    assert resolve_container_config(perms, caps, "persistent") == cfg
    assert perms == perms_before

    # Totality: never raises -- always a value (config or error), over junk.
    rng = random.Random(20240607)
    junk = [None, 0, 1, -1, "x", [], {}, (), object(), 3.14, True, False]
    for _ in range(200):
        result = resolve_container_config(
            rng.choice(junk), rng.choice(junk), rng.choice(junk)
        )
        assert isinstance(result, (ContainerConfig, ContainerConfigError))


# ── G2: admit is narrow-only, vocabulary closed ─────────────────────────────


def test_g2_admit_is_narrow_only():
    probes = FakeProbes()

    # Every verdict is a member of the closed decision algebra; a Deny's code
    # is always part of the closed REASON_CODES vocabulary.
    for spec in (
        _spec(network_mode="bridge"),
        _spec(container_type="not-a-type"),
        _spec(workspace_id=""),
        _spec(image="evil"),
        _spec(lifecycle_class="bogus"),
    ):
        decision = admit(_request(spec=spec), probes=probes)
        assert isinstance(decision, (Allow, Deny, Transform))
        if isinstance(decision, Deny):
            assert decision.code in REASON_CODES

    # Fuzz: never raises, verdict always in the algebra, Deny codes closed.
    rng = random.Random(99)
    junk = [None, 0, "x", [], {}, object()]
    for _ in range(150):
        request = AdmissionRequest(
            spec=ContainerSpec(
                container_type=rng.choice(junk + ["user"]),
                lifecycle_class=rng.choice(junk + ["persistent"]),
                workspace_id=rng.choice(junk + [WS]),
            ),
            permissions=rng.choice(junk),
            capabilities=rng.choice(junk + [PERMISSIVE]),
        )
        decision = admit(request, probes=probes)
        assert isinstance(decision, (Allow, Deny, Transform))
        if isinstance(decision, Deny):
            assert decision.code in REASON_CODES

    # WIDENING the derived network (proposed "none" vs policy "bridge") is
    # forbidden -- it is a Deny, never a silent widening Transform.
    widen = admit(_request(spec=_spec(network_mode="none")), probes=FakeProbes())
    assert isinstance(widen, Deny)
    assert widen.code == REASON_TRANSFORM_WIDEN_FORBIDDEN

    # NARROWING (proposed "bridge" vs policy "none") transforms down, never up.
    narrow = admit(
        _request(spec=_spec(network_mode="bridge"), capabilities=NO_NETWORK),
        probes=FakeProbes(),
    )
    assert isinstance(narrow, Transform)
    assert narrow.spec.network_mode == "none"


# ── G3: container limit is 6 (count==6 denies, <6 allows, None denies) ───────


def test_g3_container_limit_is_six():
    allow_spec = _spec(network_mode="bridge")

    at_limit = admit(_request(spec=allow_spec), probes=FakeProbes(count=6))
    assert isinstance(at_limit, Deny)
    assert at_limit.code == REASON_CONTAINER_LIMIT_EXCEEDED

    under_limit = admit(_request(spec=allow_spec), probes=FakeProbes(count=5))
    assert isinstance(under_limit, Allow)

    unknown = admit(_request(spec=allow_spec), probes=FakeProbes(count=None))
    assert isinstance(unknown, Deny)
    assert unknown.code == REASON_CONTAINER_LIMIT_EXCEEDED


# ── G4: admission order + host preconditions ────────────────────────────────


def test_g4_admission_order_and_host_preconditions():
    bridge = _spec(network_mode="bridge")

    # Disk insufficiency denies BEFORE a container is created.
    disk = admit(
        _request(spec=bridge), probes=FakeProbes(total=100 * GiB, used=99 * GiB, free=1 * GiB)
    )
    assert isinstance(disk, Deny)
    assert disk.code == REASON_INSUFFICIENT_DISK

    # An unreachable daemon denies.
    daemon = admit(_request(spec=bridge), probes=FakeProbes(docker_ok=False))
    assert isinstance(daemon, Deny)
    assert daemon.code == REASON_DAEMON_UNREACHABLE

    # Both bad -> disk wins (it precedes the daemon check).
    both = admit(
        _request(spec=bridge),
        probes=FakeProbes(total=100 * GiB, used=99 * GiB, free=1 * GiB, docker_ok=False),
    )
    assert isinstance(both, Deny)
    assert both.code == REASON_INSUFFICIENT_DISK

    # Bad container_type + bad disk -> unknown_container_type wins (first).
    ct = admit(
        _request(spec=_spec(container_type="bogus", network_mode="bridge")),
        probes=FakeProbes(total=100 * GiB, used=99 * GiB, free=1 * GiB),
    )
    assert isinstance(ct, Deny)
    assert ct.code == REASON_UNKNOWN_CONTAINER_TYPE

    # Full precedence: each earlier precondition masks every later one.
    # container_type > workspace_id > image > disk > daemon > config > limit
    precedence = [
        (
            _spec(container_type="bogus", workspace_id="", image="evil"),
            WRITE_PERMS,
            PERMISSIVE,
            FakeProbes(total=100 * GiB, used=99 * GiB, free=1 * GiB, docker_ok=False, count=6),
            REASON_UNKNOWN_CONTAINER_TYPE,
        ),
        (
            _spec(workspace_id="", image="evil"),
            WRITE_PERMS,
            PERMISSIVE,
            FakeProbes(total=100 * GiB, used=99 * GiB, free=1 * GiB, docker_ok=False, count=6),
            REASON_WORKSPACE_ID_REQUIRED,
        ),
        (
            _spec(image="evil", network_mode="bridge"),
            WRITE_PERMS,
            PERMISSIVE,
            FakeProbes(total=100 * GiB, used=99 * GiB, free=1 * GiB, docker_ok=False, count=6),
            REASON_IMAGE_NOT_ALLOWED,
        ),
        (
            bridge,
            WRITE_PERMS,
            PERMISSIVE,
            FakeProbes(total=100 * GiB, used=99 * GiB, free=1 * GiB, docker_ok=False, count=6),
            REASON_INSUFFICIENT_DISK,
        ),
        (
            bridge,
            WRITE_PERMS,
            PERMISSIVE,
            FakeProbes(docker_ok=False, count=6),
            REASON_DAEMON_UNREACHABLE,
        ),
        (
            bridge,
            WRITE_PERMS,
            None,  # config resolution fails
            FakeProbes(count=6),
            REASON_CAPABILITIES_REQUIRED,
        ),
        (
            bridge,
            WRITE_PERMS,
            PERMISSIVE,
            FakeProbes(count=6),
            REASON_CONTAINER_LIMIT_EXCEEDED,
        ),
    ]
    for spec, perms, caps, probes, expected in precedence:
        decision = admit(
            AdmissionRequest(spec=spec, permissions=perms, capabilities=caps),
            probes=probes,
        )
        assert isinstance(decision, Deny), (spec, expected, decision)
        assert decision.code == expected, (spec, expected, decision.code)


# ── G5: record schema + one JSON file per container ─────────────────────────


def test_g5_record_schema_and_one_file_per_container(smoke_env):
    rec_a = api.create_record(
        WS, "ephemeral", "workspace-owned", id="rec-a", vault_root=smoke_env
    )
    rec_b = api.create_record(
        WS, "persistent", "workspace-owned", id="rec-b", vault_root=smoke_env
    )

    for rec in (rec_a, rec_b):
        data = rec.to_dict()
        assert tuple(data.keys()) == tuple(SCHEMA_FIELD_NAMES)
        assert len(data) == 18
        assert data["schema_version"] == SCHEMA_VERSION_CURRENT
        assert "inferred" in data
        assert data["inferred"] is False

    # Exactly one ``<id>.json`` per container, and nothing else.
    containers = storage.containers_dir(WS, smoke_env)
    names = sorted(p.name for p in storage.iter_record_files(containers))
    assert names == ["rec-a.json", "rec-b.json"]


# ── G6: record REST surface is read-only ────────────────────────────────────


def test_g6_record_api_is_read_only(smoke_env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web_ui.backend import container_record_routes as routes

    # Every mounted route uses a safe, non-mutating HTTP method.
    mutating = {"POST", "PUT", "PATCH", "DELETE"}
    for route in routes.router.routes:
        methods = set(getattr(route, "methods", set()) or set())
        assert methods, route.path  # every route declares its method(s)
        assert not (methods & mutating), (route.path, methods)

    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: smoke_env)
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    api.create_record(WS, "ephemeral", "workspace-owned", id="rec-a", vault_root=smoke_env)
    api.append_event(WS, "rec-a", "attached", "tester", vault_root=smoke_env)

    before = _snapshot(smoke_env)

    assert client.get(
        "/api/container-records", params={"workspace_id": WS}
    ).status_code == 200
    assert client.get(
        "/api/container-records/rec-a", params={"workspace_id": WS}
    ).status_code == 200
    assert client.get(
        "/api/container-records/rec-a/events", params={"workspace_id": WS}
    ).status_code == 200

    # Writes are rejected (405 method-not-allowed / 404 no such route).
    assert client.post("/api/container-records").status_code in (404, 405)
    assert (
        client.post(
            "/api/container-records/rec-a", params={"workspace_id": WS}
        ).status_code
        in (404, 405)
    )

    # A battery of GETs leaves the on-disk store byte-for-byte untouched.
    assert _snapshot(smoke_env) == before


# ── G7: drift is expressed as events, never a silent kill ───────────────────


def test_g7_drift_events_no_silent_kills(smoke_env):
    api.create_record(WS, "ephemeral", "workspace-owned", id="rec-drift", vault_root=smoke_env)
    api.update_record(WS, "rec-drift", vault_root=smoke_env, docker_id="docker-abc")
    rec = api.load_record(WS, "rec-drift", smoke_env)

    class _EmptyContainers:
        def list(self, all=False):  # noqa: A002 - docker-py signature
            return []

    findings = drift.scan_record(
        rec,
        _EmptyContainers(),
        workspace_id=WS,
        permissions={},
        capabilities=WorkspaceCapabilities(),
        vault_root=smoke_env,
    )
    assert any(f.event_type == drift.EVENT_CONTAINER_ABSENT for f in findings)

    log = api.read_event_log(WS, "rec-drift", vault_root=smoke_env)
    assert len(log) == 1
    assert log[0]["event_type"] == drift.EVENT_CONTAINER_ABSENT
    assert log[0]["actor"] == drift.ACTOR

    # A re-scan of the unchanged state dedups: no second event is appended.
    again = drift.scan_record(
        rec,
        _EmptyContainers(),
        workspace_id=WS,
        permissions={},
        capabilities=WorkspaceCapabilities(),
        vault_root=smoke_env,
    )
    assert again == []
    assert len(api.read_event_log(WS, "rec-drift", vault_root=smoke_env)) == 1

    # The drift module never issues a destructive Docker call.
    source = pathlib.Path(drift.__file__).read_text(encoding="utf-8")
    for forbidden in (".stop(", ".restart(", ".remove("):
        assert forbidden not in source


# ── G8: lifecycle -> restart policy + append-only event log ─────────────────


def test_g8_lifecycle_restart_policy_and_append_only_log(smoke_env):
    # The policy table is total over the four lifecycle classes.
    assert set(POLICY_BY_CLASS) == set(LIFECYCLE_CLASSES)
    assert len(LIFECYCLE_CLASSES) == 4

    assert policy_for("ephemeral").restart_policy == "no"

    persistent = policy_for("persistent")
    assert persistent.restart_policy == "unless-stopped"
    assert persistent.workspace_gc_max_age_s == 86400

    resource = policy_for("resource")
    assert resource.restart_policy == "unless-stopped"
    assert resource.workspace_gc_max_age_s is None
    assert resource.own_lifecycle is True  # never swept

    service = policy_for("service")
    assert service.own_lifecycle is True
    assert service.workspace_gc_max_age_s is None

    # An unknown class fails closed.
    with pytest.raises(UnknownLifecycleClass):
        policy_for("nope")

    # The event log is append-only: it grows and preserves earlier entries.
    api.create_record(WS, "persistent", "workspace-owned", id="rec-log", vault_root=smoke_env)
    for n in range(3):
        api.append_event(WS, "rec-log", "tick", "actor", vault_root=smoke_env, n=n)
    log = api.read_event_log(WS, "rec-log", vault_root=smoke_env)
    assert len(log) == 3
    assert [entry["payload"]["n"] for entry in log] == [0, 1, 2]


# ── G9: a REAL hardened container (self-skips without Docker) ───────────────


def test_g9_real_container_hardening(require_docker, smoke_env):
    import docker

    from infra.container_registry import (
        HARDENED_CAP_DROP,
        HARDENED_READ_ONLY,
        HARDENED_SECURITY_OPT,
        ContainerProfile,
        create_hardened_container,
    )

    # A hardened container cannot run as host root; skip rather than fail.
    host = host_user()
    if host is None or host.split(":")[0] == "0":
        pytest.skip("hardened containers cannot run as host root")

    client = docker.from_env()
    name = "tm-smoke-hardening"
    profile = ContainerProfile(command=["sleep", "120"])

    container = None
    try:
        container = create_hardened_container(
            client,
            profile,
            name,
            workspace_id=WS,
            lifecycle_class="ephemeral",
            # network must be 'banned'|'ask'|'write'|'outbound'
            # (thoughtmachine/security.py:132); "banned" is the narrowest valid.
            permissions={"network": "banned", "filesystem": "read", "container": True},
            capabilities=WorkspaceCapabilities.default(),
        )
        container.reload()
        attrs = container.attrs
        host_cfg = attrs["HostConfig"]

        # Capabilities are dropped to the hardened baseline.
        assert set(host_cfg.get("CapDrop") or []) == set(HARDENED_CAP_DROP)
        # The root filesystem is mounted read-only.
        assert host_cfg.get("ReadonlyRootfs") is HARDENED_READ_ONLY
        # The hardened security options are applied.
        for opt in HARDENED_SECURITY_OPT:
            assert opt in (host_cfg.get("SecurityOpt") or [])
        # It never runs as root.
        user = attrs["Config"].get("User") or ""
        # POSIX-only assertion. On Windows host_user() is None, so the
        # hardened container falls back to "0:0" — a value forbidden here.
        # The G9 skip above (host is None) keeps that path unreachable, so
        # this check is deliberately never reached, and never weakened, on Windows.
        assert user not in ("", "0", "0:0", "root")
        assert user == host
    finally:
        if container is not None:
            container.remove(force=True)
