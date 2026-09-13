"""Host-gated, real-daemon proof that the restart policy actually takes effect.

This is the end-to-end counterpart to the hermetic restart-policy tests in
``tests/test_container_restart_policy.py``: those prove the policy NAME is
threaded into ``containers.run(...)``; this proves the Docker daemon behaves
accordingly -- a container created with ``restart_policy={"Name": "always"}``
is restarted by the daemon after its main process is killed.

It talks to a REAL Docker daemon and therefore SKIPS CLEANLY when:
  * the ``docker`` SDK is not importable (``pytest.importorskip``), or
  * the daemon is unreachable (``client.ping()`` raises -> ``pytest.skip``), or
  * the ``alpine`` image is unavailable and cannot be pulled.

It NEVER fails merely because no daemon is present -- that is the normal CI
sandbox state.  The container machinery is deliberately not imported here (see
the note in ``tests/docker/test_container_lifecycle.py`` about the
``infra.container_manager`` circular-import cascade at collection time).
"""

import time

import pytest

docker = pytest.importorskip("docker")

_IMAGE = "alpine:latest"


def _client():
    """Return a pinged Docker client, or skip when unreachable."""
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:  # daemon absent / socket unreachable
        pytest.skip(f"Docker daemon unreachable: {exc}")
    return client


def _ensure_image(client):
    """Make the test image available, or skip when it cannot be obtained."""
    try:
        client.images.get(_IMAGE)
        return
    except docker.errors.ImageNotFound:
        pass
    except Exception as exc:  # pragma: no cover - daemon flakiness
        pytest.skip(f"could not inspect image {_IMAGE}: {exc}")
    try:
        client.images.pull(_IMAGE)
    except Exception as exc:
        pytest.skip(f"could not pull image {_IMAGE}: {exc}")


def test_restart_policy_always_increments_restart_count():
    client = _client()
    _ensure_image(client)

    container = None
    try:
        container = client.containers.run(
            _IMAGE,
            ["sleep", "3600"],
            detach=True,
            restart_policy={"Name": "always", "MaximumRetryCount": 0},
        )

        container.reload()
        assert container.attrs["HostConfig"]["RestartPolicy"]["Name"] == "always"
        assert container.attrs["State"]["RestartCount"] == 0

        # SIGKILL the main process; the daemon should restart it under the policy.
        container.kill()

        restarted = False
        for _ in range(50):
            time.sleep(0.2)
            container.reload()
            if container.attrs["State"].get("RestartCount", 0) >= 1:
                restarted = True
                break

        assert restarted, (
            "container was not restarted by the daemon despite restart_policy=always"
        )
        assert container.attrs["State"]["RestartCount"] >= 1
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass
