"""``GET /api/auth/session``: the one route that answers without a credential.

The boundary refuses an unauthenticated caller on every non-exempt path.  That
is the right default, but it leaves the frontend unable to tell "you need to log
in" from "the server is down": both arrive as a refused request.  This route
closes that gap, and because it is exempt from the credential it must repeat the
check itself -- which makes it the one place where the answer could silently
drift from the middleware's.

These tests therefore run against the REAL app (``server.app``, the same object
the process serves) rather than a miniature harness app, and pin three things:

* the answer is *correct* on each ingress path (bearer header, session cookie,
  neither) and reports which one worked;
* the answer is *narrow*: it never echoes the token, a store path or a reason,
  so an unauthenticated caller cannot use it to probe the secret;
* the exempt neighbours stay narrow too: ``/api/health`` is one liveness bit,
  and ``/api/system/health`` -- live worker/session counts -- is no longer
  exempt at all.

The token is patched rather than minted: ``auth_token.ensure_token`` is the seam
both the middleware and the route resolve through, so one patch arms both.  The
middleware caches what it resolves on first use, so the module uses ONE token
(any other value would make a later test assert against the cache instead of the
check) and also overwrites an already-cached value, which is what a previous
test module in the same run would have left behind.
"""
import pytest
from fastapi.testclient import TestClient

from web_ui.backend import auth_boundary, auth_token, server

#: The secret the app is armed with for the whole module.  64 hex characters,
#: the shape ``auth_token`` mints.
TOKEN = "3a" * 32

#: A real-looking loopback peer: with ``client=`` the harness peer name is
#: never in play, so the tests exercise the same path a browser does.
LOCAL_PEER = ("127.0.0.1", 50000)

#: The Vite dev origin, which the CORS setup and this boundary both trust.
SAME_SITE_ORIGIN = "http://localhost:5173"


def _boundary_instances():
    """Every ``AuthBoundaryMiddleware`` in the app's already-built stack."""
    found = []
    node = getattr(server.app, "middleware_stack", None)
    seen = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, auth_boundary.AuthBoundaryMiddleware):
            found.append(node)
        node = getattr(node, "app", None)
    return found


@pytest.fixture(scope="module", autouse=True)
def _armed_token():
    """Arm the real app with :data:`TOKEN` without touching a real vault.

    Two seams, because the secret reaches the app two ways: a fresh stack asks
    ``auth_token.ensure_token`` at first use (patched here), while a stack some
    earlier test module already built holds the value on the instance (where the
    patch cannot reach it, so it is overwritten).  An unbuilt stack is the normal
    case and needs no instance work at all.
    """
    original = auth_token.ensure_token

    def _ensure_token(path=None):
        return TOKEN

    auth_token.ensure_token = _ensure_token
    try:
        for middleware in _boundary_instances():
            middleware._token = TOKEN
            middleware._token_error = None
        yield
    finally:
        auth_token.ensure_token = original


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app, client=LOCAL_PEER)


def _bearer(value=None):
    return {"authorization": f"Bearer {TOKEN if value is None else value}"}


def _cookie(value=None):
    credential = auth_boundary.session_credential(TOKEN) if value is None else value
    return {"cookie": f"{auth_boundary.COOKIE_NAME}={credential}"}


def _same_site(extra=None):
    headers = {"origin": SAME_SITE_ORIGIN}
    headers.update(extra or {})
    return headers


# ── the answer is correct on each ingress path ──────────────────────────────


def test_a_caller_with_no_credential_gets_401_and_a_false_body(client):
    """401 from the route, not a refusal from the boundary.

    The body is the discriminator: every other protected path answers a refused
    caller with ``{"error": "unauthorized"}``, so ``{"authenticated": False}``
    can only have been produced by this route -- which is exactly the distinction
    the frontend needs.
    """
    response = client.get("/api/auth/session", headers=_same_site())

    assert response.status_code == 401, response.text
    assert response.json() == {"authenticated": False}


def test_a_bearer_token_is_accepted_and_reported_as_bearer(client):
    response = client.get("/api/auth/session", headers=_same_site(_bearer()))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "authenticated": True,
        "credential": auth_boundary.CREDENTIAL_BEARER,
    }


def test_a_session_cookie_is_accepted_and_reported_as_cookie(client):
    response = client.get("/api/auth/session", headers=_same_site(_cookie()))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "authenticated": True,
        "credential": auth_boundary.CREDENTIAL_COOKIE,
    }


def test_the_cookie_the_app_shell_mints_authenticates_this_route():
    """The product's own flow, end to end: the shell hands out the credential.

    This gets its own client because httpx keeps a cookie jar per client, and the
    shared one must stay anonymous for the tests that assert a refusal -- a
    session cookie landing in it would silently credential them. The redirect
    target is not followed: ``GET /`` in dev mode answers 307 to the Vite dev
    server, and following it would leave the process under test.
    """
    shell_client = TestClient(server.app, client=LOCAL_PEER)
    shell = shell_client.get("/", headers=_same_site(), follow_redirects=False)
    assert shell.status_code in (200, 307), shell.text
    assert auth_boundary.COOKIE_NAME in shell.cookies, shell.headers.get("set-cookie")

    minted = shell.cookies[auth_boundary.COOKIE_NAME]
    assert minted != TOKEN, "the shell must hand out the derived value, not the secret"
    assert auth_boundary.session_credential_ok(minted, TOKEN)

    response = shell_client.get("/api/auth/session", headers=_same_site(_cookie(minted)))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "authenticated": True,
        "credential": auth_boundary.CREDENTIAL_COOKIE,
    }


def test_a_wrong_value_on_both_channels_is_refused(client):
    headers = _same_site(
        {
            "authorization": f"Bearer {'ff' * 32}",
            "cookie": f"{auth_boundary.COOKIE_NAME}=1.deadbeef",
        }
    )
    response = client.get("/api/auth/session", headers=headers)

    assert response.status_code == 401, response.text
    assert response.json() == {"authenticated": False}


def test_a_cookie_derived_from_another_token_is_refused(client):
    """Rotation must end the session: the cookie is signed, not just shaped."""
    foreign = auth_boundary.session_credential("ff" * 32)
    response = client.get("/api/auth/session", headers=_same_site(_cookie(foreign)))

    assert response.status_code == 401, response.text
    assert response.json() == {"authenticated": False}


def test_a_valid_bearer_beside_a_stale_cookie_still_authenticates(client):
    """The two channels are independent, and the route must ask both.

    A browser that kept an old cookie and a script that sends the current token
    are the same request here; answering "not authenticated" because the *first*
    candidate failed would make the answer depend on which header the caller
    happened to send.
    """
    headers = _same_site(
        {**_bearer(), "cookie": f"{auth_boundary.COOKIE_NAME}=1.deadbeef"}
    )
    response = client.get("/api/auth/session", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "authenticated": True,
        "credential": auth_boundary.CREDENTIAL_BEARER,
    }


def test_a_credentialed_script_needs_no_origin_header(client):
    """A non-browser client sends no ``Origin``; the credential is the decision.

    Pinned here because this route's *result* is what tells such a client whether
    to retry, so it must be reachable exactly the way ``curl`` reaches it.
    """
    response = client.get("/api/auth/session", headers=_bearer())

    assert response.status_code == 200, response.text
    assert response.json()["authenticated"] is True


# ── the answer is narrow ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-credential"),
        pytest.param(
            {"authorization": "Bearer not-the-token", "cookie": "tm_web_ui_session=0.x"},
            id="wrong-credential",
        ),
        pytest.param({"authorization": f"Bearer {'3a' * 32}"}, id="right-credential"),
    ],
)
def test_the_answer_never_echoes_the_secret_or_the_store_shape(client, headers):
    """Whatever the verdict, the body is a verdict -- not a hint.

    An endpoint reachable without a credential is the last place that may report
    the token, its length, the file it lives in or why a candidate failed.
    """
    response = client.get("/api/auth/session", headers=_same_site(headers))
    text = response.text

    assert TOKEN not in text
    assert set(response.json()) <= {"authenticated", "credential"}
    assert "web_ui_token" not in text
    assert str(auth_token.token_path()) not in text
    assert ".thoughtmachine" not in text


def test_the_refusal_does_not_advertise_a_bearer_realm(client):
    """The boundary's refusal carries ``WWW-Authenticate``; this route's does not.

    A printed scheme invites a browser credential prompt for a secret that is
    never typed by a human, and the frontend reads the JSON instead.
    """
    response = client.get("/api/auth/session", headers=_same_site())

    assert "www-authenticate" not in {k.lower() for k in response.headers}


# ── the exempt neighbours stay narrow ──────────────────────────────────────


@pytest.mark.parametrize("extra", [{}, {"authorization": f"Bearer {'3a' * 32}"}])
def test_api_health_is_exactly_the_liveness_bit(client, extra):
    """``/api/health`` is exempt, so its payload is what an anonymous caller sees.

    One field: ``{"status": "ok"}``.  Not the ``/health`` payload it used to
    mirror -- that also carries the build revision, which is deployment detail
    rather than liveness.
    """
    response = client.get("/api/health", headers=_same_site(extra))

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok"}


def test_the_probe_route_keeps_the_payload_it_always_had(client):
    """The narrowing is the *alias*'s, deliberately, not ``/health``'s."""
    response = client.get("/health", headers=_same_site())
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["status"] == "ok"
    assert body["service"] == "thoughtmachine-web-ui"
    assert "revision" in body


def test_the_system_health_counts_are_not_exempt():
    """Worker/session counts are operator state, not a liveness bit.

    Pinned as a fact about the exemption set as well as a response, because the
    interesting failure is a later "make the probes consistent" edit that puts
    ``/api/system/health`` back.
    """
    assert "/api/system/health" not in auth_boundary.EXEMPT_PATHS
    assert not auth_boundary.is_exempt("/api/system/health")
    assert auth_boundary.is_exempt("/api/health")


def test_an_anonymous_caller_cannot_read_the_system_health_counts(client):
    response = client.get("/api/system/health", headers=_same_site())

    assert response.status_code == 401, response.text
    assert "running_workers" not in response.text
    assert "active_sessions" not in response.text
