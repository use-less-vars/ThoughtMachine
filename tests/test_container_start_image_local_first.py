"""Container start is LOCAL-FIRST: an absent image is refused, never pulled.

``docker-py``'s ``containers.run`` implicitly PULLS on ``ImageNotFound``, so a
create against an absent image would otherwise reach out to a registry instead
of failing.  ``ContainerManager._fresh_start`` probes the image first
(``images.get`` never pulls) and refuses with a coded, precise message.

These tests are hermetic: the Docker SDK surface is faked (no daemon), and the
fake's ``images.pull`` raises if it is ever touched, so a pull attempt fails
the test loudly rather than passing silently.
"""

from docker.errors import ImageNotFound

from agent.config.defaults import DEFAULT_IMAGE
from infra.container_manager import ContainerManager
from thoughtmachine.container_record import list_records


# ---------------------------------------------------------------------------
# Fakes (same shape as tests/test_container_record_hook.py)
# ---------------------------------------------------------------------------


class _FakeCtr:
    """Minimal stand-in for a docker container returned by containers.run."""

    def __init__(self, container_id="docker-1", name="ctr", labels=None, attrs=None):
        self.id = container_id
        self.name = name
        self.status = "created"
        self.labels = dict(labels or {})
        self.attrs = (
            attrs if attrs is not None else {"State": {"Status": "created"}}
        )
        self.stopped = []
        self.removed = []

    def start(self):
        self.status = "running"

    def stop(self, timeout=None):
        self.stopped.append(timeout)

    def remove(self, **kwargs):
        self.removed.append(kwargs)

    def reload(self):
        pass

    def exec_run(self, **kwargs):
        return (0, (b"", b""))


class _FakeContainers:
    def __init__(self, containers=None):
        self.containers = list(containers or [])
        self.run_calls = []
        self.list_calls = []

    def list(self, all=False, filters=None):
        self.list_calls.append({"all": all, "filters": filters})
        return list(self.containers)

    def get(self, container_id):
        for c in self.containers:
            if c.id == container_id or c.name == container_id:
                return c
        raise LookupError(container_id)

    def run(self, *args, **kwargs):
        self.run_calls.append({"args": args, "kwargs": kwargs})
        name = kwargs.get("name") or "run-ctr"
        ctr = _FakeCtr(
            "c-run-1", name=name, labels=kwargs.get("labels") or {}
        )
        self.containers.append(ctr)
        return ctr


class _FakeImages:
    """``client.images`` fake: records probes, NEVER tolerates a pull."""

    def __init__(self, present=True, get_error=None):
        self.present = present
        self.get_error = get_error
        self.get_calls = []
        self.pull_calls = []

    def get(self, image):
        self.get_calls.append(image)
        if self.get_error is not None:
            raise self.get_error
        if not self.present:
            raise ImageNotFound(f"no such image: {image}")
        return object()

    def pull(self, *args, **kwargs):
        self.pull_calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("pull must never be attempted")


class _FakeDockerClient:
    def __init__(self, images=None, containers=None):
        self.containers = _FakeContainers(containers or [])
        self.images = images if images is not None else _FakeImages()

    def ping(self):
        return True


def _make_container_manager(client=None, *, workspace_id="w1", session_id="s1"):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/ws-test"
    cm.session_id = session_id
    cm.workspace_id = workspace_id
    cm.session_permissions = {}
    cm._session_config = {"use_container_registry": False}
    cm.image = DEFAULT_IMAGE
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm._containers = {}
    cm.client = client if client is not None else _FakeDockerClient()
    cm._compute_config = lambda *a, **k: ("none", "ro")
    cm.vault_root = "/tmp/tm-vault-test"
    cm.container_notes = {}
    cm.max_containers = 4
    cm.workspace_config = {"max_containers": 4}
    cm.dockerfile_path = None
    return cm


# ---------------------------------------------------------------------------
# 1-3. Absent image => coded refusal, no pull, no create, no record
# ---------------------------------------------------------------------------


def test_absent_image_refused_with_precise_message():
    client = _FakeDockerClient(images=_FakeImages(present=False))
    cm = _make_container_manager(client, workspace_id="ws-lf-refuse")

    result = cm.start(image=DEFAULT_IMAGE, name="agent-lf-refuse")

    assert result["code"] == "image_not_present_locally"
    message = result["error"]
    assert "ContainerBuildTool" in message
    assert DEFAULT_IMAGE in message
    # The refusal is a dict, never a raised exception: nothing was created.
    assert client.containers.run_calls == []


def test_absent_image_never_pulls():
    client = _FakeDockerClient(images=_FakeImages(present=False))
    cm = _make_container_manager(client, workspace_id="ws-lf-nopull")

    cm.start(image=DEFAULT_IMAGE, name="agent-lf-nopull")

    assert client.images.pull_calls == []
    # The probe DID run (a refusal that never probed would be vacuous).
    assert client.images.get_calls == [DEFAULT_IMAGE]


def test_absent_image_mints_no_record():
    client = _FakeDockerClient(images=_FakeImages(present=False))
    cm = _make_container_manager(client, workspace_id="ws-lf-norecord")

    cm.start(image=DEFAULT_IMAGE, name="agent-lf-norecord")

    assert list_records("ws-lf-norecord") == []


# ---------------------------------------------------------------------------
# 4-5. Present image / unprobeable client => the create proceeds
# ---------------------------------------------------------------------------


def test_present_image_creates():
    client = _FakeDockerClient(images=_FakeImages(present=True))
    cm = _make_container_manager(client, workspace_id="ws-lf-present")

    result = cm.start(image=DEFAULT_IMAGE, name="agent-lf-present")

    assert result["status"] == "created"
    assert len(client.containers.run_calls) == 1
    assert client.images.pull_calls == []


def test_unprobeable_client_still_creates():
    client = _FakeDockerClient(
        images=_FakeImages(get_error=RuntimeError("daemon hiccup"))
    )
    cm = _make_container_manager(client, workspace_id="ws-lf-unprobeable")

    result = cm.start(image=DEFAULT_IMAGE, name="agent-lf-unprobeable")

    assert result["status"] == "created"
    assert len(client.containers.run_calls) == 1
