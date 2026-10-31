"""R2 — the loopback gate is one definition, not two copies.

``/health`` (``server.py``) and ``/api/system/health`` (``health_routes.py``)
both serve operator state and both must refuse a non-loopback caller.  The gate
used to exist as two independently maintained copies with their own
``frozenset({"127.0.0.1", ...})`` literals; identical by copy, which means
identical only until somebody edits one of them.  The weaker of the two would be
the one guarding the vault-global payload, and nothing would report it.

The definition now lives in ``web_ui/backend/local_gate.py`` alone, and the peer
address is checked once — step 0 of
``web_ui/backend/auth_boundary.AuthBoundaryMiddleware``, which every route sits
behind.  This module pins that end state:

* ``server.py`` imports the shared constants, it does not restate them
  (identity, not equality — a re-declared frozenset would be a *different*
  object and fails here);
* ``local_gate.localhost_only`` is the only peer-address gate in the product:
  neither ``server.py`` nor ``health_routes.py`` declares a ``localhost_only``
  of its own, and neither carries the loopback/test-harness literals any more;
* the gate decides correctly on its own: loopback and harness peers allowed,
  remote and loopback-looking names denied, "no peer address at all" fails
  closed, and the refusal is one fixed body that never echoes the caller;
* the real routes actually run it: ``/health`` answers a loopback and a harness
  caller and refuses a remote one through the assembled app.
"""

from __future__ import annotations

import ast
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

#: The two literals the gate is allowed to own (in ``local_gate.py``).
DUPLICATED_LITERALS = (
    'frozenset({"127.0.0.1","::1","localhost"})',
    'frozenset({"testclient"})',
)

#: Names a per-route copy of the gate used to be declared under.
GATE_FUNCTION_NAMES = frozenset({"localhost_only", "_localhost_only"})


def _outcome(gate, host) -> tuple:
    """Normalise one gate decision to ``("allow", None)`` or ``("deny", status)``."""
    response = gate(_request(host))
    if response is None:
        return ("allow", None)
    return ("deny", response.status_code)


def _source(module) -> str:
    with open(module.__file__, encoding="utf-8") as handle:
        return handle.read()


def _declared_functions(text: str):
    """Names of every function ``def``-ed at module level of *text*."""
    tree = ast.parse(text)
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


# ════════════════════════════════════════════════════════════════════════════
#  One definition, imported
# ════════════════════════════════════════════════════════════════════════════


class TestServerImportsTheSharedDefinition:
    def test_server_binds_the_shared_objects(self):
        """server.py must alias the shared definitions, not restate them."""
        assert server._LOCAL_CLIENT_HOSTS is local_gate.LOCAL_CLIENT_HOSTS
        assert server._TEST_HARNESS_CLIENT_HOSTS is local_gate.TEST_HARNESS_CLIENT_HOSTS


# ════════════════════════════════════════════════════════════════════════════
#  No second copy may come back
# ════════════════════════════════════════════════════════════════════════════


class TestTheGateIsDefinedOnce:
    @pytest.mark.parametrize("module", [server, health_routes], ids=lambda m: m.__name__)
    def test_module_declares_no_gate_of_its_own(self, module):
        """A re-declared gate would be a second definition, drifting silently."""
        declared = _declared_functions(_source(module))
        assert declared & GATE_FUNCTION_NAMES == set()

    @pytest.mark.parametrize("module", [server, health_routes], ids=lambda m: m.__name__)
    def test_module_restates_no_host_literals(self, module):
        """The literal sets belong to ``local_gate.py`` and nowhere else."""
        compact = _source(module).replace(" ", "")
        for literal in DUPLICATED_LITERALS:
            assert literal not in compact


# ════════════════════════════════════════════════════════════════════════════
#  The shared gate's own decisions
# ════════════════════════════════════════════════════════════════════════════


class TestGateDecisions:
    @pytest.mark.parametrize(
        "host", sorted(local_gate.LOCAL_CLIENT_HOSTS | local_gate.TEST_HARNESS_CLIENT_HOSTS)
    )
    def test_loopback_and_harness_peers_are_allowed(self, host):
        assert _outcome(local_gate.localhost_only, host) == ("allow", None)

    @pytest.mark.parametrize("host", PEER_HOSTS)
    def test_only_the_defined_peers_are_local(self, host):
        expected = (
            ("allow", None)
            if host in local_gate.LOCAL_CLIENT_HOSTS
            or host in local_gate.TEST_HARNESS_CLIENT_HOSTS
            else ("deny", 403)
        )
        assert _outcome(local_gate.localhost_only, host) == expected

    def test_no_peer_address_fails_closed(self):
        """No peer address must never be read as local."""
        assert _outcome(local_gate.localhost_only, None) == ("deny", 403)

    def test_refusal_body_is_fixed_and_echoes_nothing(self):
        """One fixed body, and never the caller's address."""
        peer = "203.0.113.9"
        refused = local_gate.localhost_only(_request(peer))
        assert refused.status_code == 403
        assert json.loads(refused.body) == {"error": "localhost only"}
        assert peer not in refused.body.decode()


# ════════════════════════════════════════════════════════════════════════════
#  The routes actually run the gate
# ════════════════════════════════════════════════════════════════════════════


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
