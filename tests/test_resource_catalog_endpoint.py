"""Tests for GET /api/resource-catalog — bare resource catalog array.

Hermetic: the endpoint reads the repo-relative resource catalog JSON
(``agent/config/resource_catalog.json``) and returns it as a bare array;
no vault or runtime state is touched, so the fresh server module (temp HOME
+ prefix purge) is the only hermetic machinery needed.
"""

import sys

import pytest
from starlette.testclient import TestClient

_EXPECTED_NAMES = {"git", "filesystem", "container", "host_bash", "tty", "jtag"}
_EXPECTED_KEYS = {
    "name",
    "display_name",
    "description",
    "permission_grain_set",
    "execution_mode",
    "container_image",
    "dockerfile_reference",
    "tools",
}


@pytest.fixture(scope="module")
def server_module():
    """Fresh import of web_ui.backend.server (temp HOME + prefix purge)."""
    import os
    import tempfile

    prefixes = (
        "web_ui.backend",
        "agent.config.provider_profile",
        "thoughtmachine.bootstrap",
        "session",
    )
    _ABSENT = object()

    def _matched():
        return [
            n for n in list(sys.modules)
            if any(n == p or n.startswith(p + ".") for p in prefixes)
        ]

    with tempfile.TemporaryDirectory() as tmp_home:
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = tmp_home
        # Snapshot the pre-purge state (matched sys.modules entries + the
        # parent-package attributes the re-import rebinds) so teardown puts the
        # ORIGINAL singletons back instead of leaving purged modules behind.
        saved_modules = {n: sys.modules[n] for n in _matched()}
        saved_attrs: dict = {}
        for n in saved_modules:
            parent_name, _, child = n.rpartition(".")
            if not parent_name or parent_name in saved_modules:
                continue
            parent_mod = sys.modules.get(parent_name)
            if parent_mod is not None:
                saved_attrs.setdefault(parent_name, {})[child] = parent_mod.__dict__.get(
                    child, _ABSENT
                )
        try:
            for name in saved_modules:
                parent_name, _, leaf = name.rpartition(".")
                parent_pkg = sys.modules.get(parent_name)
                del sys.modules[name]
                if parent_pkg is not None and leaf:
                    try:
                        delattr(parent_pkg, leaf)
                    except AttributeError:
                        pass
            import web_ui.backend.server as server_mod

            yield server_mod
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
            for name in _matched():
                del sys.modules[name]
            sys.modules.update(saved_modules)
            for parent_name, attrs in saved_attrs.items():
                parent_mod = sys.modules.get(parent_name)
                if parent_mod is None:
                    continue
                for child, value in attrs.items():
                    if value is _ABSENT:
                        parent_mod.__dict__.pop(child, None)
                    else:
                        parent_mod.__dict__[child] = value


@pytest.fixture(scope="module")
def client(server_module):
    with TestClient(server_module.app) as test_client:
        yield test_client


def test_resource_catalog_returns_bare_array(client):
    """The response body IS the array — no {items: [...]} wrapper."""
    resp = client.get("/api/resource-catalog")

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 6


def test_resource_catalog_six_resources_with_exact_keys(client):
    """Each entry carries exactly the 8 declared resource keys."""
    resp = client.get("/api/resource-catalog")

    assert resp.status_code == 200
    body = resp.json()
    assert {entry["name"] for entry in body} == _EXPECTED_NAMES
    for entry in body:
        assert set(entry.keys()) == _EXPECTED_KEYS, entry["name"]


def test_resource_catalog_git_entry_exact_fields(client):
    """The git resource pins the dockerfile reference, tools and defaults."""
    resp = client.get("/api/resource-catalog")

    assert resp.status_code == 200
    git = next(entry for entry in resp.json() if entry["name"] == "git")
    assert git["dockerfile_reference"] == "docker/resource/git_overlay.Dockerfile"
    assert git["tools"] == ["git_read", "git_write"]
    assert git["execution_mode"] == "container"
    assert git["permission_grain_set"] == ["banned", "read", "ask", "write"]
