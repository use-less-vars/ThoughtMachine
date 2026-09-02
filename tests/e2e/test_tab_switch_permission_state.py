"""
E2E regression test: switching between session tabs must not lose a
permission edit made in the Permissions tab.

Scenario (bug: tab switch loses permission state):
  1. Open session A, set Network permission from default ``banned`` to
     ``ask`` (within the research-workspace ceiling) — WITHOUT pressing
     Apply, so the edit exists only as an unsaved draft (the "Unsaved
     changes" indicator is showing).
  2. Switch to session tab B, then switch back to tab A.
  3. Open the Permissions tab again: the Network select must still show
     ``ask`` and "Unsaved changes" must still be present.

Reproduced failure (current code): after the A -> B -> A round-trip the
select reverts to ``banned`` and "Unsaved changes" disappears. Root cause:
App.jsx mounts ONLY the active SessionTab (``key={activeTab.sessionId}``),
so switching tabs unmounts tab A's ConfigPanel and destroys its local
``draft`` state; on remount ConfigPanel's ``useEffect([config])`` re-
initializes the draft from the last backend config (``session_permissions``
is empty, so the UI falls back to the default ``banned``). The unsaved
edit never reaches the store or the backend, so nothing can restore it.

NOTE on the workspace ceiling: the conftest ``workspace`` fixture creates a
``purpose=research`` workspace whose permission ceiling (purpose preset)
caps ``filesystem`` at ``read`` and ``container`` at ``banned``, but
``network`` at ``ask``.  ``banned -> ask`` is therefore a real, persisted
permission change that survives the ceiling, which is why the test uses
Network rather than Filesystem.

Evidence capture: the test prints (1) the UI select value, (2) REST
snapshots (workspace permissions / summary / effective-permissions) and
(3) WebSocket ``config_changed`` / ``session_loaded`` frames for the
affected session, so a failure can be classified as frontend-only loss,
backend session loss, or apply-never-persisted.
"""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

SESSION_A_NAME = "perm-switch-a"
SESSION_B_NAME = "perm-switch-b"
APPLIED_NETWORK = "ask"  # default is 'banned'; research ceiling allows up to 'ask'


def _wait_config_panel_ready(page, timeout=20000):
    """Wait until the session ConfigPanel is rendered and Apply is enabled."""
    apply_btn = page.get_by_role("button", name="Apply", exact=True)
    expect(apply_btn).to_be_enabled(timeout=timeout)


def _switch_tab(page, name):
    """Click the session tab whose label span matches *name*."""
    tab = page.locator(".tab-item", has=page.locator(".tab-label", has_text=name)).first
    expect(tab).to_be_visible(timeout=10000)
    tab.click()


def _network_select(page):
    """Locator for the Network <select> inside the ConfigPanel Permissions tab.

    ``label has_text`` matches any ancestor div too, so scope to the label's
    own parent (the per-permission div that holds the <select>).
    """
    return page.locator("label", has_text="Network").locator("..").locator("select").first


def test_tab_switch_preserves_session_permissions(page, e2e_frontend, e2e_backend, workspace):
    import httpx

    base_url = e2e_frontend["base_url"]
    ws_id = workspace["ws_id"]
    backend_url = e2e_backend["base_url"]

    # ── Create two sessions via the backend REST API ──────────────────────
    with httpx.Client(timeout=30) as client:
        resp_a = client.post(
            f"{backend_url}/api/session/create",
            json={"name": SESSION_A_NAME, "workspace_id": ws_id, "mode": "custom"},
        )
        resp_a.raise_for_status()
        sid_a = resp_a.json()["session_id"]

        resp_b = client.post(
            f"{backend_url}/api/session/create",
            json={"name": SESSION_B_NAME, "workspace_id": ws_id, "mode": "custom"},
        )
        resp_b.raise_for_status()
        sid_b = resp_b.json()["session_id"]

    # ── WebSocket frame capture (config_changed / session_loaded) ─────────
    ws_frames = []

    def _on_ws(ws):
        def _on_frame(payload):
            try:
                txt = payload if isinstance(payload, str) else payload.decode("utf-8", "replace")
            except Exception:
                return
            if any(k in txt for k in ("config_changed", "session_loaded", "load_session")):
                ws_frames.append((ws.url, txt[:1500]))
        ws.on("framereceived", _on_frame)

    page.on("websocket", _on_ws)

    # ── REST snapshots for evidence classification ────────────────────────
    def _rest_snapshot(tag):
        lines = [f"--- REST snapshot [{tag}] ---"]
        for label, path, params in (
            ("permissions", f"/api/workspace/{ws_id}/permissions", None),
            ("summary", f"/api/workspace/{ws_id}/summary", None),
            ("effective_underscore", f"/api/workspace/{ws_id}/effective_permissions",
             {"session_id": sid_a}),
            ("effective_hyphen", f"/api/workspace/{ws_id}/effective-permissions",
             {"session_id": sid_a}),
        ):
            try:
                r = httpx.get(backend_url + path, params=params, timeout=10)
                lines.append(f"{label}: {r.status_code} {r.text[:400]}")
            except Exception as exc:
                lines.append(f"{label}: ERR {exc}")
        print("\n".join(lines), flush=True)

    def _ui_value(tag, select):
        try:
            print(f"UI select value [{tag}]: {select.input_value()}", flush=True)
        except Exception as exc:
            print(f"UI select value [{tag}]: ERR {exc}", flush=True)

    # ── Open session A and apply a Network permission change ──────────────
    page.goto(f"{base_url}/#/workspace/{ws_id}/session/{sid_a}")
    _wait_config_panel_ready(page)

    page.get_by_role("button", name="Permissions", exact=True).click()

    network_select = _network_select(page)
    expect(network_select).to_be_visible(timeout=10000)
    current = network_select.input_value()
    print(f"network value before change: {current}", flush=True)
    if current == APPLIED_NETWORK:
        pytest.fail(f"network already at {APPLIED_NETWORK}; cannot exercise a change")

    network_select.select_option(APPLIED_NETWORK)
    expect(network_select).to_have_value(APPLIED_NETWORK)  # dirty draft

    # UNSAVED-DRAFT variant: do NOT press Apply. The dirty edit (plus the
    # "Unsaved changes" indicator) is the permission state that must survive
    # the tab round-trip.
    expect(network_select).to_have_value(APPLIED_NETWORK)
    expect(page.get_by_text("Unsaved changes")).to_have_count(1, timeout=5000)

    _ui_value("after-edit-no-apply", network_select)
    _rest_snapshot("after-edit-no-apply")

    # ── Tab round-trip: A → B → A ─────────────────────────────────────────
    _switch_tab(page, SESSION_B_NAME)
    _wait_config_panel_ready(page)

    _switch_tab(page, SESSION_A_NAME)
    _wait_config_panel_ready(page)

    # ── Re-open the Permissions tab: the applied value must survive ───────
    page.get_by_role("button", name="Permissions", exact=True).click()

    network_select = _network_select(page)
    expect(network_select).to_be_visible(timeout=10000)
    _ui_value("after-tab-roundtrip", network_select)
    _rest_snapshot("after-tab-roundtrip")

    # ── Dump captured WS frames (config_changed / session_loaded) ─────────
    print(f"--- WS frames captured: {len(ws_frames)} ---", flush=True)
    for url, txt in ws_frames:
        if sid_a in txt or "load_session" in txt:
            print(f"[WS {url}] {txt[:1200]}", flush=True)

    # If the ConfigPanel remounts on tab reactivation (or the draft is
    # re-initialized from a stale config prop), the unsaved edit is lost here.
    expect(page.get_by_text("Unsaved changes")).to_have_count(1, timeout=5000)

    expect(network_select).to_have_value(APPLIED_NETWORK, timeout=15000)
