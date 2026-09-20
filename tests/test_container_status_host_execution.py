"""Route-level host-fallback observability on container status.

Pins the Strand-A change: the workspace-scoped STATUS route

    GET /api/workspace/{workspace_id}/containers/{container_name}/status

gains an OPTIONAL ``"host_execution"`` object carrying a projection of the
LAST host-git fallback event recorded for the workspace:

    {"fallback": True, "reason": <reason>, "at": <iso8601 UTC>}

Contract (read-only, fail-closed):

* when the workspace vault JSONL
  ``<vault>/workspaces/<ws>/resources/git.host_execution.jsonl`` holds at
  least one recorded event, the key is present and mirrors the last event;
* when NO event is recorded -- or the store is missing / empty / malformed --
  the key is OMITTED entirely (absence != a fabricated ``{"fallback": false}``);
* surfacing the projection must NEVER turn a working status into a 5xx.

No docker daemon: a fake manager (``_make_container_manager`` is monkeypatched)
exposes ONLY ``list_containers()`` + ``status(...)``.  It deliberately has no
``.client``, so ``manager.client.containers.get(...)`` raises inside the
route's try/except, the container object is ``None`` and the hardening verdict
reads ``unverified``.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.server as server_module
from web_ui.backend.server import app

client = TestClient(app)

STATUS = "/api/workspace/{ws}/containers/{name}/status"

WS = "ws-test"


# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeManager:
    """Minimal stand-in exposing ONLY ``list_containers()`` + ``status()``.

    No ``.client`` attribute: the status route's container lookup raises,
    yielding ``container=None`` and an ``unverified`` hardening verdict.
    """

    def list_containers(self):
        return [{"name": "c1", "container_id": "id1"}]

    def status(self, container_id):
        return {"container_id": container_id, "name": "c1",
                "status": "running"}


@pytest.fixture
def status_vault(monkeypatch, tmp_path):
    """Point the vault at a tmp dir and install the fake container manager."""
    vault = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    manager = _FakeManager()
    monkeypatch.setattr(
        server_module, "_make_container_manager",
        lambda workspace_id, workspace_path="": manager)
    return vault


def _write_event(vault, at="2026-01-02T03:04:05Z", raw=None, append=False):
    """Write one host-fallback event line to the workspace vault JSONL.

    When ``raw`` is given it is written verbatim (used to inject malformed
    content).  ``append=True`` adds a further line instead of truncating.
    """
    path = vault / "workspaces" / WS / "resources" / "git.host_execution.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        text = raw
    else:
        entry = {
            "timestamp": at,
            "event_type": "host_execution",
            "actor": "git_read",
            "payload": {
                "fallback": True,
                "reason": "container_unavailable",
                "workspace_id": WS,
                "operation": "status",
                "kill_switch_state": "on",
                "detail": None,
            },
        }
        text = json.dumps(entry, sort_keys=True) + "\n"
    mode = "a" if append else "w"
    with open(path, mode, encoding="utf-8") as fh:
        fh.write(text)


# ── Tests ────────────────────────────────────────────────────────────────────


def test_key_present_when_event_recorded(status_vault):
    """A recorded event surfaces the key with the exact three-field shape."""
    _write_event(status_vault)
    resp = client.get(STATUS.format(ws=WS, name="c1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_execution"] == {
        "fallback": True,
        "reason": "container_unavailable",
        "at": "2026-01-02T03:04:05Z",
    }
    assert set(body["host_execution"]) == {"fallback", "reason", "at"}


def test_key_omitted_when_no_event(status_vault):
    """With no recorded event the projection key is OMITTED entirely."""
    resp = client.get(STATUS.format(ws=WS, name="c1"))
    assert resp.status_code == 200
    body = resp.json()
    assert "host_execution" not in body


def test_key_omitted_when_store_malformed(status_vault):
    """A fully-malformed store is skipped, not fatal -- key omitted, 200."""
    _write_event(status_vault, raw="{not json\n")
    resp = client.get(STATUS.format(ws=WS, name="c1"))
    assert resp.status_code == 200
    body = resp.json()
    assert "host_execution" not in body


def test_last_recorded_event_wins(status_vault):
    """The projection mirrors the LAST recorded event (append-only log)."""
    _write_event(status_vault, at="2026-01-02T03:04:05Z")
    _write_event(status_vault, at="2026-03-04T05:06:07Z", append=True)
    resp = client.get(STATUS.format(ws=WS, name="c1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_execution"]["at"] == "2026-03-04T05:06:07Z"


def test_other_status_fields_unchanged(status_vault):
    """The pre-existing status payload keys are preserved verbatim."""
    _write_event(status_vault)
    resp = client.get(STATUS.format(ws=WS, name="c1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "c1"
    assert body["status"] == "running"
    assert body["hardening"]["status"] == "unverified"
