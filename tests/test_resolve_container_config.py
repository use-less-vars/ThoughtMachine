"""Contract + leaf-table tests for ``security.security_gate.resolve_container_config``.

``resolve_container_config`` is the single source of truth (SSOT) for deriving a
container's ``(network_mode, workspace_mode)`` pair from session permissions and
workspace capabilities.  It is documented as **pure**, **total** and
**fail-closed**:

* pure   -- no filesystem/vault/env/network IO; a call never mutates its inputs;
* total  -- it never raises; every input maps to a ``ContainerConfig`` or a
             ``ContainerConfigError``;
* fail-closed -- ambiguity (unknown lifecycle, missing capabilities, malformed
             permissions) yields a ``ContainerConfigError`` rather than a
             permissive default.

These tests pin that contract plus the leaf mapping tables.

Run:  pytest tests/test_resolve_container_config.py -q
"""

import dataclasses

import pytest

from security.security_gate import (
    ContainerConfig,
    ContainerConfigError,
    LIFECYCLE_CLASSES,
    WorkspaceCapabilities,
    resolve_container_config,
    resolve_network_mode,
)

# ``WorkspaceCapabilities()`` is fully permissive by default (allow_network,
# allow_docker, filesystem_write, git_available all True).
PERMISSIVE = WorkspaceCapabilities.default()
# The fail-closed ceiling: nothing allowed.
RESTRICTIVE = WorkspaceCapabilities(
    allow_network=False,
    allow_docker=False,
    filesystem_write=False,
    git_available=False,
)


# ---------------------------------------------------------------------------
# Purity / totality / fail-closed contract
# ---------------------------------------------------------------------------


def test_pure_identical_inputs_produce_equal_outputs():
    a = resolve_container_config(
        {"network": "outbound", "filesystem": "write"}, PERMISSIVE, "persistent"
    )
    b = resolve_container_config(
        {"network": "outbound", "filesystem": "write"}, PERMISSIVE, "persistent"
    )
    assert isinstance(a, ContainerConfig) and isinstance(b, ContainerConfig)
    assert (a.network_mode, a.workspace_mode, a.effective, a.lifecycle_class) == (
        b.network_mode,
        b.workspace_mode,
        b.effective,
        b.lifecycle_class,
    )
    assert a.network_mode == "bridge"
    assert a.workspace_mode == "rw"


def test_pure_does_not_mutate_input_permissions():
    perms = {"network": "write", "filesystem": "write"}
    snapshot = dict(perms)
    resolve_container_config(perms, PERMISSIVE, "persistent")
    assert perms == snapshot


def test_pure_performs_no_io(monkeypatch):
    """Resolving a config touches no filesystem, vault or environment state.

    Every IO entry point (``open``, ``io.open``, ``os.getenv``,
    ``pathlib.Path.open`` and ``os.environ`` item access) is booby-trapped to
    raise ; a correct resolver therefore must not trip any of them.
    """
    import builtins
    import io
    import os
    import pathlib

    def _boom(*args, **kwargs):
        raise AssertionError(
            "resolve_container_config performed IO via %r / %r" % (args, kwargs)
        )

    monkeypatch.setattr(builtins, "open", _boom)
    monkeypatch.setattr(io, "open", _boom)
    monkeypatch.setattr(os, "getenv", _boom)
    monkeypatch.setattr(pathlib.Path, "open", _boom, raising=False)
    # Block env reads without swapping the ``os.environ`` object itself (a swap
    # breaks pytest's own bookkeeping); patch the mapping's *read* hooks instead.
    environ_type = type(os.environ)
    monkeypatch.setattr(environ_type, "__getitem__", _boom)
    monkeypatch.setattr(environ_type, "get", _boom)

    # Only the resolver call runs under the guard; monkeypatch restores the real
    # hooks on teardown.
    cfg = resolve_container_config(
        {"network": "outbound", "filesystem": "write"}, PERMISSIVE, "persistent"
    )
    assert isinstance(cfg, ContainerConfig)
    assert cfg.network_mode == "bridge"
    assert cfg.workspace_mode == "rw"


@pytest.mark.parametrize(
    "perms,caps,lifecycle",
    [
        ({"network": "write"}, PERMISSIVE, "persistent"),
        ({}, PERMISSIVE, "persistent"),
        ({"unknown_field": 1}, PERMISSIVE, "persistent"),
        (None, PERMISSIVE, "persistent"),
        ("x", PERMISSIVE, "persistent"),
        ({"network": "write"}, None, "persistent"),
        ({"network": "write"}, RESTRICTIVE, "persistent"),
        ({"network": "write"}, PERMISSIVE, "bogus"),
        ({"network": "write"}, PERMISSIVE, None),
    ],
)
def test_total_never_raises(perms, caps, lifecycle):
    result = resolve_container_config(perms, caps, lifecycle)
    assert isinstance(result, (ContainerConfig, ContainerConfigError))


def test_fail_closed_missing_capabilities():
    err = resolve_container_config({"network": "write"}, None, "persistent")
    assert isinstance(err, ContainerConfigError)
    assert err.code == "capabilities_required"


def test_fail_closed_bad_permissions():
    for bad in (None, "x", 123):
        err = resolve_container_config(bad, PERMISSIVE, "persistent")
        assert isinstance(err, ContainerConfigError)
        assert err.code == "bad_permissions"


@pytest.mark.parametrize("lifecycle", ["bogus", "ephemeral-x", None, "", 123])
def test_fail_closed_unknown_lifecycle_class(lifecycle):
    err = resolve_container_config({"network": "write"}, PERMISSIVE, lifecycle)
    assert isinstance(err, ContainerConfigError)
    assert err.code == "unknown_lifecycle_class"


def test_lifecycle_class_does_not_change_resolved_axes():
    """The ``(network_mode, workspace_mode)`` pair is orthogonal to lifecycle.

    For a fixed permission/capability pair that resolves to a ``ContainerConfig``
    every known lifecycle class yields the *same* axes; only ``lifecycle_class``
    echoes the requested class.  (Unknown classes are the fail-closed counterpart,
    covered by :func:`test_fail_closed_unknown_lifecycle_class`.)
    """
    perms = {"network": "outbound", "filesystem": "write"}
    axes = set()
    for lifecycle in LIFECYCLE_CLASSES:
        cfg = resolve_container_config(perms, PERMISSIVE, lifecycle)
        assert isinstance(cfg, ContainerConfig)
        assert cfg.lifecycle_class == lifecycle
        axes.add((cfg.network_mode, cfg.workspace_mode))
    assert axes == {("bridge", "rw")}


def test_restrictive_capabilities_clamp_to_locked_down():
    cfg = resolve_container_config(
        {"network": "outbound", "filesystem": "write"}, RESTRICTIVE, "persistent"
    )
    assert isinstance(cfg, ContainerConfig)
    assert cfg.network_mode == "none"
    assert cfg.workspace_mode == "ro"


# ---------------------------------------------------------------------------
# Signature / shape guards (regression pins)
# ---------------------------------------------------------------------------


def test_requires_lifecycle_class_positional():
    with pytest.raises(TypeError):
        resolve_container_config({"network": "write"}, PERMISSIVE)


def test_workspace_id_kwarg_is_rejected():
    with pytest.raises(TypeError):
        resolve_container_config(
            {"network": "write"}, PERMISSIVE, "persistent", workspace_id="ws"
        )


def test_config_error_has_no_mapping_protocol():
    err = resolve_container_config({"network": "write"}, None, "persistent")
    assert isinstance(err, ContainerConfigError)
    # A plain frozen dataclass: no __getitem__ / __contains__ / get().
    with pytest.raises(TypeError):
        err["code"]
    with pytest.raises(TypeError):
        "code" in err
    assert not hasattr(err, "get")


def test_dataclasses_are_frozen():
    cfg = resolve_container_config({"network": "write"}, PERMISSIVE, "persistent")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.network_mode = "none"
    err = ContainerConfigError(code="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        err.code = "y"


# ---------------------------------------------------------------------------
# Leaf mapping tables
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level,expected",
    [
        ("write", "bridge"),
        ("outbound", "bridge"),
        ("OUTBOUND", "bridge"),
        ("Write", "bridge"),
        (True, "bridge"),
        (False, "none"),
        ("read", "none"),
        ("banned", "none"),
        ("ask", "none"),
        (None, "none"),
        ("", "none"),
    ],
)
def test_resolve_network_mode_leaf(level, expected):
    assert resolve_network_mode(level) == expected


@pytest.mark.parametrize(
    "network,expected",
    [
        ("write", "bridge"),
        ("outbound", "bridge"),
        (True, "bridge"),
        (False, "none"),
        ("ask", "none"),
        ("banned", "none"),
        (None, "none"),
    ],
)
def test_network_mode_through_resolver(network, expected):
    perms = {} if network is None else {"network": network}
    cfg = resolve_container_config(perms, PERMISSIVE, "persistent")
    assert isinstance(cfg, ContainerConfig)
    assert cfg.network_mode == expected


@pytest.mark.parametrize(
    "filesystem,expected",
    [
        ("write", "rw"),
        ("full", "rw"),
        ("read", "ro"),
        ("banned", "ro"),
        (None, "ro"),
    ],
)
def test_workspace_mode_leaf(filesystem, expected):
    perms = {} if filesystem is None else {"filesystem": filesystem}
    cfg = resolve_container_config(perms, PERMISSIVE, "persistent")
    assert isinstance(cfg, ContainerConfig)
    assert cfg.workspace_mode == expected


def test_no_permissions_is_locked_down():
    cfg = resolve_container_config({}, PERMISSIVE, "persistent")
    assert isinstance(cfg, ContainerConfig)
    assert cfg.network_mode == "none"
    assert cfg.workspace_mode == "ro"
