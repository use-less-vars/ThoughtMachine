"""Regression test: start() REFUSES a non-conforming container and recreates it.

Scenario
--------
A container with the SAME name already exists with WEAK docker hardening (no
``cap_drop=ALL``, no ``no-new-privileges``, writable rootfs) and no other
binding container record.  ``ContainerManager.start()`` finds it via the label
path and runs the drift admission
(``ContainerManager._start_drift_decision``).  In addition to the network mode,
the ``/workspace`` mount mode, the container ``user`` and the restart policy,
that decision now inspects the docker HARDENING axes (``cap_drop`` /
``security_opt`` / ``read_only`` rootfs) via
``infra/container_create.py::_expected_hardening_recipe()`` and
``infra/container_create.py::_hardening_conformance()``.  A container whose
network and ``/workspace`` mount match the resolved policy (and that carries a
blank ``user``, which the user axis ignores) is still flagged as non-conforming
because it is far weaker on the hardening axes than a fresh create would be.

Refuse-and-recreate (the contract this test pins)
-------------------------------------------------
When an existing container is non-conforming on the hardening axes, ``start()``
REFUSES to reuse it and CREATES a new, fully hardened container - it never
silently hands back the weak one.  ``_start_drift_decision`` signals this with
``("recreate", drift)``; ``start()`` then removes the weak container
(``container.remove(force=True)``) and calls ``self._fresh_start(...)`` for the
SAME name, so the weak container cannot survive the recreate.

This test therefore:

1. creates "config A" - a deliberately WEAK container whose ``network=none`` and
   read-only ``/workspace`` bind mount match the resolved policy but that omits
   the hardening axes;
2. calls ``start(name=...)`` ("config B" - the full hardened isolation a FRESH
   create would produce under the same default policy: network none, workspace
   ro, ``cap_drop=ALL``, ``no-new-privileges:true``, read-only rootfs and the
   host ``user``);
3. FIRST builds an ``attribute | expected | actual`` table of the RETURNED
   container's live docker config and asserts it is fully conformant - so if
   refuse-and-recreate regresses (the weak container is handed back) the test
   fails with the full table;
4. then (structural guard) asserts the returned id is NOT the weak container's
   id, and (secondary) that the fresh create reported ``status == "created"``.

Post-fix contract
-----------------
The assertions PASS on fixed code: ``start()`` refuses the weak container and
replaces it with a freshly created, fully hardened one under the SAME name.
They FAIL if refuse-and-recreate regresses (the weak container is reused), so
the test protects the hardening-admission behaviour rather than documenting its
absence.

Self-skips cleanly when no Docker daemon is available (class-level
``@needs_docker`` skipif gate, mirroring tests/docker/test_persistence.py).
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

try:  # pragma: no cover - import guard mirrors the sibling docker tests
    import docker
    from docker.types import Mount
except Exception:  # pragma: no cover
    docker = None
    Mount = None

_DOCKER_AVAILABLE = False


def docker_available() -> bool:
    global _DOCKER_AVAILABLE
    if _DOCKER_AVAILABLE:
        return True
    if docker is None:
        return False
    try:
        client = docker.from_env()
        client.ping()
        _DOCKER_AVAILABLE = True
        return True
    except Exception:
        return False


needs_docker = pytest.mark.skipif(
    not docker_available(), reason="Docker daemon is not available"
)


IMAGE = os.environ.get("TM_REUSE_TEST_IMAGE", "alpine:3.19")
WS_ID = "ws-reuse-isolation-test"
NAME = "reuse-iso-test"


@needs_docker
class TestReuseRefusesNonConforming:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        # The default CREATE path refuses to run for a host root user; mirror
        # that guard so the test skips instead of dying for the wrong reason.
        if hasattr(os, "getuid") and os.getuid() == 0:
            pytest.skip("host root (uid 0): container create is refused by policy")

        from agent.config.defaults import (
            CONTAINER_NAME_LABEL,
            CONTAINER_TYPE_FREE_USE,
            CONTAINER_TYPE_LABEL,
        )
        from infra.container_manager import ContainerManager

        self.client = docker.from_env()
        try:
            self.client.images.get(IMAGE)
        except Exception:
            self.client.images.pull(IMAGE)

        self.ws = str(tmp_path / "ws")
        os.makedirs(self.ws, exist_ok=True)
        self.vault = str(tmp_path / "vault")
        os.makedirs(self.vault, exist_ok=True)

        self.labels = {
            CONTAINER_NAME_LABEL: NAME,
            "thoughtmachine.workspace_id": WS_ID,
            CONTAINER_TYPE_LABEL: CONTAINER_TYPE_FREE_USE,
        }
        self.manager = ContainerManager(
            workspace_path=self.ws,
            session_id="sess-reuse-iso",
            workspace_id=WS_ID,
            session_permissions={},
            image=IMAGE,
            vault_root=self.vault,
            session_config={},
        )
        self.weak = None
        yield
        try:
            if self.weak is not None:
                self.weak.remove(force=True)
        except Exception:
            pass

    def _create_weak_container(self):
        """Config A: weak hardening; network + /workspace match, hardening axes omitted."""
        return self.client.containers.run(
            image=IMAGE,
            name=NAME,
            labels=self.labels,
            command="sleep 3600",
            detach=True,
            network="none",
            mounts=[
                Mount(
                    target="/workspace",
                    source=self.ws,
                    type="bind",
                    read_only=True,
                )
            ],
            # Deliberately NO cap_drop / security_opt / read_only:
            # this is the weak "config A" isolation.
        )

    def _isolation_rows(self, attrs, expected_user):
        """Build the ``attribute | expected | actual`` rows for "config B".

        Each row is ``(attribute, expected, actual, ok)``.  Actual values are
        read from the live docker attrs of the RETURNED container, using the
        correct section per attribute (``Config`` vs ``HostConfig``).  The four
        expected values are defined by
        ``infra/container_create.py::_expected_hardening_recipe()`` and checked
        against the live container by
        ``infra/container_create.py::_hardening_conformance()``:

        * ``HostConfig.CapDrop``          -> must contain ``"ALL"``
        * ``HostConfig.SecurityOpt``      -> must contain ``"no-new-privileges:true"``
        * ``HostConfig.ReadonlyRootfs``   -> must be ``True``
        * ``Config.User``                 -> the host user (``host_user() or "0:0"``)
        """
        host = attrs.get("HostConfig") or {}
        cfg = attrs.get("Config") or {}
        cap_drop = host.get("CapDrop")
        security_opt = host.get("SecurityOpt")
        return [
            (
                "HostConfig.CapDrop",
                "contains 'ALL'",
                repr(cap_drop),
                "ALL" in (cap_drop or []),
            ),
            (
                "HostConfig.SecurityOpt",
                "contains 'no-new-privileges:true'",
                repr(security_opt),
                "no-new-privileges:true" in (security_opt or []),
            ),
            (
                "HostConfig.ReadonlyRootfs",
                repr(True),
                repr(host.get("ReadonlyRootfs")),
                host.get("ReadonlyRootfs") is True,
            ),
            (
                "Config.User",
                repr(expected_user),
                repr(cfg.get("User")),
                cfg.get("User") == expected_user,
            ),
        ]

    @staticmethod
    def _render_table(rows):
        """Render ``(attribute, expected, actual, ok)`` rows as one table.

        Rows that do NOT match are prefixed ``! `` so the offending rows are
        obvious at a glance.
        """
        header = ("attribute", "expected", "actual")
        w0 = max([len(header[0])] + [len(str(r[0])) for r in rows])
        w1 = max([len(header[1])] + [len(str(r[1])) for r in rows])
        w2 = max([len(header[2])] + [len(str(r[2])) for r in rows])
        lines = [
            f"{header[0]:<{w0}} | {header[1]:<{w1}} | {header[2]}",
            f"{'-' * w0}-+-{'-' * w1}-+-{'-' * w2}",
        ]
        for name, expected, actual, ok in rows:
            flag = "  " if ok else "! "
            lines.append(f"{flag}{name:<{w0}} | {expected:<{w1}} | {actual}")
        return "\n".join(lines)

    def test_reuse_refuses_nonconforming(self):
        from agent.config.defaults import host_user

        # The hardened user a fresh create applies - DERIVED from source, never
        # assumed (infra/container_manager.py:1474 -> ``user=(host_user() or
        # "0:0")``).
        expected_user = host_user() or "0:0"

        self.weak = self._create_weak_container()
        weak_id = self.weak.id

        # "Config B": the default resolution is network=none / workspace=ro, so
        # the drift admission passes; the correct implementation MUST refuse the
        # weak container and CREATE a new hardened one instead of reusing it.
        res = self.manager.start(name=NAME)

        # ---- comparison table FIRST (before any guard that also fails now) ----
        # Read the RETURNED container's live docker config and fail on the
        # table, so an unfixed (reuse) implementation fails HERE with the full
        # table rather than with a bare guard message further down.
        returned_id = res.get("id")
        returned = (
            self.client.containers.get(returned_id) if returned_id else None
        )
        rows = self._isolation_rows(
            returned.attrs if returned is not None else {}, expected_user
        )
        mismatches = [row for row in rows if not row[3]]
        assert not mismatches, (
            "start() did not apply the hardened isolation to the returned "
            "container; it must REFUSE the weak container and recreate it:\n"
            + self._render_table(rows)
        )

        # ---- structural guards (run only once the table above passes) ----
        assert "error" not in res, res

        # Load-bearing: a fresh create yields a DIFFERENT container - handing
        # back the weak one is exactly the regression this test pins.
        assert res.get("id") != weak_id, (
            f"start() returned the EXISTING (weak) container id {weak_id!r} "
            "instead of refusing it and creating a new hardened container; "
            f"response={res!r}"
        )

        # Secondary: a fresh create reports status "created"
        # (infra/container_manager.py:1743 -> status "created" for the legacy
        # new-record create).
        assert res.get("status") == "created", (
            "a fresh create must report status 'created' "
            f"(infra/container_manager.py:1743); got {res.get('status')!r} in "
            f"{res!r}"
        )
