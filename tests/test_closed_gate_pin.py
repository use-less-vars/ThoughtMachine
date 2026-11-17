"""Pin: the closed gate refuses an untrusted in-process ``TestClient``.

Why this test exists
--------------------
``auth_boundary.TRUST_TEST_HARNESS`` is what lets a ``TestClient`` peer
(``testclient``) past the credential boundary.  The pre-existing suite relies on
it: it drives the real app unauthenticated, so the root ``conftest.py`` turns the
flag on for the whole session.  Flipping that session value off made 42 tests
fail, and 170 ``TestClient(`` construction sites will each have to say what they
meant by it before any of them can be migrated.

Before that migration starts, the *negative* direction has to be pinned against
the object the migration will use: an uncredentialed ``TestClient`` must be
refused while the allowance is off, and accepted while it is on.  Both halves
are in one test on purpose -- a refusal on its own would also pass for a
boundary whose flag does nothing at all.

The endpoint is ``/api/vault/status``: a real, non-exempt ``GET`` route on the
full app, the same one ``web_ui/backend/tests/test_auth_boundary.py`` uses for
this pair of answers.  The status codes are read from that module rather than
guessed -- 401 closed (``test_protected_route_without_a_credential_is_refused``),
200 open (``test_the_harness_peer_is_allowed_when_the_flag_is_on``).

The test drives the flag locally, so it does not depend on the session default
it is meant to outlive.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from fastapi.testclient import TestClient

from web_ui.backend import auth_boundary, server

#: A real, non-exempt ``GET`` route on the *full* app: it answers 200 to an
#: uncredentialed in-process harness peer while the allowance is in force and
#: 401 while it is not, so it measures the boundary and nothing else.
_PROTECTED_PATH = "/api/vault/status"

#: What the boundary answers an uncredentialed caller.  Pinned by the existing
#: suite (``test_protected_route_without_a_credential_is_refused``) rather than
#: chosen here: the number is the closed gate's contract.
_CLOSED_GATE_STATUS = 401


@contextlib.contextmanager
def _harness_trust(enabled: bool) -> Iterator[None]:
    """Run the block with ``auth_boundary.TRUST_TEST_HARNESS`` set as given.

    A copy of the helper in ``web_ui/backend/tests/test_auth_boundary.py``
    rather than an import of that module: importing one test module from
    another would make this file depend on the other test directory being a
    package and on that module's own collection-time work, for six lines.
    Whatever was in force is put back, so a test that needs the shipped default
    cannot leak that state into its neighbours.
    """
    previous = auth_boundary.TRUST_TEST_HARNESS
    auth_boundary.enable_test_harness_trust(enabled)
    try:
        yield
    finally:
        auth_boundary.enable_test_harness_trust(previous)


def test_closed_gate_rejects_untrusted_testclient() -> None:
    """401 with the allowance off, 200 with it on: one request, one client."""
    harness = TestClient(server.app)  # default peer == ("testclient", 50000)

    with _harness_trust(False):
        refused = harness.get(_PROTECTED_PATH)
    assert refused.status_code == _CLOSED_GATE_STATUS, refused.text

    with _harness_trust(True):
        allowed = harness.get(_PROTECTED_PATH)
    assert allowed.status_code == 200, allowed.text
