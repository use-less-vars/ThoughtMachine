"""auth_boundary.py -- the single ingress gate in front of the local Web UI.

Before this module the Web UI had no answer to the only question that matters
at the boundary: *is this caller allowed to drive the agent?*  It had four
partial answers, each about a different thing:

* ``local_gate.localhost_only`` -- did the request arrive from this machine?
* ``origin_policy``, invoked per route -- which web page is the browser on?
* the CORS middleware -- which dev-server origins may read the response?
* the ``--allow-remote-bind`` warning -- a printed sentence, not a check.

None of those is authentication.  A local process other than the browser, a
page on a foreign site that gets past one guard, or anything at all that
reaches the port can drive the agent: the API had no credential.  This module
adds the credential, and it does so as ONE middleware rather than a decorator
per route, so the default is *deny* and a new route is protected by
construction instead of by remembering to decorate it.
``web_ui/backend/tests/test_auth_boundary.py`` pins that property by
enumerating ``server.app.routes``.

Order of checks (each one is cheap and each one can only refuse)
---------------------------------------------------------------
0. **Peer address.**  The caller must *be* this machine.  This is
   ``local_gate.localhost_only`` -- imported, not restated, so there is exactly
   one definition of "local" in this product.
1. **Proxy shape.**  A request carrying ``Forwarded``, ``X-Forwarded-For``,
   ``X-Forwarded-Host`` or ``X-Real-IP`` is refused outright, before anything
   else reads the scope.  Nothing in this product proxies to the Web UI, so
   such a header is either a misconfiguration or an attempt to launder a remote
   caller through a local proxy -- and the peer address checked in step 0 is
   *derived* from it: uvicorn's ``ProxyHeadersMiddleware`` is on by default
   (``proxy_headers=True``) and rewrites ``scope["client"]`` from
   ``X-Forwarded-For`` for any peer listed in ``FORWARDED_ALLOW_IPS``.  Refusing
   here turns that whole class of misconfiguration -- the ``127.0.0.1,::1``
   default (a local proxy, including a local attacker's proxy) and
   ``FORWARDED_ALLOW_IPS=*`` alike -- from a silent bypass into a loud refusal.
   An operator who genuinely fronts this server with a reverse proxy would need
   an explicit allow-path; none exists, and inventing one is a decision for
   whoever needs it, not a default.  See the harness bullet under "Deliberate
   decisions".
2. **Host validation.**  ``Host`` must be a loopback spelling.  This is the
   DNS-rebinding guard: an attacker who points ``evil.example`` at ``127.0.0.1``
   makes the browser send ``Origin: http://evil.example`` *and* connect to a
   loopback socket, so neither the peer address nor "the origin is on this
   machine" separates the attacker's page from the real one -- only the name
   the browser was told to connect to does.
3. **Exemption set.**  A short, explicit list (health probes, the session
   probe, the app shell and its assets).  Exempt paths still get Host
   validation: a rebinding page must not be able to read the health payload
   either.  Two names are trusted here that are not loopback spellings --
   ``testclient`` and ``testserver``, the peer address and default ``base_url``
   host Starlette's ``TestClient`` synthesises.  They weaken only the *name*
   check, never the credential: a request that carries one still faces steps 4
   and 5, so a rebinding page pointed at ``http://testserver`` is refused by its
   foreign ``Origin`` like any other.
4. **Origin.**  Applies, once and for every route, the predicate the per-route
   guards used to apply individually --
   :func:`web_ui.backend.origin_policy._origin_check_request` -- so "same site"
   is decided in one place instead of being restated on each route.  One small
   surface keeps the stricter
   :func:`web_ui.backend.origin_policy._origin_allowed` it had before this
   boundary: see :data:`STRICT_ORIGIN_PATH_PREFIXES`.
5. **Credential.**  Either the session cookie (an HMAC derived from the token,
   so the cookie never carries the bearer secret, *and* an issue time the MAC
   covers, so the cookie can expire -- see the lifetime bullet below) or
   ``Authorization: Bearer <token>`` for non-browser clients (the CLI,
   integration harnesses).  Both comparisons go through
   :func:`hmac.compare_digest`: a byte-at-a-time comparison of a 256-bit secret
   is a timing oracle, and this is the one place where getting that wrong is
   unrecoverable.
6. **WebSockets** are refused *before* ``accept()`` with close code ``1008``
   (policy violation), so a rejected handshake never becomes a live socket.

Deliberate decisions worth arguing with
---------------------------------------
* **Absent ``Origin`` is allowed through step 4** -- because step 5 follows it.
  ``_origin_check_request`` documents that allowance for non-browser clients,
  and it is only sound while something else authenticates the caller: a
  browser cannot present the credential without the cookie, and the cookie is
  ``SameSite=Strict``.  The stricter ``origin_policy._origin_allowed`` (absent
  -> refuse) was rejected as the *general* rule for a concrete reason: browsers
  omit ``Origin`` on same-origin subresource GETs, so refusing it would break the
  frontend this boundary is meant to protect, and it would break the CLI (which
  sends a bearer token and no ``Origin``) with no security gain.  It is not
  rejected everywhere: :data:`STRICT_ORIGIN_PATH_PREFIXES` names the one surface
  that keeps it -- the filesystem-mutating vault repair, which was guarded with
  ``_origin_allowed`` before this boundary existed, whose callers are not a
  browser subresource fetch, and where "no ``Origin`` at all" is itself a
  decision to refuse rather than to fall back on the credential.
* **The in-process test harness is trusted only when a flag says so.**
  Starlette's ``TestClient`` puts ``scope["client"] = ("testclient", 50000)``
  on every request, and the pre-existing suite drives the real app through it
  without a credential, so this boundary does honour that peer name.  It must
  not honour it *by default*.  An earlier version of this module argued that
  ``scope["client"]`` "is set by the server, never read from a header, so no
  network traffic can carry it" -- and that argument was wrong: uvicorn's
  ``ProxyHeadersMiddleware`` rewrites ``scope["client"]`` straight out of
  ``X-Forwarded-For`` (step 1), so ``client=("testclient", 50000)`` was one
  unauthenticated header away from bypassing this gate.  The allowance is
  therefore gated on :data:`TRUST_TEST_HARNESS`, which is ``False`` at import
  time, is never read from the environment, and is flipped only by the test
  suite through :func:`enable_test_harness_trust`.  It is keyed on the peer name
  rather than on ``Host`` because ``Host`` is attacker-controlled -- and on a
  flag as well because the peer name turned out to be, too.
* **``OPTIONS`` passes through unauthenticated.**  Preflights carry no
  credentials by specification, and ``OPTIONS`` never changes state.  The
  boundary is registered *inside* the CORS middleware, which answers preflights
  itself, so in the shipped stack this branch is unreachable; it exists so the
  boundary stays correct if someone re-registers it outermost.
* **The session cookie is bounded, not revocable.**  Its value is
  ``"<issued-at>.<mac>"``, the MAC covers the timestamp, and verification
  refuses anything stamped more than :data:`SESSION_MAX_AGE` (12 hours) in the
  past *or* in the future -- a future stamp is a clock-skewed or forged cookie,
  and "the clock was wrong" is not evidence of authenticity.  A stolen cookie is
  therefore worth at most 12 hours rather than forever.  Deliberately NOT
  provided: server-side revocation before expiry.  There is no session store,
  so a cookie cannot be cancelled, only outlived; ``GET /`` mints a fresh one on
  every visit, which is what keeps "opening the URL is the login" true without
  a login form.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from http.cookies import CookieError, SimpleCookie
from typing import Optional

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.websockets import WebSocket

from web_ui.backend import auth_token
from web_ui.backend.local_gate import TEST_HARNESS_CLIENT_HOSTS, localhost_only
from web_ui.backend.origin_policy import _origin_allowed, _origin_check_request

logger = logging.getLogger(__name__)

#: Name of the cookie the app shell mints and every same-site request replays.
COOKIE_NAME = "tm_web_ui_session"

#: Ingress paths a credential can arrive on, as reported by
#: :func:`credential_kind`.  Constants rather than bare strings because the
#: kind is echoed to a client by ``GET /api/auth/session``: a rename there would
#: be a contract change, not a typo a test would catch.
CREDENTIAL_COOKIE = "cookie"
CREDENTIAL_BEARER = "bearer"

#: HTTP status used when the credential is missing or wrong.
UNAUTHORIZED = 401

#: HTTP status used when Host/origin is wrong.  Deliberately distinct from
#: :data:`UNAUTHORIZED`: "your page is not allowed to talk to this server" and
#: "we do not know who you are" call for different reactions from an operator.
FORBIDDEN = 403

#: WebSocket close code for a refused handshake (RFC 6455 policy violation).
WS_POLICY_VIOLATION = 1008

#: Paths that need no credential.  EXACT matches -- see :func:`is_exempt`.
#:
#: * ``/health``, ``/api/health`` -- probes, already gated on the peer address
#:   by ``local_gate``.  ``/api/health`` is here because it is the health
#:   contract the launcher (``start_thoughtmachine.sh``) and the CI smoke jobs
#:   poll over loopback with no credential: refusing it would report the
#:   backend as dead;
#: * ``/api/health/containers`` -- container liveness probe;
#: * ``/api/auth/session`` -- must be able to answer "do you have a credential?"
#:   *before* the caller has one, otherwise the frontend cannot tell "log in"
#:   from "server down";
#: * ``/`` -- the app shell, and the only path that mints the cookie.
#:
#: ``/api/system/health`` is deliberately NOT here.  It reports live worker and
#: session counts -- deployment state, not a liveness bit -- so it belongs
#: behind the credential like every other ``/api`` route.  Nothing in the
#: product polls it (the UI uses ``/api/health``; see ``docs/ui_map.md``), so
#: exempting it only ever widened what an unauthenticated caller could read.
EXEMPT_PATHS = frozenset(
    {
        "/health",
        "/api/health",
        "/api/health/containers",
        "/api/auth/session",
        "/",
    }
)

#: Path PREFIXES that need no credential: the hashed asset bundle the shell
#: references.  A prefix (unlike a path) cannot be confused with an API route,
#: because every API path in this app starts with ``/api``.
EXEMPT_PREFIXES = ("/assets/",)

#: Path prefixes that keep the *strict* origin predicate rather than the default
#: lenient one.  :func:`web_ui.backend.origin_policy._origin_allowed` refuses a
#: request carrying neither ``Origin`` nor ``Referer``;
#: :func:`web_ui.backend.origin_policy._origin_check_request` -- the rule
#: everywhere else, see the module docstring -- allows that case through for
#: non-browser clients.  Only the filesystem-mutating vault repair is listed:
#: it was guarded with the strict predicate before this boundary existed, so
#: keeping it strict here is what makes this a move of the existing decision
#: rather than a quiet loosening of it.  Everything else is guarded by the
#: credential that follows, which no bare ``curl`` can supply.
STRICT_ORIGIN_PATH_PREFIXES = ("/api/vault/repair/",)

#: Exempt paths on which a ``GET`` mints the session cookie.
COOKIE_MINT_PATHS = frozenset({"/"})

#: Host names that are this machine, in every spelling a browser produces.
TRUSTED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Names Starlette's ``TestClient`` puts on the wire in place of a real host:
#: ``testserver`` is its default ``base_url`` host, ``testclient`` its peer
#: address (which reaches this check too when a test overrides ``client=``).
#: Trusting them weakens only the *name* check -- the origin and credential
#: checks still run -- so they live in their own constant, away from the
#: loopback set, and cannot be mistaken for it.
HARNESS_HOST_NAMES = frozenset({"testclient", "testserver"})

#: Whether an in-process ASGI harness peer may bypass this gate.  ``False`` is
#: the value a fresh import yields, and it is the only safe value: the harness
#: peer name is honoured only when this flag says so, because ``scope["client"]``
#: is *not* something a proxy cannot touch -- uvicorn's
#: ``ProxyHeadersMiddleware`` rewrites it from ``X-Forwarded-For``.  The test
#: suite flips this through :func:`enable_test_harness_trust`; nothing else may,
#: and nothing reads it from the environment.
TRUST_TEST_HARNESS = False

#: Domain separator for the cookie HMAC, versioned because the message being
#: signed now includes the issue time.  Changing it invalidates every issued
#: cookie -- which is the point of having one.
SESSION_CONTEXT = b"thoughtmachine.web_ui.session.v2"

#: How long a minted session cookie is accepted, in seconds (12 hours).
#: Verification refuses a cookie stamped further in the past *or* the future, so
#: a stolen value buys a bounded window rather than forever.  There is no
#: server-side revocation: the only way to end a session early is to rotate the
#: token, which invalidates every outstanding cookie at once.
SESSION_MAX_AGE = 12 * 3600

#: Headers no client of this product ever legitimately sends: see step 1 of the
#: order of checks.  A tuple rather than a set, because the refusal logs the
#: first one it finds and the message should not depend on hash order.
FORWARDED_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")


def enable_test_harness_trust(enabled: bool = True) -> None:
    """Let an in-process ASGI harness peer skip the boundary.

    FOR THE TEST SUITE.  Leaving this on disables the gate for any request whose
    peer address is ``testclient`` -- including a real request whose
    ``scope["client"]`` was rewritten to that value out of ``X-Forwarded-For``
    by a proxy middleware.  That is why the module default is ``False``, why
    nothing reads the value from the environment or from a request, and why the
    suite puts the previous value back when it is done.  Flipping it is a code
    change, visible in review, which is the point.
    """
    global TRUST_TEST_HARNESS
    TRUST_TEST_HARNESS = bool(enabled)


def is_exempt(path: str) -> bool:
    """True when *path* may be served without a credential.

    Exempt matching is EXACT for :data:`EXEMPT_PATHS` (a prefix match on
    ``/health`` would exempt ``/healthcheck-leak``) and prefix-only for
    :data:`EXEMPT_PREFIXES`.
    """
    if not path:
        return False
    if path in EXEMPT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in EXEMPT_PREFIXES)


def host_without_port(value: str) -> str:
    """Strip the port from a ``Host`` header value, lowercased.

    Handles the bracketed IPv6 form (``[::1]:8000``) and the bare IPv6 form
    (``::1``), which must not be split on its colons.
    """
    host = (value or "").strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def host_is_trusted(value: Optional[str]) -> bool:
    """True when the ``Host`` header names this machine.

    An absent ``Host`` is NOT trusted: certificate-less HTTP/1.0 and a
    hand-written ASGI scope can both omit it, and "no name" is exactly the
    input a rebinding attack would prefer.
    """
    host = host_without_port(value or "")
    if not host:
        return False
    return host in TRUSTED_HOSTS or host in HARNESS_HOST_NAMES


def connection_for(scope, receive=None, send=None):
    """A ``Request``-like view of *scope* for the guards that read it.

    ``starlette.requests.Request`` asserts ``scope["type"] == "http"``, so a
    WebSocket handshake must be wrapped in :class:`starlette.websockets.WebSocket`
    instead.  Both expose ``.client`` and ``.headers`` -- exactly the two
    attributes the reused guards (``localhost_only`` and
    ``_origin_check_request``) read -- so one wrapper serves both without a
    second copy of either decision.
    """
    if scope.get("type") == "websocket":
        return WebSocket(scope, receive, send)
    return Request(scope)


def peer_host(scope) -> Optional[str]:
    """The socket peer address from *scope*, or ``None`` when unreported."""
    client = scope.get("client")
    if not client:
        return None
    try:
        return client[0]
    except (IndexError, TypeError):
        return None


def forwarded_header(scope) -> Optional[str]:
    """The name of the proxy header present in *scope*, or ``None``.

    Presence is what matters, not validity: an empty or garbled
    ``X-Forwarded-For`` is still evidence that something in front of this server
    is speaking proxy, and a value that is merely *wrong* is no more trustworthy
    than one that is right.
    """
    headers = Headers(scope=scope)
    for name in FORWARDED_HEADERS:
        if headers.get(name) is not None:
            return name
    return None


def _session_mac(token: str, issued_at: int) -> str:
    """HMAC-SHA256 over the context and the issue time, keyed by *token*.

    The timestamp is inside the message: it is what expires the cookie, so it
    must not be editable by whoever is holding the cookie.
    """
    message = SESSION_CONTEXT + b"." + str(issued_at).encode("ascii")
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def session_credential(token: str, issued_at: Optional[int] = None) -> str:
    """The cookie value derived from *token*: ``"<issued-at>.<mac>"``.

    The cookie is derived rather than equal to the bearer token so that a
    cookie captured from browser storage cannot be replayed as an
    ``Authorization: Bearer`` credential (and vice versa) -- the two ingress
    paths share a secret without sharing a value.  It carries its own issue time
    because a derived value with no timestamp can never expire, and "forever"
    is the wrong lifetime for a secret that lives in a browser.
    """
    issued = int(time.time()) if issued_at is None else int(issued_at)
    return f"{issued}.{_session_mac(token, issued)}"


def session_credential_ok(
    candidate: Optional[str], token: str, now: Optional[int] = None
) -> bool:
    """True when *candidate* is a fresh, correctly-signed session cookie value.

    Three refusals, in this order: the value must be ``"<digits>.<mac>"`` (a raw
    token, a truncated value or junk is not one); the issue time must be within
    :data:`SESSION_MAX_AGE` of *now* **in either direction** -- a future stamp
    means a clock that cannot be trusted, which is not the same thing as a
    cookie that is valid longer; and the MAC for *that* timestamp must match,
    compared with ``hmac.compare_digest`` (via :func:`_matches`) so the
    comparison is not a timing oracle.  The digits are checked and bounded
    before ``int()`` sees them: the input is attacker-controlled, and ``int()``
    raises on absurdly long digit strings.
    """
    if not candidate:
        return False
    timestamp, separator, mac = candidate.partition(".")
    if not separator or not mac:
        return False
    if not (timestamp.isascii() and timestamp.isdigit()) or len(timestamp) > 12:
        return False
    issued_at = int(timestamp)
    current = int(time.time()) if now is None else int(now)
    if abs(current - issued_at) > SESSION_MAX_AGE:
        return False
    return _matches(mac, _session_mac(token, issued_at))


def _matches(candidate: Optional[str], expected: str) -> bool:
    """Constant-time comparison of *candidate* against *expected*.

    Compared as bytes: ``hmac.compare_digest`` raises ``TypeError`` for
    non-ASCII ``str`` input, and the candidate is attacker-controlled.
    """
    if not candidate:
        return False
    return hmac.compare_digest(
        candidate.encode("utf-8", "surrogatepass"), expected.encode("utf-8")
    )


def cookie_credential(scope) -> Optional[str]:
    """The session cookie value from *scope*, or ``None``.

    A malformed ``Cookie`` header is treated as "no cookie": a parse error is
    not a reason to 500 an ingress check, and it cannot authenticate anyone.
    """
    raw = Headers(scope=scope).get("cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:
        return None
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel is not None else None


def bearer_credential(scope) -> Optional[str]:
    """The ``Authorization: Bearer`` value from *scope*, or ``None``."""
    raw = Headers(scope=scope).get("authorization") or ""
    scheme, _, value = raw.partition(" ")
    if scheme.strip().lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def credential_candidates(scope):
    """Every credential *scope* carries, as ``(kind, value)`` in check order.

    The single place that knows which ingress paths exist.  Both are returned
    rather than just the first one found: the two are independent, so a caller
    that sends a *wrong* value on one channel alongside a *right* one on the
    other must still authenticate -- refusing it would make the answer depend
    on which header the client happened to put on the wire.  The order is the
    order the checks run in, and it is fixed: the cookie first, because that is
    the channel this product's own UI uses.
    """
    return (
        (CREDENTIAL_COOKIE, cookie_credential(scope)),
        (CREDENTIAL_BEARER, bearer_credential(scope)),
    )


# NOTE: there is deliberately no "first non-empty credential" helper here.  That
# question ("what did the caller present?", answered without verifying anything)
# reads like a natural primitive but is never the right one to act on: an
# unverified value can only be used to decide *something else*, and the only
# decision this module makes is :func:`credential_ok` / :func:`credential_kind`,
# which need the expected token and must try every candidate (see
# :func:`credential_candidates` on why the first one is not enough).


def _credential_value_ok(kind: str, value: Optional[str], token: str) -> bool:
    """Whether *value* from ingress *kind* authenticates against *token*.

    The two channels verify different things: the cookie is a *derived* value
    (``"<issued-at>.<mac>"``) that needs the signature and the freshness window,
    while the bearer header carries the secret itself and needs a constant-time
    comparison against it.
    """
    if kind == CREDENTIAL_COOKIE:
        return session_credential_ok(value, token)
    return _matches(value, token)


def credential_kind(scope, token: str) -> Optional[str]:
    """The ingress kind carrying a valid credential, or ``None``.

    :func:`credential_ok` with the *reason* kept instead of collapsed to a
    bool.  Callers that report to a client (``GET /api/auth/session``) need the
    kind to be derived here rather than sniffed with a second, drift-prone
    check.  ``None`` and the empty token both mean "not authenticated".
    """
    if not token:
        return None
    for kind, value in credential_candidates(scope):
        if _credential_value_ok(kind, value, token):
            return kind
    return None


def credential_ok(scope, token: str) -> bool:
    """True when *scope* carries a valid session cookie or bearer token."""
    if not token:
        # An empty expected secret would make an empty candidate "match".
        return False
    return credential_kind(scope, token) is not None


def set_cookie_header(token: str) -> bytes:
    """The ``Set-Cookie`` value the app shell ships.

    ``HttpOnly`` -- script cannot read the credential, so an XSS in the shell
    cannot exfiltrate it.  ``SameSite=Strict`` -- the cookie is not attached to
    any cross-site request, which is what makes the absent-``Origin`` allowance
    in step 4 safe.  ``Path=/`` -- the whole app is same-origin.
    ``Max-Age`` -- the cookie dies after :data:`SESSION_MAX_AGE` on the client as
    well as on the server: a credential that never expires is a credential that
    a stolen backup keeps forever.

    Deliberately NO ``Secure``: this server speaks plain HTTP on loopback (no
    TLS termination; ``uvicorn.run`` is called with no ``ssl_*`` arguments), and
    a ``Secure`` cookie is dropped by every browser on an ``http://`` origin,
    which would silently break the UI it is meant to protect.  The protections
    that do apply on loopback are the ones above.
    """
    return (
        f"{COOKIE_NAME}={session_credential(token)}; "
        f"Max-Age={SESSION_MAX_AGE}; HttpOnly; SameSite=Strict; Path=/"
    ).encode("latin-1")


class AuthBoundaryMiddleware:
    """ASGI middleware: the one place a request may become "authenticated".

    ``token`` is injected rather than read from disk when the caller already
    has it (tests, and any future supervisor that mints the token itself).
    Otherwise the token is resolved lazily from :mod:`web_ui.backend.auth_token`
    on first use, so importing the server never writes to the vault -- an
    import-time side effect on a credential store is a bug waiting for a test
    harness to hit it.
    """

    def __init__(self, app, token: Optional[str] = None, token_path=None):
        self.app = app
        self._token = token
        self._token_path = token_path
        self._token_error: Optional[str] = None

    # ── token resolution ────────────────────────────────────────────────
    def _resolve_token(self) -> Optional[str]:
        """The expected secret, or ``None`` when the store is unusable.

        ``None`` means "refuse everything non-exempt": a credential gate whose
        secret is missing must not degrade into a gate that accepts anything.
        The value itself is never logged.
        """
        if self._token is not None:
            return self._token
        if self._token_error is not None:
            return None
        try:
            self._token = auth_token.ensure_token(self._token_path)
        except Exception as exc:  # noqa: BLE001 - fail closed, never 500 the gate
            self._token_error = str(exc)
            logger.error(
                "auth_boundary: no web UI token available (%s) -- every "
                "non-exempt request will be refused",
                exc,
            )
            return None
        logger.info(
            "auth_boundary: credential gate armed (%s)",
            self._token_path or auth_token.token_path(),
        )
        return self._token

    # ── ASGI entry point ────────────────────────────────────────────────
    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            # lifespan, and anything a future server invents: not an ingress.
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        connection = connection_for(scope, receive, send)

        # 0. Peer address.  ``local_gate.localhost_only`` is the one definition
        #    of "this machine" -- imported rather than restated, so this gate and
        #    every other caller of ``local_gate`` cannot drift apart.  Its refusal
        #    (403, ``{"error": "localhost only"}``) is reused verbatim.
        peer_refusal = localhost_only(connection)
        if peer_refusal is not None:
            if kind == "websocket":
                await self._refuse(
                    scope, send, FORBIDDEN, {"error": "localhost only"}
                )
            else:
                await peer_refusal(scope, receive, send)
            return

        # 1. Proxy shape.  Nothing in this product proxies to the Web UI, so a
        #    forwarded header means either a misconfiguration or an attempt to
        #    launder a remote caller through a local proxy -- and it is the raw
        #    material of the bypass this whole ordering exists to close:
        #    uvicorn's ``ProxyHeadersMiddleware`` (on by default) rewrites
        #    ``scope["client"]`` from ``X-Forwarded-For``, so step 0 above may
        #    already be quoting a header.  Refuse before anything else reads the
        #    scope; WebSockets are refused with 1008 like any other refusal.
        proxy_header = forwarded_header(scope)
        if proxy_header is not None:
            logger.warning(
                "auth_boundary: refusing %s %s -- proxy header %r present; "
                "nothing in this product proxies to the Web UI",
                scope.get("method"),
                path,
                proxy_header,
            )
            await self._refuse(scope, send, FORBIDDEN, {"error": "proxied request"})
            return

        #    The in-process ASGI test harness is not an ingress -- but its peer
        #    name is evidence of the harness only while ``scope["client"]`` holds
        #    what the server put there, which step 1 shows a proxy middleware
        #    does not preserve.  Hence the flag: ``False`` at import, flipped only
        #    by the suite (``enable_test_harness_trust``).  Keyed on the peer name
        #    rather than ``Host`` because ``Host`` is attacker-controlled, and on
        #    a flag as well because the peer name turned out to be reachable over
        #    the wire.
        if TRUST_TEST_HARNESS and peer_host(scope) in TEST_HARNESS_CLIENT_HOSTS:
            await self.app(scope, receive, send)
            return

        # 2. Host validation -- applies to exempt paths too.
        if not host_is_trusted(Headers(scope=scope).get("host")):
            await self._refuse(scope, send, FORBIDDEN, {"error": "untrusted host"})
            return

        # 3. Exemption set (still Host-validated above).
        if is_exempt(path):
            await self._serve_exempt(scope, receive, send)
            return

        # 4. Origin -- the predicate the REST guards used, so the two layers
        #    cannot drift.  See the module docstring on the absent-Origin case.
        #    The vault-repair surface keeps the stricter predicate it was
        #    guarded with (see STRICT_ORIGIN_PATH_PREFIXES).
        origin_ok = (
            _origin_allowed(connection)
            if path.startswith(STRICT_ORIGIN_PATH_PREFIXES)
            else _origin_check_request(connection, "auth_boundary")
        )
        if not origin_ok:
            await self._refuse(scope, send, FORBIDDEN, {"error": "untrusted origin"})
            return

        # 5. Credential.  The origin check in step 4 above is applied to
        #    OPTIONS like to any other method -- a preflight is not exempt
        #    from it.  Only the credential is skipped here: a CORS preflight
        #    carries no credentials by specification and cannot change state.
        #    In the shipped stack CORSMiddleware sits outside this gate and
        #    answers preflights itself, so this branch is normally unreachable.
        if kind == "http" and scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        token = self._resolve_token()
        if token is not None and credential_ok(scope, token):
            await self.app(scope, receive, send)
            return

        # 6. WebSockets land here too: _refuse closes with 1008 before the
        #    handshake is accepted, so a rejected socket never comes alive.
        await self._refuse(scope, send, UNAUTHORIZED, {"error": "unauthorized"})

    # ── helpers ─────────────────────────────────────────────────────────
    async def _serve_exempt(self, scope, receive, send) -> None:
        """Forward an exempt request, minting the session cookie on ``GET /``."""
        mint = (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and (scope.get("path") or "") in COOKIE_MINT_PATHS
        )
        if mint:
            token = self._resolve_token()
            if token is not None:
                await self.app(scope, receive, _cookie_minting_send(send, token))
                return
        await self.app(scope, receive, send)

    async def _refuse(self, scope, send, status: int, payload: dict) -> None:
        """Refuse *scope*; never raises."""
        reason = payload.get("error", "refused")
        if scope.get("type") == "websocket":
            logger.warning(
                "auth_boundary: refusing WebSocket handshake (path=%s reason=%s)",
                scope.get("path"),
                reason,
            )
            try:
                await send({"type": "websocket.close", "code": WS_POLICY_VIOLATION})
            except Exception as exc:  # noqa: BLE001 - refusal must never raise
                logger.warning(
                    "auth_boundary: websocket.close failed during rejection: %s", exc
                )
            return

        logger.warning(
            "auth_boundary: refusing %s %s (%s)",
            scope.get("method"),
            scope.get("path"),
            reason,
        )
        body = json.dumps(payload).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if status == UNAUTHORIZED:
            headers.append(
                (b"www-authenticate", b'Bearer realm="thoughtmachine-web-ui"')
            )
        try:
            await send(
                {"type": "http.response.start", "status": status, "headers": headers}
            )
            await send({"type": "http.response.body", "body": body})
        except Exception as exc:  # noqa: BLE001 - refusal must never raise
            logger.warning("auth_boundary: refusal response failed: %s", exc)


def _cookie_minting_send(send, token: str):
    """Wrap *send* so the response carries the session cookie."""
    header = set_cookie_header(token)

    async def wrapped(message):
        if message.get("type") == "http.response.start":
            headers = list(message.get("headers") or [])
            headers.append((b"set-cookie", header))
            message = dict(message, headers=headers)
        await send(message)

    return wrapped
