"""End-to-end browser test: workspace permission persistence across restarts.

Requires ``--run-e2e`` (see tests/e2e/README.md).  The test drives the real
UI (Vite dev server + FastAPI backend subprocess) with Playwright:

1. Open the workspace selector, enter the workspace, switch to the
   ``Permissions & Resources`` tab and set ``Filesystem`` to ``banned``.
2. Apply the change via the UI (PUT /api/workspace/{id}/permissions).
3. Restart the backend subprocess (same vault, same port).
4. Reload the UI and assert the persisted value is shown again.
"""

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.e2e


def _open_permissions_tab(page, base_url, ws_id):
    """Navigate to the workspace's Permissions & Resources tab."""
    page.goto(f"{base_url}/#/workspaces")
    # The project-root workspace is auto-registered on backend boot, so the
    # card list may contain more than our workspace: filter by ws id.
    card = page.locator(
        "article.ws-card",
        has=page.locator(".ws-card-desc", has_text=ws_id),
    )
    card.first.wait_for(state="visible", timeout=15000)
    card.first.click()
    page.get_by_role("tab", name="Permissions & Resources").click()


def test_permission_persistence_across_restart(
    page, e2e_frontend, e2e_backend, workspace, restart_backend
):
    base_url = e2e_frontend["base_url"]
    ws_id = workspace["ws_id"]

    _open_permissions_tab(page, base_url, ws_id)

    filesystem_card = page.locator(
        ".wdp-resource-card",
        has=page.locator(".wdp-resource-name", has_text="Filesystem"),
    )
    filesystem_card.first.wait_for(state="visible", timeout=15000)
    perm_select = filesystem_card.locator(".wdp-perm-select").first
    perm_select.select_option("banned")

    apply_btn = page.get_by_role("button", name="Apply Permissions")
    expect(apply_btn).to_be_enabled(timeout=15000)
    apply_btn.click()
    # The success banner is rendered both in the page header and in the
    # permissions tab's apply row (shared page-level applySuccess state);
    # scope to the tab's row to keep the locator unambiguous.
    expect(page.locator(".wdp-perm-apply-row .wdp-apply-success")).to_be_visible(timeout=15000)

    # Restart the backend process on the same port; the vault on disk must
    # retain the permission we just applied.
    restart_backend()

    # Fresh navigation simulates the user reloading the page.
    _open_permissions_tab(page, base_url, ws_id)

    persisted_select = (
        page.locator(
            ".wdp-resource-card",
            has=page.locator(".wdp-resource-name", has_text="Filesystem"),
        )
        .locator(".wdp-perm-select")
        .first
    )
    expect(persisted_select).to_have_value("banned", timeout=15000)
