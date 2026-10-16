"""Live-fire regression test for the build egress kwarg (bug/build-networkmode-kwarg).

``docker_executor._run_image_build`` forwards a build's network restriction to
docker-py as ``network_mode`` (underscore); docker-py translates it *internally*
to the ``networkmode`` query parameter, so the *public* kwarg is ``network_mode``
and never ``networkmode``.  A prior revision passed ``networkmode`` straight
through, which is an unsupported keyword argument -- every restricted build
raised ``TypeError`` before it ever reached the daemon.

Unlike ``tests/docker/test_build_networkmode.py`` (which mocks
``client.api.build`` / ``_run_image_build`` and therefore never touches
docker-py's real signature), this test drives a REAL ``docker.APIClient``
through ``_run_image_build`` against a real daemon.  Nothing is monkeypatched:
hitting docker-py's real signature is the entire point.

Asserted here:
  1. The build RAISES.
  2. The failure is a NETWORK failure (name resolution / connection / timeout).
  3. The failure is EXPLICITLY NOT a kwarg error -- the exact TypeError text
     "unexpected keyword argument" is absent.  This is the regression assertion
     for the bug being fixed.  The bare words "networkmode" / "TypeError" are
     deliberately NOT asserted absent: docker-py forwards the restriction to the
     daemon as the "networkmode" URL query parameter, so a healthy build's error
     text may legitimately contain "networkmode", and unrelated RUN-step
     tracebacks may mention TypeError.
  4. The build never reached package installation (no "Downloading", no
     "Successfully installed").

Caveat: ``_run_image_build`` hardcodes ``pull=True``.  If the base image is not
already cached locally the build may fail at the *pull* step instead of at the
``RUN`` step.  Either way the failure must be a NETWORK failure -- this test
accepts both, and a cached base exercises the intended RUN-time path.

Requires a real Docker daemon; self-skips via the module-local ``needs_docker``.
It is *additionally* marked ``@pytest.mark.docker`` so the fast lens
(``-m "not docker and not e2e"``) does not collect+run this live test on a
docker-capable host.
"""

from __future__ import annotations

import docker
import pytest

import infra.containers.executor as dex


# ---------------------------------------------------------------------------
# Docker-availability guard -- mirrors tests/docker/test_container_lifecycle.py.
# ---------------------------------------------------------------------------
_DOCKER_AVAILABLE: bool = False


def docker_available():
    global _DOCKER_AVAILABLE
    if _DOCKER_AVAILABLE:
        return True
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


# Base image chosen from existing repo evidence: ``python:3.11-slim`` is the
# base used by the sibling LIVE docker tests (tests/docker/test_persistence.py
# and tests/docker/test_container_lifecycle.py) and by live_smoke_docker.py, so
# a docker-capable host already depends on / caches it.
_BASE_IMAGE = "python:3.11-slim"

# A RUN step that makes a real outbound call; with network_mode="none" the
# daemon must fail it at name resolution / connect time.
_DOCKERFILE = (
    "FROM " + _BASE_IMAGE + "\n"
    'RUN python -c "import urllib.request; '
    "urllib.request.urlopen('https://pypi.org', timeout=5)\"\n"
)

_NETWORK_PATTERNS = (
    "name resolution",
    "name or service not known",
    "temporary failure",
    "could not resolve host",
    "connection refused",
    "network is unreachable",
    "timed out",
    "read timed out",
    "urlerror",
    "gaierror",
)

# Only the exact TypeError text produced by the OLD ("networkmode") spelling
# is forbidden. docker-py forwards the restriction to the daemon as the
# "networkmode" URL query parameter, so a real daemon's error text can
# legitimately contain the substring "networkmode"; likewise a bare "typeerror"
# can appear in unrelated RUN-step tracebacks. Either would flake the
# assertion on a healthy build, whereas "unexpected keyword argument" is the
# exact renamed-kwarg TypeError text and discriminates precisely.
_FORBIDDEN_KWARG_PATTERNS = ("unexpected keyword argument",)


@pytest.mark.docker
@needs_docker
class TestBuildNetworkIsolation:
    def test_restricted_build_fails_as_network_error_not_kwarg_error(self, tmp_path):
        (tmp_path / "Dockerfile").write_text(_DOCKERFILE, encoding="utf-8")

        collected: list[str] = []

        def _collect(line):
            collected.append(line)

        client = docker.from_env()
        # ``_run_image_build`` wraps docker-py's BuildError in a RuntimeError, and
        # the bug being fixed surfaces as a TypeError from the real docker-py
        # signature -- accept both so the regression assertion below is reached.
        with pytest.raises(
            (TypeError, RuntimeError, docker.errors.DockerException)
        ) as excinfo:
            dex._run_image_build(
                client,
                str(tmp_path),
                "Dockerfile",
                "tm-build-network-isolation:test",
                networkmode="none",
                on_line=_collect,
            )

        # The daemon's build log is embedded in the raised message (RuntimeError
        # carries the wrapped BuildError text); also fold in the on_line collector
        # and, if present, a ``build_log`` attribute.
        exc_text = str(excinfo.value)
        build_log = getattr(excinfo.value, "build_log", None)
        if build_log:
            exc_text += "\n" + "\n".join(str(x) for x in build_log)
        combined = (exc_text + "\n" + "\n".join(collected)).lower()

        # (3) REGRESSION: the failure is NOT the renamed-kwarg error.
        for forbidden in _FORBIDDEN_KWARG_PATTERNS:
            assert forbidden not in combined, (
                "build failed on an unsupported kwarg (%r), not on the network:\n%s"
                % (forbidden, combined)
            )

        # (2) the failure IS a network failure.
        assert any(p in combined for p in _NETWORK_PATTERNS), (
            "expected a network failure, got:\n" + combined
        )

        # (4) the build never reached package installation.
        assert "downloading" not in combined
        assert "successfully installed" not in combined
