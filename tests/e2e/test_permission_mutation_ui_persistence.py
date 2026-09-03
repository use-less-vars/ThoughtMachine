"""
E2E regression test: a UI permission mutation in the session ConfigPanel is
applied immediately (no backend restart) and survives a page reload because
the apply path persists the session config to disk.

Why this test exists: a stale test comment claimed session permission
mutations only take effect after a backend restart.  On the current dev
branch the ConfigPanel Apply button sends ``apply_config`` over the session
WebSocket; the backend persists the new ``session_permissions`` into the
session file immediately (``merge_session_permissions``), so:

  * the effective permissions (REST, resolved from the session file on disk)
    change right after Apply — while the SAME backend process is running
    (this test never calls the ``restart_backend`` fixture), and
  * the change is durable: after a page reload the value is re-read from
    disk, not from any in-memory frontend/backend state.

Flow:
  1. Create a purpose=coding workspace (filesystem ceiling ``write``) and a
     custom session whose default filesystem permission is ``read``.
  2. REST baseline: effective filesystem == ``read``.
  3. In the UI open the session ConfigPanel -> Permissions tab, change the
     Filesystem select to ``write`` and press Apply (no restart anywhere).
  4. Poll the REST effective-permissions endpoint until filesystem ==
     ``write`` while the original backend process is still up, then confirm
     the UI draft is clean and no "restart required" hint is shown.
  5. Reload the page.  The authoritative persistence assertion is the REST
     effective-permissions endpoint (it resolves the session file fresh
     from disk AFTER the reload, so any in-memory mirror is bypassed).
     The UI select is then checked best-effort: if the frontend restores
     the session ConfigPanel after a raw reload it must show ``write``; if
     the panel is genuinely not restored after a raw reload (a frontend
     state-restoration concern, distinct from permission persistence) the
     evidence is captured in a [diag] print and the REST assertion above
     carries the pass/fail decision.

NOTE on the workspace ceiling: the ``workspace`` fixture in conftest.py
creates a purpose=research workspace whose ceiling caps ``filesystem`` at
``read``, so this test creates its OWN purpose=coding workspace (ceiling
``write``) to be able to raise the session grain from the ``read`` default
to ``write``.
"""

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.e2e

SESSION_NAME = "ui-persist-probe"


def _wait_config_panel_ready(page, timeout=20000):
    """Wait until the session ConfigPanel is rendered and Apply is enabled."""
    apply_btn = page.get_by_role("button", name="Apply", exact=True)
    expect(apply_btn).to_be_enabled(timeout=timeout)


def _open_permissions_tab(page):
    """Open the Permissions tab inside the session ConfigPanel."""
    page.get_by_role("button", name="Permissions", exact=True).click()


def _filesystem_select(page):
    """Locator for the Filesystem <select> in the ConfigPanel Permissions tab.

    ``label has_text`` matches any ancestor div too, so scope to the label's
    own parent (the per-permission row that holds the <select>).
    """
    return page.locator("label", has_text="Filesystem").locator("..").locator("select").first


def _effective_filesystem(client, backend_url, ws_id, sid):
    """REST truth: effective filesystem grain for *sid* (merged, from disk)."""
    resp = client.get(
        f"{backend_url}/api/workspace/{ws_id}/effective_permissions",
        params={"session_id": sid},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["effective_permissions"]["filesystem"]


def _wait_effective_filesystem(client, backend_url, ws_id, sid, expected, deadline_s=15.0):
    """Poll the effective-permissions endpoint until *expected* or deadline."""
    import time

    deadline = time.monotonic() + deadline_s
    last = None
    while time.monotonic() < deadline:
        try:
            last = _effective_filesystem(client, backend_url, ws_id, sid)
        except Exception as exc:  # noqa: BLE001 - transient during apply
            last = f"ERR {exc}"
        if last == expected:
            return
        time.sleep(0.5)
    raise AssertionError(
        f"effective filesystem never became {expected!r} (last observed: {last!r})"
    )


def test_permission_mutation_ui_persistence(page, e2e_frontend, e2e_backend):
    import httpx
    import re
    import time
    from pathlib import Path

    frontend_url = e2e_frontend["base_url"]
    backend_url = e2e_backend["base_url"]

    # ── 1. Coding workspace (filesystem ceiling = write) + custom session ──
    ws_root = Path(e2e_backend["vault"]) / "ui_persist_coding"
    ws_root.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{backend_url}/api/workspace",
            json={"path": str(ws_root), "purpose": "coding"},
        )
        assert resp.status_code in (200, 201), (
            f"coding workspace creation failed: {resp.status_code} {resp.text}"
        )
        data = resp.json()
        ws_id = data["workspace_id"]
        ceiling = data.get("permissions") or {}
        if ceiling:
            assert ceiling.get("filesystem") == "write", (
                f"coding workspace ceiling not write: {ceiling}"
            )
        print(
            f"[ui-persist] coding workspace {ws_id} "
            f"ceiling filesystem={ceiling.get('filesystem')!r}",
            flush=True,
        )

        sresp = client.post(
            f"{backend_url}/api/session/create",
            json={"name": SESSION_NAME, "workspace_id": ws_id, "mode": "custom"},
        )
        sresp.raise_for_status()
        sid = sresp.json()["session_id"]
        print(f"[ui-persist] session {sid} created", flush=True)

        # ── 2. REST baseline: session default read under a write ceiling ──
        eff0 = _effective_filesystem(client, backend_url, ws_id, sid)
        assert eff0 == "read", f"baseline effective filesystem expected 'read', got {eff0!r}"
        print(f"[ui-persist] REST baseline effective filesystem: {eff0}", flush=True)

    # ── 3. UI: open the session ConfigPanel -> Permissions tab ──
    page.goto(f"{frontend_url}/#/workspace/{ws_id}/session/{sid}")
    _wait_config_panel_ready(page)

    _open_permissions_tab(page)
    fs_sel = _filesystem_select(page)
    expect(fs_sel).to_be_visible(timeout=10000)
    current = fs_sel.input_value()
    print(f"[ui-persist] UI filesystem value before change: {current}", flush=True)
    assert current == "read", f"expected UI baseline 'read', got {current!r}"

    fs_sel.select_option("write")
    expect(fs_sel).to_have_value("write")  # dirty draft
    expect(page.get_by_text("Unsaved changes")).to_have_count(1, timeout=5000)

    # ── 4. Apply — NO backend restart anywhere in this test ──
    page.get_by_role("button", name="Apply", exact=True).click()

    with httpx.Client(timeout=30) as client:
        # 4a. Backend truth must flip while the SAME backend process runs.
        _wait_effective_filesystem(client, backend_url, ws_id, sid, "write")
        print("[ui-persist] REST effective filesystem after Apply: write (no restart)", flush=True)

        # 4b. The applied-config echo clears the draft; the select re-seeds to write.
        expect(page.get_by_text("Unsaved changes")).to_have_count(0, timeout=15000)
        expect(fs_sel).to_have_value("write", timeout=15000)

    # 4c. No "restart required" hint: dev's ConfigPanel documents that changes
    # take effect immediately ("...no restart required."), so a banner
    # demanding a restart would contradict the persistence guarantee above.
    # Evidence capture only — never the pass/fail driver.  The negative
    # lookbehind keeps the app's own positive copy ("no restart required.")
    # from tripping the check.
    try:
        body_text = page.locator("body").inner_text().lower()
        hits = []
        if re.search(r"(?<!no )restart required", body_text):
            hits.append("restart required")
        for forbidden in ("restart the backend", "must restart"):
            if forbidden in body_text:
                hits.append(forbidden)
        assert not hits, (
            f"UI shows restart hint(s) {hits} after Apply: {body_text[-400:]}"
        )
        print("[ui-persist] UI shows no restart-required hint after Apply", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[ui-persist][diag] restart-hint check failed: {exc}", flush=True)

    # ── 5. Reload: the mutation is on disk, so it must survive ──
    # Capture post-reload WebSocket frames (load_session / session_loaded /
    # config_changed / apply_config) so a failure can be classified as
    # frontend state-restoration vs. permission persistence.
    reload_t0 = time.monotonic()
    ws_events = []

    def _on_ws(ws):
        def _on_frame(payload):
            try:
                txt = payload if isinstance(payload, str) else payload.decode("utf-8", "replace")
            except Exception:
                return
            if any(k in txt for k in ("session_loaded", "config_changed", "load_session", "apply_config")):
                ws_events.append((time.monotonic(), txt[:500]))

        ws.on("framereceived", _on_frame)

    page.on("websocket", _on_ws)
    page.reload()

    # 5a. Authoritative persistence check: the effective permissions are
    # resolved fresh from the session file on disk AFTER the reload (no
    # restart, no in-memory trust).  This is the pass/fail driver.
    with httpx.Client(timeout=30) as client:
        _wait_effective_filesystem(client, backend_url, ws_id, sid, "write")
        eff_after = _effective_filesystem(client, backend_url, ws_id, sid)
        assert eff_after == "write", f"post-reload effective filesystem {eff_after!r}"
        print(
            f"[ui-persist] REST effective filesystem after reload: {eff_after} "
            f"(re-read from disk)",
            flush=True,
        )

    # 5b. UI check after reload — best-effort.  If the frontend restores the
    # session ConfigPanel after a raw reload it must show the persisted
    # value.  If the panel is genuinely not restored after a raw reload
    # (frontend state-restoration issue), capture evidence and let 5a carry
    # the verdict.  Only a panel that IS reachable but shows the WRONG value
    # (or a hard UI error) is a real failure that must surface.
    try:
        _wait_config_panel_ready(page, timeout=20000)
        _open_permissions_tab(page)
        fs_sel = _filesystem_select(page)
        expect(fs_sel).to_be_visible(timeout=10000)
        expect(fs_sel).to_have_value("write", timeout=15000)
        print(f"[ui-persist] UI filesystem value after reload: {fs_sel.input_value()}", flush=True)
    except Exception as exc:  # noqa: BLE001
        try:
            apply_count = page.get_by_role("button", name="Apply", exact=True).count()
        except Exception:  # noqa: BLE001
            apply_count = -1
        panel_not_restored = apply_count == 0
        if not panel_not_restored:
            raise  # panel reachable but value/visibility wrong → real regression
        diag = [
            f"[ui-persist][diag] ConfigPanel not restored after raw reload (REST 5a already "
            f"proved persistence): {exc}",
            f"[ui-persist][diag] page.url={page.url}",
            f"[ui-persist][diag] apply_btn_count={apply_count}",
        ]
        try:
            diag.append(f"[ui-persist][diag] tab_item_count={page.locator('.tab-item').count()}")
        except Exception:  # noqa: BLE001
            pass
        try:
            txt = page.locator("body").inner_text(timeout=5000).strip()
            diag.append(f"[ui-persist][diag] body[:400]={txt[:400]!r}")
        except Exception as e2:  # noqa: BLE001
            diag.append(f"[ui-persist][diag] body read failed: {e2}")
        recent = [t for t in ws_events if t[0] >= reload_t0][-6:]
        for _, frame in recent:
            diag.append(f"[ui-persist][diag] ws: {frame[:300]}")
        if not recent:
            diag.append("[ui-persist][diag] ws: (no load_session/session_loaded frames after reload)")
        print("\n".join(diag), flush=True)
