"""R2 — the loopback gate is one definition, not two copies.

``/health`` (``server.py``) and ``/api/system/health`` (``health_routes.py``)
both serve operator state and both must refuse a non-loopback caller.  The gate
used to exist as two independently maintained copies with their own
``frozenset({"127.0.0.1", ...})`` literals; identical by copy, which means
identical only until somebody edits one of them.  The weaker of the two would be
the one guarding the vault-global payload, and nothing would report it.

This module pins the fix recorded as R2 in
``ticket-residual-w3-isolation-findings-not-fixed.md``:

* ``server.py`` imports the shared definitions, it does not restate them
  (identity, not equality — a re-declared frozenset would be a *different*
  object and fails here);
* the copy that is still inside ``health_routes.py`` agrees with the shared
  definitions on both constant sets;
* both gates behave identically when driven with the same injected client
  address, including the fail-closed no-peer case.

**Residual, recorded rather than hidden:** ``web_ui/backend/health_routes.py``
still holds its own copy because that file is frozen for this wave.  The copy is
no longer unobserved — a divergence in either constant set or in the decision
fails the tests below — but it is still a copy.  Unfreezing that module and
replacing its local definitions with imports of ``web_ui.backend.local_gate`` is
the remaining half.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from web_ui.backend import health_routes, local_gate, server


def _request(host):
    """A request stand-in carrying *host* as the peer address.

    ``host=None`` models an ASGI server that cannot report a peer address.
    """
    return SimpleNamespace(client=None if host is None else SimpleNamespace(host=host))


#: Peer addresses a caller can present; the fail-closed no-peer case is added by
#: the parametrised tests below.
PEER_HOSTS = [
    "127.0.0.1",
    "::1",
    "localhost",
    "testclient",
    "203.0.113.9",          # documentation-range remote address
    "10.1.2.3",             # private network
    "127.0.0.1.evil.com",   # loopback-looking name that is not loopback
    "",                     # empty host is not loopback
]


def _outcome(gate, host) -> tuple:
    """Normalise one gate decision to ``("allow", None)`` or ``("deny", status)``."""
    response = gate(_request(host))
    if response is None:
        return ("allow", None)
    return ("deny", response.status_code)


# ══════════════════════════════════════════════════════════════════════════
#  One definition
# ══════════════════════════════════════════════════════════════════════════


class TestSingleDefinition:
    def test_server_binds_the_shared_objects(self):
        """server.py must alias the shared definitions, not restate them."""
        assert server._LOCAL_CLIENT_HOSTS is local_gate.LOCAL_CLIENT_HOSTS
        assert server._TEST_HARNESS_CLIENT_HOSTS is local_gate.TEST_HARNESS_CLIENT_HOSTS
        assert server._localhost_only is local_gate.localhost_only

    def test_server_source_carries_no_local_copy(self):
        """The literal and the gate body must not come back into server.py."""
        with open(server.__file__, encoding="utf-8") as handle:
            text = handle.read()
        assert 'frozenset({"127.0.0.1", "::1", "localhost"})' not in text
        assert 'frozenset({"testclient"})' not in text
        assert "def _localhost_only(" not in text


class TestFrozenCopyAgrees:
    """health_routes.py is frozen, so its copy is pinned instead of deleted."""

    def test_local_client_hosts_agree(self):
        assert health_routes._LOCAL_CLIENT_HOSTS == local_gate.LOCAL_CLIENT_HOSTS

    def test_test_harness_hosts_agree(self):
        assert (
            health_routes._TEST_HARNESS_CLIENT_HOSTS
            == local_gate.TEST_HARNESS_CLIENT_HOSTS
        )


# ══════════════════════════════════════════════════════════════════════════
#  Identical behaviour under the same injected peer
# ══════════════════════════════════════════════════════════════════════════


class TestBehavioursAgree:
    @pytest.mark.parametrize("host", PEER_HOSTS + [None])
    def test_health_copy_matches_the_shared_gate(self, host):
        assert _outcome(health_routes._localhost_only, host) == _outcome(
            local_gate.localhost_only, host
        )

    @pytest.mark.parametrize("host", PEER_HOSTS + [None])
    def test_server_gate_matches_the_shared_gate(self, host):
        assert _outcome(server._localhost_only, host) == _outcome(
            local_gate.localhost_only, host
        )

    def test_no_peer_address_fails_closed(self):
        """No gate may read "no peer" as local."""
        for gate in (
            local_gate.localhost_only,
            health_routes._localhost_only,
            server._localhost_only,
        ):
            assert _outcome(gate, None)[0] == "deny"

    def test_refusal_body_is_identical_and_echoes_nothing(self):
        """Both gates answer the same fixed body and never the caller address."""
        peer = "203.0.113.9"
        shared = local_gate.localhost_only(_request(peer))
        frozen = health_routes._localhost_only(_request(peer))
        assert shared.status_code == frozen.status_code == 403
        assert shared.body == frozen.body
        assert json.loads(shared.body) == {"error": "localhost only"}
        assert peer not in shared.body.decode()

    def test_loopback_and_harness_peers_are_allowed(self):
        for host in sorted(
            local_gate.LOCAL_CLIENT_HOSTS | local_gate.TEST_HARNESS_CLIENT_HOSTS
        ):
            assert _outcome(local_gate.localhost_only, host) == ("allow", None)
            assert _outcome(health_routes._localhost_only, host) == ("allow", None)


# ══════════════════════════════════════════════════════════════════════════
#  The routes actually run the gate
# ══════════════════════════════════════════════════════════════════════════


class TestRoutesUseTheGate:
    @pytest.mark.parametrize("peer", ["127.0.0.1", "testclient"])
    def test_health_route_allows_local_callers(self, peer):
        response = TestClient(server.app, client=(peer, 50000)).get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_health_route_refuses_a_remote_caller(self):
        response = TestClient(server.app, client=("203.0.113.9", 45678)).get("/health")
        assert response.status_code == 403
        assert response.json() == {"error": "localhost only"}
