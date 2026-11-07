"""Tests for ``origin_policy._origin_check_request`` (Deliverable 1).

No app boot, no ``TestClient``, no network: requests are built directly from
a Starlette scope dict, so the helper is exercised at its own seam.

The log lines are load-bearing, not decoration.  The absent-Origin allow
branch is an assumption about the *client*, so every branch must leave a
greppable record.  The assertions below use exact full-message equality, so a
dropped or reworded log line fails the test instead of passing silently.
"""

import logging

from starlette.requests import Request

from web_ui.backend import origin_policy

LOGGER_NAME = "web_ui.backend.origin_policy"


def make_request(origin=None, referer=None, method="POST", path="/api/x"):
    """Build a Starlette ``Request`` from a scope dict (no server, no app)."""
    headers = []
    if origin is not None:
        headers.append((b"origin", origin.encode("latin-1")))
    if referer is not None:
        headers.append((b"referer", referer.encode("latin-1")))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "scheme": "http",
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 50000),
    }
    return Request(scope)


def log_lines(caplog):
    """Only this module's records -- exactly one per call."""
    return [r for r in caplog.records if r.name == LOGGER_NAME]


# ---------------------------------------------------------------------------
# Branch 1 -- absent Origin AND absent Referer -> allow + log
# ---------------------------------------------------------------------------

def test_absent_origin_allows_and_logs_absent_branch(caplog):
    request = make_request()

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        result = origin_policy._origin_check_request(request, "d1_probe")

    assert result is True
    records = log_lines(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].getMessage() == (
        "origin_check: route=d1_probe result=allow_absent origin=<absent>"
    )


# ---------------------------------------------------------------------------
# Branch 2 -- present-and-same -> allow + log
# ---------------------------------------------------------------------------

def test_loopback_origin_allows_and_logs_same_branch(caplog):
    origin = origin_policy.allowed_origins()[0]
    assert origin_policy.origin_is_loopback(origin) is True
    request = make_request(origin=origin)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        result = origin_policy._origin_check_request(request, "d1_probe")

    assert result is True
    records = log_lines(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].getMessage() == (
        "origin_check: route=d1_probe result=allow_same origin=" + origin
    )


# ---------------------------------------------------------------------------
# Branch 3 -- present-and-foreign -> refuse + log
# ---------------------------------------------------------------------------

def test_foreign_origin_refuses_and_logs_foreign_branch(caplog):
    request = make_request(origin="http://evil.example")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        result = origin_policy._origin_check_request(request, "d1_probe")

    assert result is False
    records = log_lines(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].getMessage() == (
        "origin_check: route=d1_probe result=refuse_foreign "
        "origin=http://evil.example"
    )


# ---------------------------------------------------------------------------
# Referer fallback -- scheme+host only, path stripped
# ---------------------------------------------------------------------------

def test_referer_without_origin_is_reduced_to_scheme_and_host(caplog):
    origin = origin_policy.allowed_origins()[0]
    referer = origin + "/some/path"
    request = make_request(referer=referer)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        result = origin_policy._origin_check_request(request, "d1_probe")

    assert result is True
    records = log_lines(caplog)
    assert len(records) == 1
    assert records[0].getMessage() == (
        "origin_check: route=d1_probe result=allow_same origin=" + origin
    )


# ---------------------------------------------------------------------------
# Shape of the accept-path: branch 2 IS origin_is_loopback, nothing else
# ---------------------------------------------------------------------------

def test_accepted_set_is_exactly_the_loopback_set():
    """State the unreachable case explicitly.

    Branch 2 is literally ``origin_is_loopback(raw)`` -- there is no second,
    wider accept-path and no consult of ``allowed_origins()``.  Consequence:
    an origin that is in ``allowed_origins()`` but NOT loopback is
    UNREACHABLE (no branch could accept it).  Today the two sets coincide,
    because ``allowed_origins()`` is built from loopback hosts, so this is a
    fact about the shape rather than a live gap -- and it is the reason the
    brief rules that ``_origin_check_request`` mirrors
    ``websocket_origin_allowed``'s three semantic branches and not a
    signature.
    """
    for origin in origin_policy.allowed_origins():
        assert origin_policy.origin_is_loopback(origin) is True
    assert origin_policy.origin_is_loopback("https://app.example.com") is False
