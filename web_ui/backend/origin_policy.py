"""origin_policy.py -- one definition of "trusted local web origin".

Three independent ingress guards on the web UI boundary need the same notion
of a local origin:

* the CORS middleware (the vite dev server runs on ``TM_FRONTEND_PORT``),
* the WebSocket handshake (``server.websocket_endpoint``),
* the REST origin guard (``vault_repair_routes``).

Keeping the loopback regex and the port resolution in ONE module means the
guards cannot drift apart.  Only loopback spellings (``localhost`` /
``127.0.0.1``) are trusted; the wildcard ``*`` is never trusted.
"""

from __future__ import annotations

import logging
import os
import re
from typing import List, Optional

from fastapi import Request

logger = logging.getLogger(__name__)

DEFAULT_FRONTEND_PORT = 5173
FRONTEND_PORT_ENV = "TM_FRONTEND_PORT"

_ORIGIN_RE = re.compile(r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$",
                        re.I)


def frontend_port() -> int:
    """Dev-server port the launcher binds (``TM_FRONTEND_PORT``, default 5173)."""
    raw = (os.environ.get(FRONTEND_PORT_ENV) or "").strip()
    if not raw:
        return DEFAULT_FRONTEND_PORT
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_FRONTEND_PORT
    return port if 0 < port < 65536 else DEFAULT_FRONTEND_PORT


def allowed_origins() -> List[str]:
    """Exact origins the CORS middleware answers for (never a wildcard)."""
    port = frontend_port()
    return [f"http://{host}:{port}" for host in ("localhost", "127.0.0.1")]


def origin_is_loopback(origin: str) -> bool:
    """True when *origin* is an ``http(s)://<loopback>[:port]`` origin."""
    return bool(origin) and bool(_ORIGIN_RE.match(origin.strip()))


def websocket_origin_allowed(origin: Optional[str]) -> bool:
    """WebSocket handshake policy.

    An absent ``Origin`` header is allowed: non-browser clients (the CLI,
    integration harnesses, native tooling) do not send one.  A PRESENT
    ``Origin`` must be a loopback origin -- a browser on any other site must
    not be able to open a socket to the local server.
    """
    if not origin:
        return True
    return origin_is_loopback(origin)


def _origin_allowed(request: Request) -> bool:
    """True when the request comes from a local web origin.

    Accepts a matching ``Origin`` header; falls back to the ``Referer``
    header (scheme+host part) when Origin is absent.  No header -> False.
    """
    raw = request.headers.get("origin") or request.headers.get("referer") or ""
    if not raw:
        return False
    if request.headers.get("referer") and not request.headers.get("origin"):
        match = re.match(r"^https?://[^/]+", raw, re.I)
        if not match:
            return False
        raw = match.group(0)
    return origin_is_loopback(raw)


def _origin_check_request(request: Request, route_name: str) -> bool:
    """Request-time origin guard for HTTP non-GET routes.

    A SEMANTIC mirror of :func:`websocket_origin_allowed`'s three branches
    (absent / same / foreign) -- NOT a signature mirror: that function takes
    an ``Optional[str]`` origin, this one takes a :class:`Request`.

    Deliberately differs from :func:`_origin_allowed` in exactly ONE way:
    an absent ``Origin``/``Referer`` pair is a **data condition**, not a
    refusal.  Non-browser clients (the CLI, integration harnesses, native
    tooling) send no ``Origin`` and are not a CSRF vector, so they are
    allowed -- and logged, so the assumption stays falsifiable: a
    browser-sent ``User-Agent: Mozilla/...`` on a state-changing route with
    an absent ``Origin`` is the trigger to revisit the option.

    Returns True on allow, False on refuse.  Never raises -- callers raise
    ``HTTPException(403)`` on False.  Every branch logs exactly one ``INFO``
    line, greppable form::

        origin_check: route=<name> result=<...> origin=<...>
    """
    raw = request.headers.get("origin") or request.headers.get("referer") or ""
    if not raw:
        logger.info(
            "origin_check: route=%s result=allow_absent origin=<absent>",
            route_name,
        )
        return True
    if request.headers.get("referer") and not request.headers.get("origin"):
        match = re.match(r"^https?://[^/]+", raw, re.I)
        raw = match.group(0) if match else raw
    if origin_is_loopback(raw):
        logger.info(
            "origin_check: route=%s result=allow_same origin=%s",
            route_name,
            raw,
        )
        return True
    logger.info(
        "origin_check: route=%s result=refuse_foreign origin=%s",
        route_name,
        raw,
    )
    return False
