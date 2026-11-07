# Ticket: bug/container-restart-does-not-reassert-resource-limits

**Filed:** Gen 47, 2026-10-20 at `124477b7`
**Source:** gen-47 container-ops run (§Step 6); code verified at `124477b7`. Replaces the dropped "operator restart resets confinement" S6 framing.
**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` §Step 6 (lines 118–143), specifically the P1 attribution at lines 140–142.
**Status:** filed, not dispatched.

## Defect

Restart is a stop plus a *reuse-start of the same container*: `infra/container_manager.py:3546-3599` builds `ContainerIntent(kind="restart", explicit=True, reason="explicit_restart")`, reconciles with `emit=False`, records the restart, stops the container, then calls `self.start(name=name)` — with **no** `allow_fresh`, **no** `created_by`, **no** `note` and **no** limits. It never recreates, and it never re-derives the container's resource envelope.

Resource limits are applied **only** on the fresh-create branch (`infra/container_manager.py:1626-1640`, `:1756-1767`). The reuse path never re-applies the record's `intent_snapshot`.

The reuse drift check therefore cannot see a resource divergence. `_start_drift_decision` (`infra/container_manager.py:1715-1740`) covers hardening/isolation only; `:281-288` covers restart policy only. Neither axis reads memory or CPU. Consequence: a container whose live `HostConfig` has diverged from the record's `intent_snapshot` restarts **silently drifted**, with no drift event and no refusal — the restart path is not a re-assertion point for the envelope.

The only path that rebuilds from record intent is an `allow_fresh` recreate: the panel `action == "recreate"` route (`web_ui/backend/server.py:4439`) or the resize route (`web_ui/backend/server.py:4746`, which destroys with `keep_record=True` at `:4919-4923` and re-creates with `allow_fresh=True` at `:4920-4924`). Restart uses neither.

Fix shape: add memory/CPU to the reuse drift decision, and then either re-apply limits on reuse or emit a drift event when the live limits differ from the recorded intent. Doing neither leaves the restart path permanently unable to tell "still conformant" from "drifted since creation".

## Bearing

**This ticket corrects the report's own attribution.** §Step 6 (lines 140–142) assigns the non-reproduction of the prior restart P1 to the *caller* — "was on the **operator/panel** restart path and does not evidence on the agent tool path". There is a single `restart()` implementation (`infra/container_manager.py:3546-3599`), and the panel restart route (`web_ui/backend/server.py:4504-4509` → `_record_user_action` `:4427-4444`) reaches the same funnel. The real discriminator is **state, not path**: whether the Docker object still existed at restart time. When it does, `_reuse_container` → `_ensure_running` → `container.start()` (`infra/container_manager.py:4543-4560`) keeps the immutable Docker object, so labels and `HostConfig` limits survive by construction, not by any restart-side preservation. When it does not, restart has no `allow_fresh` and cannot rebuild. *Caveat: the panel route's exact call target was not re-read for this ticket — the "same funnel" claim is by route shape, not by a line-level read of the handler body.*

Terminology note: "confinement" in this codebase names **HOST-PATH** confinement (`web_ui/backend/server.py:3053-3082`, `web_ui/backend/workspace_routes.py:70-97`, `web_ui/backend/global_routes.py:100`, covered by `tests/test_host_api_hardening.py`) — it is not a container-resource concept. The earlier "restart resets confinement" wording should not be reused for this defect; the defect is unasserted resource limits.

Blocks nothing. It is the reason a restart cannot be used as a conformance checkpoint after an out-of-band `docker update`, a host reboot with a changed default, or any drift between record intent and live `HostConfig`.
