# Ticket: bug/resource-overlay-not-rebuilt-with-runtime

**Filed:** 2026-10-20
**Source:** gen-47 container-ops run — dispatched finding F15 (orchestrator summary), grounded in code at HEAD.
**Severity:** medium — silent half-applied image state; produces "the toolchain is missing" reports that are false.
**Status:** filed, not dispatched.
**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` — read in full (384 lines); `## Findings` (L318) has **F1–F10 only**, no Appendix A/B, no F11–F22. F15 has **no citable report line numbers**; the code evidence below was read directly from the tree.

## Defect

Resource images are two-stage: a runtime base plus a resource overlay.

- `infra/resource_container_manager.py:87` — "Resource image identity. The `tm-resource-git` resource image is layered on [the runtime]"
- `:272` — the overlay covers BOTH the runtime base and its own layer
- `:551-565` — "Stage 2 — `tm-resource-git`: the git resource overlay, built from …"; a legacy *unified* `tm-resource-git` is identified by being labeled with only the runtime
- `:881` — the build command: `docker build -t tm-resource-git -f ~/.thoughtmachine/docker/resource/git_overlay.Dockerfile --build-arg BASE_IMAGE=tm-workspace-runtime:latest ~/.thoughtmachine/docker/resource`

So the overlay pins `BASE_IMAGE=tm-workspace-runtime:latest` at **its own build time**. Rebuilding `agent-executor` with new base contents (e.g. a compiler toolchain) does not rebuild `tm-resource-git`; the git resource keeps serving the older base. Nothing surfaces the divergence: no tool rebuilds resource images, and no status surface reports "overlay is stale relative to its base".

The operator-visible symptom is a half-applied state: the compiler is present in executor containers and absent in resource containers, with the configuration that was edited being identical in both cases. This is the same class as the report's F9/F10 evidence-gathering gaps — the system cannot be asked whether the state it is in is internally consistent.

## Fix shape (not written)

Make the overlay's staleness *computable and visible*, then rebuild it: derive the overlay identity from (overlay Dockerfile hash + base image id) rather than a `latest` tag reference, so a base rebuild invalidates the overlay label; expose the pair in the container-status surface; and give the existing build path the ability to rebuild the overlay when the base moved (single call, both stages, so the two can never drift by omission).

## Bearing

Medium-high operational: every image-content change silently splits the fleet. Overlaps `ticket-feat-executor-image-provisioning.md` (image provisioning as a first-class flow) and `ticket-hygiene-legacy-docker-executor-path-orphaned.md` (two build paths); this ticket is the specific two-stage drift, not the provisioning feature.
