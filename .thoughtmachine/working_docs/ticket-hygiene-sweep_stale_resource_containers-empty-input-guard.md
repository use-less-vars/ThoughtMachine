# Ticket: hygiene/sweep_stale_resource_containers-empty-input-guard

- **Title:** hygiene/sweep_stale_resource_containers-empty-input-guard
- **Finding:** `sweep_stale_resource_containers` (`infra/resource_container_manager.py:2032`) lacks an empty-input short-circuit (`registered_workspace_ids or []`); sibling sweeps correctly pass `None`.
- **Frame:** Latent defensive gap, not currently live. A caller that passes an empty registry set (instead of `None`) would sweep every resource container.
- **Action:** File only; do not fix.
- **Tag:** pass-2 evidence / hygiene (downgraded from E6 STOP-class after independent verification).
- **Source:** Pass 2 value-audit — E6 correction (`pass2-value-audit-20261008T211600Z.md`).
- **Checkout SHA:** 6fa9c1f224dbcbfe48bc03be601680bb2d6fcf98.
