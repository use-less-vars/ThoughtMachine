"""The web UI credential boundary: policy pinned, not described.

The API used to authenticate with nothing.  This module pins the replacement
(``web_ui/backend/auth_boundary.py``) the way ``test_local_gate_shared.py`` pins
the loopback gate: by enumerating the decision points and asserting the answer
for each, so a policy change has to be a test change.

Three properties are load-bearing and each has a test that fails by
construction:

* **Deny by default.**  ``test_every_non_exempt_route_refuses_an_unauthenticated_caller``
  walks ``server.app.routes`` -- the app's own routing table -- and demands a
  refusal for every path that is not on the exemption list.  A route added in a
  future wave is protected without anybody remembering to protect it, and a
  path silently added to :data:`EXEMPT_PATHS` breaks
  ``test_exempt_set_is_the_documented_list``.
* **Host before exemption.**  A rebinding page must not read even ``/health``.
* **A credential, not a heuristic.**  Cookie or bearer, compared with
  ``hmac.compare_digest``; a wrong credential is refused exactly like no
  credential.

Requests here deliberately use ``client=("127.0.0.1", 50000)`` so they look
like real traffic.  The default ``TestClient`` peer is ``testclient``, which
the boundary may treat as an in-process harness rather than an ingress -- but
only while ``auth_boundary.TRUST_TEST_HARNESS`` is on, which the root
``conftest.py`` arranges for the duration of the session.  The flag ships
``False``, and that default is pinned across a fresh interpreter by
``test_the_trust_flag_is_false_on_a_fresh_import``; the allowance itself is
pinned off by ``test_the_harness_peer_is_refused_when_the_flag_is_off``, and
``test_a_forwarded_header_is_refused_even_with_a_valid_credential`` pins that
no request arriving over the wire reaches it by claiming the name.  Using the
default peer for the tests below would test nothing.
"""

from __future__ import annotations

import contextlib
import logging
import pathlib
import re
import subprocess
import sys
import time

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from web_ui.backend import auth_boundary, server
from web_ui.backend.auth_boundary import (
    COOKIE_NAME,
    EXEMPT_PATHS,
    SESSION_MAX_AGE,
    AuthBoundaryMiddleware,
    session_credential,
    session_credential_ok,
)

#: A literal token -- the boundary never validates the token's shape, and a
#: fixed value keeps the cookie/bearer assertions readable.  64 hex characters,
#: the shape ``auth_token.ensure_token`` mints.
TOKEN = "8f" * 32

#: Peer address that is this machine, and therefore NOT the test harness.
LOCAL_PEER = ("127.0.0.1", 50000)

#: The Origin a browser on the served frontend sends.
SAME_SITE_ORIGIN = "http://localhost:5173"

#: An Origin no page on this machine can legitimately have.
FOREIGN_ORIGIN = "http://evil.example"


# ── fixtures ────────────────────────────────────────────────────────────────


def _build_app() -> FastAPI:
    """A miniature app carrying the real boundary with a fixed token."""
    app = FastAPI()
    app.add_middleware(AuthBoundaryMiddleware, token=TOKEN)

    @app.get("/")
    def shell():
        return {"shell": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/system/health")
    def system_health():
        return {"status": "ok"}

    @app.get("/api/health/containers")
    def containers():
        return {"status": "ok"}

    @app.get("/api/auth/session")
    def auth_session():
        return {"authenticated": False}

    @app.get("/assets/app.js")
    def asset():
        return {"asset": True}

    @app.get("/api/secret")
    def secret():
        return {"secret": "vault-global-state"}

    @app.post("/api/secret")
    def secret_write():
        return {"written": True}

    # The two vault-repair verbs, so the strict-origin prefix has a surface to
    # be strict about.  Adding routes here does not weaken
    # ``test_exempt_set_is_the_documented_list``, which pins the exemption set
    # rather than the route table.
    @app.get("/api/vault/repair/status")
    def vault_repair_status():
        return {"findings": []}

    @app.post("/api/vault/repair/apply")
    def vault_repair_apply():
        return {"applied": []}

    @app.post("/api/config/mode")
    def set_config_mode():
        return {"mode": "chat"}

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"live": True})

    return app


@pytest.fixture(scope="module")
def app():
    return _build_app()


@pytest.fixture
def client(app):
    """A client whose peer address looks like real loopback traffic."""
    return TestClient(app, client=LOCAL_PEER)


def _cookie_header(token: str = TOKEN) -> dict:
    return _cookie_with(session_credential(token))


def _cookie_with(value: str) -> dict:
    return {"cookie": f"{COOKIE_NAME}={value}"}


@contextlib.contextmanager
def _harness_trust(enabled: bool):
    """Run the block with ``auth_boundary.TRUST_TEST_HARNESS`` set as given.

    The session fixture in the root ``conftest.py`` turns the flag on for the
    whole run; this restores whatever was in force, so a test that needs the
    shipped default cannot leak that state into its neighbours.
    """
    previous = auth_boundary.TRUST_TEST_HARNESS
    auth_boundary.enable_test_harness_trust(enabled)
    try:
        yield
    finally:
        auth_boundary.enable_test_harness_trust(previous)


# ── 1. the exemption set is explicit ────────────────────────────────────────


def test_exempt_set_is_the_documented_list():
    """Exempting a path is a policy change, so it must edit this test."""
    assert EXEMPT_PATHS == frozenset(
        {
            "/health",
            "/api/health",
            "/api/system/health",
            "/api/health/containers",
            "/api/auth/session",
            "/",
        }
    )


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/api/system/health",
        "/api/health/containers",
        "/api/auth/session",
        "/assets/app.js",
    ],
)
def test_exempt_paths_need_no_credential(client, path):
    response = client.get(path, headers={"origin": SAME_SITE_ORIGIN})
    assert response.status_code == 200, response.text


def test_exempt_matching_is_exact_not_prefix():
    """``/healthcheck`` must not inherit ``/health``'s exemption."""
    assert auth_boundary.is_exempt("/health")
    assert not auth_boundary.is_exempt("/healthcheck")
    assert auth_boundary.is_exempt("/assets/app.js")
    assert not auth_boundary.is_exempt("/assets")


# ── 2. deny by default ──────────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_protected_route_without_a_credential_is_refused(client, method):
    response = client.request(
        method, "/api/secret", headers={"origin": SAME_SITE_ORIGIN}
    )
    assert response.status_code == 401, response.text
    assert response.json() == {"error": "unauthorized"}
    assert response.headers["www-authenticate"].startswith("Bearer")


# ── 3. the credential itself ────────────────────────────────────────────────


def test_bearer_token_is_accepted(client):
    response = client.get(
        "/api/secret",
        headers={"origin": SAME_SITE_ORIGIN, "authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"secret": "vault-global-state"}


def test_bearer_scheme_is_case_insensitive_but_the_value_is_not(client):
    ok = client.get(
        "/api/secret",
        headers={"origin": SAME_SITE_ORIGIN, "authorization": f"bearer {TOKEN}"},
    )
    assert ok.status_code == 200

    wrong_value = client.get(
        "/api/secret",
        headers={
            "origin": SAME_SITE_ORIGIN,
            "authorization": f"Bearer {TOKEN.upper()}",
        },
    )
    assert wrong_value.status_code == 401


def test_raw_token_is_not_a_cookie_value(client):
    """The cookie is an HMAC of the token, not the token: one secret, two values."""
    response = client.get(
        "/api/secret",
        headers={"origin": SAME_SITE_ORIGIN, "cookie": f"{COOKIE_NAME}={TOKEN}"},
    )
    assert response.status_code == 401


def test_the_app_shell_mints_a_session_cookie(client):
    """The cookie is an HMAC over the token *and the time it was issued*."""
    response = client.get("/")
    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{COOKIE_NAME}=")
    value = set_cookie[len(f"{COOKIE_NAME}=") :].split(";", 1)[0]
    assert value != TOKEN
    match = re.fullmatch(r"(\d+)\.([0-9a-f]{64})", value)
    assert match is not None, value
    assert value == session_credential(TOKEN, issued_at=int(match.group(1)))
    # Compared attribute-by-attribute, not by substring: ``"Max-Age=432000" in
    # set_cookie`` is satisfied by a WRONG ``Max-Age=4320000`` as well, so the
    # assertion would pass for a cookie that outlives the server-side window.
    attributes = [part.strip() for part in set_cookie.split(";")]
    assert f"Max-Age={SESSION_MAX_AGE}" in attributes
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    assert "Path=/" in set_cookie
    # Deliberately absent: the server speaks plain http on loopback, and a
    # ``Secure`` cookie would be dropped by every browser (see the docstring of
    # ``auth_boundary.set_cookie_header``).
    assert "Secure" not in set_cookie


def test_minted_cookie_opens_a_protected_route(client):
    assert client.get("/").status_code == 200
    response = client.get("/api/secret", headers={"origin": SAME_SITE_ORIGIN})
    assert response.status_code == 200, response.text


def test_a_cookie_minted_for_another_token_is_refused(client):
    """Rotation invalidates outstanding cookies -- the point of the HMAC."""
    other = session_credential("ff" * 32)
    response = client.get(
        "/api/secret",
        headers={"origin": SAME_SITE_ORIGIN, "cookie": f"{COOKIE_NAME}={other}"},
    )
    assert response.status_code == 401


def test_malformed_cookie_header_is_refused_not_crashed(client):
    response = client.get(
        "/api/secret",
        headers={"origin": SAME_SITE_ORIGIN, "cookie": "==not-a-cookie=="},
    )
    assert response.status_code == 401


def test_a_session_cookie_older_than_the_max_age_is_refused(client):
    """The cookie carries its issue time, so it can and does expire."""
    stale = session_credential(
        TOKEN, issued_at=int(time.time()) - SESSION_MAX_AGE - 60
    )
    response = client.get(
        "/api/secret", headers={"origin": SAME_SITE_ORIGIN, **_cookie_with(stale)}
    )
    assert response.status_code == 401


def test_a_future_dated_session_cookie_is_refused(client):
    """The window is bounded in both directions, not just backwards."""
    ahead = session_credential(
        TOKEN, issued_at=int(time.time()) + SESSION_MAX_AGE + 60
    )
    response = client.get(
        "/api/secret", headers={"origin": SAME_SITE_ORIGIN, **_cookie_with(ahead)}
    )
    assert response.status_code == 401


def test_a_session_cookie_with_a_non_numeric_timestamp_is_refused(client):
    """Junk before the dot is a refusal, not a ``ValueError`` in the log."""
    mac = session_credential(TOKEN).partition(".")[2]
    response = client.get(
        "/api/secret",
        headers={"origin": SAME_SITE_ORIGIN, **_cookie_with(f"not-a-timestamp.{mac}")},
    )
    assert response.status_code == 401


def test_a_current_session_cookie_still_authenticates(client):
    response = client.get(
        "/api/secret", headers={"origin": SAME_SITE_ORIGIN, **_cookie_header()}
    )
    assert response.status_code == 200, response.text


def test_the_minted_cookie_is_stamped_with_the_current_clock(client):
    """The stamp is the wall clock, not a constant or a counter.

    Expiry (and the ``Max-Age`` the browser honours) is derived from this
    number, so a fixed or lagging stamp would silently give every session a
    different lifetime than ``SESSION_MAX_AGE``.
    """
    response = client.get("/")
    set_cookie = response.headers["set-cookie"]
    value = set_cookie[len(f"{COOKIE_NAME}=") :].split(";", 1)[0]
    timestamp = value.partition(".")[0]
    assert timestamp.isdigit(), value
    assert abs(int(timestamp) - time.time()) <= 5


def test_the_max_age_boundary_is_inclusive_at_exactly_the_max_age():
    """``abs(now - issued) > SESSION_MAX_AGE`` is the refusal: equal is ACCEPTED.

    Pinned with an explicit ``now`` because the same assertion driven through
    the middleware would depend on the wall clock not ticking between minting
    the cookie and handling the request (one second of drift turns the
    boundary case into the refused case, i.e. a flake).  Both directions are
    checked: the window is symmetric, so a stamp ``SESSION_MAX_AGE`` in the
    future is inside it too.
    """
    now = 1_700_000_000
    at_the_limit = session_credential(TOKEN, issued_at=now - SESSION_MAX_AGE)
    assert session_credential_ok(at_the_limit, TOKEN, now=now) is True
    one_second_past = session_credential(TOKEN, issued_at=now - SESSION_MAX_AGE - 1)
    assert session_credential_ok(one_second_past, TOKEN, now=now) is False
    ahead_at_the_limit = session_credential(TOKEN, issued_at=now + SESSION_MAX_AGE)
    assert session_credential_ok(ahead_at_the_limit, TOKEN, now=now) is True


def test_a_cookie_one_second_past_the_max_age_is_refused_by_the_middleware(client):
    """The boundary above, seen through the gate: ``MAX_AGE + 1`` is a 401.

    Only the refused side is driven over HTTP, because it is the side clock
    drift cannot flip (a later ``now`` only makes the cookie staler).
    """
    stale = session_credential(TOKEN, issued_at=int(time.time()) - SESSION_MAX_AGE - 1)
    response = client.get(
        "/api/secret", headers={"origin": SAME_SITE_ORIGIN, **_cookie_with(stale)}
    )
    assert response.status_code == 401


def test_a_thirteen_digit_timestamp_is_refused_even_with_a_valid_mac():
    """``len(timestamp) > 12`` is checked before ``int()`` sees the digits.

    ``now`` is set to the very stamp the cookie carries, so the age check
    cannot be the reason and the MAC does match: the only thing refusing this
    value is the length guard.  ``time.time()`` is ten digits, so no stamp this
    server mints can be rejected by it.
    """
    thirteen = 1_234_567_890_123
    candidate = session_credential(TOKEN, issued_at=thirteen)
    timestamp = candidate.partition(".")[0]
    assert len(timestamp) == 13, candidate
    assert session_credential_ok(candidate, TOKEN, now=thirteen) is False
    # Control: the same construction one digit shorter, same ``now``, is
    # accepted -- proving the MAC/age path is intact and length is the reason.
    twelve = 123_456_789_012
    assert session_credential_ok(
        session_credential(TOKEN, issued_at=twelve), TOKEN, now=twelve
    ) is True


# ── 4. Host validation (DNS rebinding) ──────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    ["localhost", "localhost:8000", "127.0.0.1", "127.0.0.1:8000", "[::1]:8000"],
)
def test_loopback_host_spellings_are_trusted(host):
    assert auth_boundary.host_is_trusted(host)


@pytest.mark.parametrize(
    "host",
    [
        "",
        None,
        "evil.example",
        "evil.example:8000",
        "127.0.0.1.evil.example",
        "localhost.evil.example",
        "0.0.0.0",
    ],
)
def test_non_loopback_host_spellings_are_not_trusted(host):
    assert not auth_boundary.host_is_trusted(host)


def test_untrusted_host_is_refused_even_on_an_exempt_path(app):
    """A rebinding page must not read the health payload either."""
    rebinding = TestClient(app, base_url="http://evil.example:8000", client=LOCAL_PEER)
    response = rebinding.get("/health")
    assert response.status_code == 403
    assert response.json() == {"error": "untrusted host"}


# ── 5. origin, even with a valid credential ─────────────────────────────────


def test_foreign_origin_is_refused_with_a_valid_bearer(client):
    response = client.get(
        "/api/secret",
        headers={"origin": FOREIGN_ORIGIN, "authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 403
    assert response.json() == {"error": "untrusted origin"}


def test_foreign_origin_is_refused_with_a_valid_cookie(client):
    headers = {"origin": FOREIGN_ORIGIN, **_cookie_header()}
    assert client.get("/api/secret", headers=headers).status_code == 403


def test_same_site_origin_is_accepted(client):
    response = client.get(
        "/api/secret",
        headers={"origin": SAME_SITE_ORIGIN, "authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200


def test_absent_origin_is_allowed_because_the_credential_follows(client):
    """The CLI sends no ``Origin``; without a credential it still gets nowhere."""
    with_token = client.get("/api/secret", headers={"authorization": f"Bearer {TOKEN}"})
    assert with_token.status_code == 200
    assert client.get("/api/secret").status_code == 401


# ── 5b. the vault repair keeps the strict origin predicate ─────────────────


def test_absent_origin_is_refused_on_vault_repair_apply_with_a_credential(client):
    """``STRICT_ORIGIN_PATH_PREFIXES`` refuses the bare POST before step 5.

    Same client, same credential, same absent ``Origin`` that is accepted on
    ``/api/secret`` above -- refused here, and refused with the *origin* error
    rather than the credential one, so the decision is demonstrably step 4's.
    """
    response = client.post(
        "/api/vault/repair/apply", headers={"authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 403
    assert response.json() == {"error": "untrusted origin"}


def test_absent_origin_is_refused_on_vault_repair_status(client):
    """Both verbs under the prefix are strict, not just the mutating one."""
    response = client.get(
        "/api/vault/repair/status", headers={"authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 403
    assert response.json() == {"error": "untrusted origin"}


def test_absent_origin_falls_through_to_the_credential_gate_elsewhere(client):
    """The strict set is a set: every other route keeps the lenient allowance."""
    response = client.post("/api/config/mode", headers={"authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json() != {"error": "untrusted origin"}


def test_foreign_origin_is_refused_on_every_vault_repair_verb(client):
    for method, path in (
        ("get", "/api/vault/repair/status"),
        ("post", "/api/vault/repair/apply"),
    ):
        response = getattr(client, method)(
            path, headers={"origin": FOREIGN_ORIGIN, "authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 403, path
        assert response.json() == {"error": "untrusted origin"}, path


# ── 6. WebSockets are refused before accept ─────────────────────────────────


def test_websocket_handshake_without_a_credential_is_refused_with_1008(client):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws"):
            pass  # pragma: no cover - the handshake never completes
    assert excinfo.value.code == auth_boundary.WS_POLICY_VIOLATION == 1008


def test_websocket_handshake_with_a_cookie_is_accepted(client):
    with client.websocket_connect("/ws", headers=_cookie_header()) as ws:
        assert ws.receive_json() == {"live": True}


def test_websocket_handshake_with_a_bearer_token_is_accepted(client):
    with client.websocket_connect(
        "/ws", headers={"authorization": f"Bearer {TOKEN}"}
    ) as ws:
        assert ws.receive_json() == {"live": True}


def test_websocket_handshake_from_a_foreign_origin_is_refused(client):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/ws", headers={"origin": FOREIGN_ORIGIN, **_cookie_header()}
        ):
            pass  # pragma: no cover - the handshake never completes
    assert excinfo.value.code == 1008


# ── 7. loopback peer gate + harness allowance (flag-gated) ──────────────────


def test_non_local_peer_is_refused_before_the_credential(app):
    """``local_gate``'s refusal is reused verbatim, so its tests keep passing."""
    remote = TestClient(app, client=("203.0.113.9", 45678))
    response = remote.get("/health", headers={"authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 403
    assert response.json() == {"error": "localhost only"}


def test_the_trust_flag_is_false_on_a_fresh_import():
    """Regression guard: the shipped default must never drift to ``True``.

    Read in a subprocess, because this interpreter's session fixture has
    already turned the flag on -- the value that matters is the one a fresh
    import of the module yields.
    """
    root = pathlib.Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from web_ui.backend import auth_boundary;"
            " print(auth_boundary.TRUST_TEST_HARNESS)",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"


def test_the_harness_peer_is_allowed_when_the_flag_is_on(app):
    """The other direction of the flag: on, the in-process peer is allowed.

    Without this, the refusal below would also pass for a boundary that
    refuses the harness peer unconditionally -- i.e. a boundary whose flag
    does nothing at all.
    """
    harness = TestClient(app)  # peer == ("testclient", 50000)
    with _harness_trust(True):
        response = harness.get("/api/secret")
    assert response.status_code == 200, response.text


def test_the_harness_peer_is_refused_when_the_flag_is_off(app):
    """A peer that merely calls itself ``testclient`` is not a credential.

    The test that would have caught the finding: with the trust flag off, the
    harness peer is an ordinary unauthenticated caller and gets the ordinary
    refusal.
    """
    harness = TestClient(app)  # peer == ("testclient", 50000)
    with _harness_trust(False):
        assert harness.get("/api/secret").status_code == 401


#: One case per name in ``auth_boundary.FORWARDED_HEADERS``, plus a second
#: value for ``x-forwarded-for`` (the header the finding used) so both the
#: "names the harness" and "names loopback" shapes are exercised.
FORWARDED_HEADER_CASES = [
    ("x-forwarded-for", "testclient"),
    ("x-forwarded-for", "127.0.0.1"),
    ("x-forwarded-host", "evil.example"),
    ("x-real-ip", "127.0.0.1"),
    ("forwarded", "for=127.0.0.1"),
]


def test_the_forwarded_cases_cover_every_header_the_boundary_refuses():
    """``FORWARDED_HEADERS`` cannot grow without a case being added here.

    Otherwise a header could be added to the refusal tuple (an
    ``x-forwarded-proto``, say) and the parametrised test below would keep
    claiming coverage of a set it no longer covers.
    """
    covered = {header for header, _ in FORWARDED_HEADER_CASES}
    assert covered == set(auth_boundary.FORWARDED_HEADERS)


@pytest.mark.parametrize("header,value", FORWARDED_HEADER_CASES)
def test_a_forwarded_header_is_refused_even_with_a_valid_credential(app, header, value):
    """A request that claims to come through a proxy is refused outright.

    This is the shape the finding took: uvicorn's ``ProxyHeadersMiddleware``
    rewrites ``scope["client"]`` from ``X-Forwarded-For``, so a remote caller
    could name itself ``testclient`` and inherit the harness allowance.  The
    peer here *is* trusted by the flag -- the request would be a 200 without
    the proxy-shape check -- so only that check can produce the 403.
    """
    harness = TestClient(app)  # peer == ("testclient", 50000), flag on
    response = harness.get(
        "/api/secret",
        headers={
            header: value,
            "host": "localhost",
            "origin": SAME_SITE_ORIGIN,
            "authorization": f"Bearer {TOKEN}",
        },
    )
    assert response.status_code == 403
    assert response.json() == {"error": "proxied request"}


def test_the_proxy_refusal_applies_to_an_exempt_path(app):
    """Exemption is a property of the path; a proxied request is refused anyway.

    Step 1 of the order of checks runs before the exemption set is consulted,
    so ``/health`` -- which needs no credential and would be a 200 for this
    very client -- cannot be reached through anything speaking proxy either.
    """
    harness = TestClient(app)  # peer == ("testclient", 50000), flag on
    control = harness.get("/health")
    assert control.status_code == 200, control.text
    response = harness.get("/health", headers={"x-forwarded-for": "1.2.3.4"})
    assert response.status_code == 403
    assert response.json() == {"error": "proxied request"}


def test_the_forwarded_refusal_names_the_header_in_the_log(app, caplog):
    """The 403 is logged with the header that caused it, so it is diagnosable."""
    harness = TestClient(app)
    with caplog.at_level(logging.WARNING, logger="web_ui.backend.auth_boundary"):
        harness.get("/api/secret", headers={"x-real-ip": "127.0.0.1"})
    assert "x-real-ip" in caplog.text


# ── 8. the real app: registered, ordered, and denying by construction ───────


def test_boundary_is_registered_inside_the_cors_middleware():
    """CORS must stay outermost, or every preflight would be 401'd."""
    classes = [middleware.cls for middleware in server.app.user_middleware]
    assert AuthBoundaryMiddleware in classes
    boundary_at = classes.index(AuthBoundaryMiddleware)
    cors_at = classes.index(server.CORSMiddleware)
    assert cors_at < boundary_at, (
        "AuthBoundaryMiddleware must be registered BEFORE CORSMiddleware; "
        "because add_middleware prepends, the LAST registration is the "
        "OUTERMOST one. Registered after CORS, the boundary would 401 every "
        "preflight."
    )


def _concrete(route: APIRoute) -> str:
    """Route template with path params replaced by a literal placeholder."""
    return re.sub(r"\{[^}]+\}", "placeholder", route.path)


def test_every_non_exempt_route_refuses_an_unauthenticated_caller():
    """Deny by default, proved against the app's own routing table.

    A route added in a later wave is covered without being told to be: it is
    not on the exemption list, so this test drives it unauthenticated and
    demands a 401.  The refusal happens in the middleware, before routing, so
    no handler executes.
    """
    client = TestClient(server.app, client=LOCAL_PEER)
    protected = [
        _concrete(route)
        for route in server.app.routes
        if isinstance(route, APIRoute) and not auth_boundary.is_exempt(route.path)
    ]
    assert protected, "no protected routes found -- the enumeration is broken"

    refused = []
    for path in protected:
        response = client.get(path)
        if response.status_code != 401:
            refused.append((path, response.status_code, response.text[:80]))
    assert refused == [], (
        f"non-exempt routes answered an unauthenticated caller: {refused}"
    )


def test_real_app_refuses_its_websocket_without_a_credential():
    client = TestClient(server.app, client=LOCAL_PEER)
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws"):
            pass  # pragma: no cover - the handshake never completes
    assert excinfo.value.code == 1008


# ── 9. the harness allowance survives the suite's own purge + re-import ─────


#: A real, non-exempt ``GET`` route on the *full* app (the one the integration
#: tests purge and re-import).  Chosen empirically: with the harness allowance
#: in force this path answers 200 to an uncredentialed in-process harness peer,
#: and with the allowance off it answers 401 -- so it measures the boundary and
#: nothing else.
_FRESH_APP_PROTECTED_PATH = "/api/vault/status"


@contextlib.contextmanager
def _a_fresh_backend_from_the_harness():
    """Purge and re-import the backend exactly the way the suite does.

    ``tests.integration.harness.start_backend`` is the suite's own cold-start:
    it redirects ``HOME``, drops every ``web_ui.backend*`` entry from
    ``sys.modules`` and imports the app again.  Yields ``(fresh_app,
    fresh_boundary)`` and tears the whole thing down afterwards (module state,
    environment and the throwaway HOME are all restored by ``stop_fn``).
    """
    from tests.integration import harness

    fresh_app, _tmp_home, stop_fn = harness.start_backend()
    try:
        fresh_boundary = sys.modules["web_ui.backend.auth_boundary"]
        yield fresh_app, fresh_boundary
    finally:
        stop_fn()


def test_a_freshly_imported_boundary_carries_the_suite_trust_value():
    """The conftest import hook re-applies the allowance to the re-imported module.

    The trust flag lives *inside* ``web_ui.backend.auth_boundary``, so the
    suite's own purge throws it away: the re-imported module runs its module
    body and gets the shipped default ``False``.  Without the hook in the root
    ``conftest.py`` the fresh app below refuses the harness peer and this test
    fails with a 401 -- which is the whole point: a guard that silently stops
    being applied after a re-import is worse than no guard at all.
    """
    with _a_fresh_backend_from_the_harness() as (fresh_app, fresh_boundary):
        assert fresh_boundary is not auth_boundary, (
            "the purge did not produce a new module object -- this test would "
            "be pinning the module the conftest already fixed up"
        )
        assert fresh_boundary.TRUST_TEST_HARNESS is True
        response = TestClient(fresh_app).get(_FRESH_APP_PROTECTED_PATH)
        assert response.status_code == 200, response.text


def test_a_freshly_imported_boundary_honours_the_suite_value_when_it_is_off():
    """The hook applies the suite's CURRENT value, not a hardcoded ``True``.

    A test that turns the allowance off (``_harness_trust(False)``) and then
    triggers a purge must get a module that is also off: otherwise the hook
    would re-arm the very bypass such a test is measuring, and the negative
    direction of the flag would silently stop being testable.
    """
    with _harness_trust(False):
        with _a_fresh_backend_from_the_harness() as (fresh_app, fresh_boundary):
            assert fresh_boundary.TRUST_TEST_HARNESS is False
            response = TestClient(fresh_app).get(_FRESH_APP_PROTECTED_PATH)
            assert response.status_code == 401, response.text
