"""Loopback-only caller gate, shared by the web-UI route modules.

Two endpoints in this package serve operator state and must never be reachable
from another host:

* ``GET /health`` / ``GET /api/health`` (``web_ui/backend/server.py``), and
* ``GET /api/system/health`` (``web_ui/backend/health_routes.py``), which exposes
  the running-worker registry and the open sessions.

The server binds ``127.0.0.1`` by default, but the bind can be overridden
(``--host`` / ``HOST``) and a reverse proxy in front of the process would
forward a remote caller as a local one, so each route checks the peer address
itself.

This module exists because the gate used to be copied: one copy in
``server.py`` and one in ``health_routes.py``, identical by copy rather than by
construction.  Two copies of a security check stay consistent only until
somebody edits one of them, and the weaker of the two would be the one guarding
the vault-global payload.  A third module avoids the reason the copy was made —
``server.py`` imports ``health_routes``, so importing back into it is circular —
while giving both sides one definition.

Whichever module applies the gate must import these definitions rather than
restating the frozensets; ``web_ui/backend/tests/test_local_gate_shared.py``
fails when a copy is reintroduced or when the copies drift apart.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

#: Peer addresses that are this machine.  One notion of "local", shared by the
#: health gate and by the bind-host check in ``server.py``.
LOCAL_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: In-process ASGI test-harness address.  Starlette's ``TestClient`` puts
#: ``scope["client"] = ("testclient", 50000)`` on every request it builds
#: (``starlette/testclient.py``); the name is neither resolvable nor routable
#: over any network, so real traffic can never carry it.  Kept in its own
#: constant so the allowance is explicit and cannot be confused with the
#: loopback set.
TEST_HARNESS_CLIENT_HOSTS = frozenset({"testclient"})


def localhost_only(request: Request) -> Optional[JSONResponse]:
    """Return a 403 response when *request* did not come from a local caller.

    ``request.client`` is ``None`` when the ASGI server cannot report a peer
    address; that is treated as NOT local (fail closed).  The caller's address
    is deliberately never echoed in the response body.
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    if host is not None and (
        host in LOCAL_CLIENT_HOSTS or host in TEST_HARNESS_CLIENT_HOSTS
    ):
        return None
    return JSONResponse({"error": "localhost only"}, status_code=403)
