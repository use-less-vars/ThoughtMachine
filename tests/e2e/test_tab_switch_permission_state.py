"""
E2E regression test: tab switching and the disk-pure permission-draft UX.

Disk-pure UX contract under test
--------------------------------
* ConfigPanel permission edits are TAB-LOCAL drafts.  App.jsx mounts ONLY
  the active SessionTab (``key={activeTab.sessionId}``), so switching tabs
  unmounts the previous tab's ConfigPanel and destroys its local draft
  state.
* An edit that is never Apply'd is therefore DROPPED on a tab round-trip:
  after A -> B -> A the Permissions tab re-initializes from the last
  backend config, so the select reverts to its ORIGINAL value and no
  unsaved-change banner is shown.
* An edit that IS Apply'd is persisted to the session file (session
  WebSocket ``apply_config`` -> ``merge_session_permissions``) and survives
  the round-trip: after A -> B -> A the select still shows the applied
  value.

Scenario
--------
Phase 1 - unsaved draft is dropped:
  1. Open session A, set Network permission from its original value to
     ``ask`` (within the research-workspace ceiling) - WITHOUT pressing
     Apply.  The specific banner "Unsaved permission changes" shows
     (count 1); the generic "Unsaved changes" banner does not (count 0).
  2. Switch to session tab B, then switch back to tab A.
  3. Re-open the Permissions tab: the Network select shows the ORIGINAL
     value again and both banners are absent (count 0 each).

Phase 2 - Apply'd change survives:
  4. Select ``ask`` again and press Apply; the applied-config echo clears
     the draft (both banners count 0) and the select re-seeds to ``ask``.
  5. Switch A -> B -> A and re-open Permissions: the select still shows
     ``ask`` and both banners stay at count 0.

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


def test_tab_switch_permission_state(page, e2e_frontend, e2e_backend, workspace):
    import httpx

    base_url = e2e_frontend["base_url"]
    ws_id = workspace["ws_id"]
    backend_url = e2e_backend["base_url"]

    # ── Create two sessions via the backend REST API ─────────────────────
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

    # ── WebSocket frame capture (config_changed / session_loaded) ────────
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

    # ── REST snapshots for evidence classification ───────────────────────
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

    # ── Open session A, Permissions tab ──────────────────────────────────
    page.goto(f"{base_url}/#/workspace/{ws_id}/session/{sid_a}")
    _wait_config_panel_ready(page)

    page.get_by_role("button", name="Permissions", exact=True).click()

    network_select = _network_select(page)
    expect(network_select).to_be_visible(timeout=10000)
    original = network_select.input_value()
    print(f"network value before change: {original}", flush=True)
    if original == APPLIED_NETWORK:
        pytest.fail(f"network already at {APPLIED_NETWORK}; cannot exercise a change")

    # ══ Phase 1: an UNSAVED draft is tab-local and is dropped on unmount ══
    network_select.select_option(APPLIED_NETWORK)
    expect(network_select).to_have_value(APPLIED_NETWORK)  # dirty draft

    # Do NOT press Apply.  The permission edit shows the specific banner;
    # the generic "Unsaved changes" banner must NOT appear for it.
    expect(page.get_by_text("Unsaved permission changes")).to_have_count(1, timeout=5000)
    expect(page.get_by_text("Unsaved changes")).to_have_count(0, timeout=5000)

    _ui_value("phase1-edit-no-apply", network_select)
    _rest_snapshot("phase1-edit-no-apply")

    # ── Tab round-trip: A → B → A ────────────────────────────────────────
    _switch_tab(page, SESSION_B_NAME)
    _wait_config_panel_ready(page)

    _switch_tab(page, SESSION_A_NAME)
    _wait_config_panel_ready(page)

    # ── Re-open Permissions: the un-Apply'd draft must be gone ───────────
    page.get_by_role("button", name="Permissions", exact=True).click()

    network_select = _network_select(page)
    expect(network_select).to_be_visible(timeout=10000)
    _ui_value("phase1-after-roundtrip", network_select)
    _rest_snapshot("phase1-after-roundtrip")

    # Remount re-initializes from the backend config: ORIGINAL value, no
    # draft, no banner (generic or specific).
    expect(network_select).to_have_value(original, timeout=15000)
    expect(page.get_by_text("Unsaved permission changes")).to_have_count(0, timeout=5000)
    expect(page.get_by_text("Unsaved changes")).to_have_count(0, timeout=5000)

    # ══ Phase 2: an Apply'd change IS persisted and survives the round-trip ══
    network_select.select_option(APPLIED_NETWORK)
    expect(network_select).to_have_value(APPLIED_NETWORK)  # dirty draft

    page.get_by_role("button", name="Apply", exact=True).click()

    # Applied-config echo clears the draft; select re-seeds to ask.
    expect(page.get_by_text("Unsaved permission changes")).to_have_count(0, timeout=15000)
    expect(page.get_by_text("Unsaved changes")).to_have_count(0, timeout=5000)
    expect(network_select).to_have_value(APPLIED_NETWORK, timeout=15000)

    _ui_value("phase2-after-apply", network_select)
    _rest_snapshot("phase2-after-apply")

    # ── Tab round-trip: A → B → A ────────────────────────────────────────
    _switch_tab(page, SESSION_B_NAME)
    _wait_config_panel_ready(page)

    _switch_tab(page, SESSION_A_NAME)
    _wait_config_panel_ready(page)

    # ── Re-open Permissions: the Apply'd value must survive ──────────────
    page.get_by_role("button", name="Permissions", exact=True).click()

    network_select = _network_select(page)
    expect(network_select).to_be_visible(timeout=10000)
    _ui_value("phase2-after-roundtrip", network_select)
    _rest_snapshot("phase2-after-roundtrip")

    expect(network_select).to_have_value(APPLIED_NETWORK, timeout=15000)
    expect(page.get_by_text("Unsaved permission changes")).to_have_count(0, timeout=5000)
    expect(page.get_by_text("Unsaved changes")).to_have_count(0, timeout=5000)

    # ── Dump captured WS frames (config_changed / session_loaded) ────────
    print(f"--- WS frames captured: {len(ws_frames)} ---", flush=True)
    for url, txt in ws_frames:
        if sid_a in txt or "load_session" in txt:
            print(f"[WS {url}] {txt[:1200]}", flush=True)
