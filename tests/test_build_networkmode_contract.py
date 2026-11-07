"""Collection-time contract guard for docker-py's build network kwarg.

``docker_executor._run_image_build`` forwards a build's network restriction to
docker-py as ``network_mode`` (underscore); docker-py translates it internally
to the ``networkmode`` query parameter.  This module asserts that the installed
docker-py still exposes a ``network_mode`` parameter on
``docker.api.build.BuildApiMixin.build``, so a future docker-py rename is
caught at collection time.

This is deliberately a PLAIN failing assert (NOT a skip) when docker is missing
or the signature changes: docker is a hard requirement (requirements.txt) and a
missing/renamed signature is exactly the breakage this guard exists to catch.

It is a fast, daemon-free guard.  It is NOT a substitute for the live-fire
regression test in tests/docker/test_build_network_isolation.py, which drives
the real daemon end to end.
"""

import inspect

from docker.api.build import BuildApiMixin


def test_build_api_accepts_network_mode_kwarg():
    params = inspect.signature(BuildApiMixin.build).parameters
    assert "network_mode" in params, (
        "docker-py BuildApiMixin.build no longer exposes a 'network_mode' "
        "parameter (got: %s); _run_image_build would fail" % sorted(params)
    )
