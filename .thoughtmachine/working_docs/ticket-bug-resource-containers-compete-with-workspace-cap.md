# bug/resource-containers-compete-with-workspace-cap

Resource containers (`tm-resource-git`, tool execution vehicles) count against
the same per-workspace container cap as workspace content containers
(`emfit-build`, `kicad-build`, etc).

**Reproduced 2026-10-20** in `gentle-wolf-72`: 6 persistent containers
(workspace legitimately full), git tool needs a 7th for `tm-resource-git` ->
`container_create_failed: container limit reached (6/6)`. Agent cannot commit.
Git tool never invokes git — dies at container allocation.

**Design question:** should resource containers compete with workspace
containers for the cap? Three fix shapes:
- (a) Exempt `tm-resource-git` (and other one-shot resource containers) from
  the workspace cap — separate budget
- (b) Cap counts by class: persistent / ephemeral / resource each get their own
  limit
- (c) Git tool reuses one persistent `tm-resource-git` container instead of
  creating per call — reduces pressure if per-call creation is happening

Need a read first: does the git tool create a new container per call, or reuse?

**Filed by:** Gen 46, 2026-10-20.

## ANSWERED 2026-10-20 (source read) — it reuses, and the deadlock is permanent

Fix shape (c) is already the behaviour, so it is not a candidate.
`infra/resource_container_manager.py:1304-1400 ensure_resource()` looks for an
existing container (`_find_resource_container()`, `:993-1021`) and creates ONLY
on a miss (`:1367-1384`). Reuse key: the deterministic docker name
`tm-res-<sha256(workspace_path)[:12]>-git`, matched within
`thoughtmachine.workspace_id=<ws>` (`:935-941`, `:993-1021`). Per-call work is
exec only (`:1503-1575`); nothing tears the container down — `git_info_tool.py`
contains zero `.stop()`/`.remove()` calls, and `ResourceContainerManager`
stop/remove are reachable only from workspace provisioning/cleanup.

The deadlock is therefore ONE-TIME and PERMANENT, not recurring: the resource
container must be born once, and its creation passes through
`security/admission_gate.py:475-490`, which is unconditional and runs even for
`container_type="resource"`. But once it exists it does not consume a slot —
`lifecycle_class_from_labels` classifies it RESOURCE and
`_counts_toward_ceiling` (`:242-268`) only counts `persistent`, non-terminal
containers. So one operator removal frees a slot, the resource container is
created, and that workspace never hits this again.

**Filed by:** Gen 46, 2026-10-20.
