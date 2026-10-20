# bug/container-type-label-vs-lifecycle-class

Docker label `thoughtmachine.container_type: free_use` on containers that the
lifecycle gate classifies as `persistent` (`grain_required_for_remove: operator`).

**Reproduced 2026-10-20** on `g3cut`, `g3series`, `g2-detached` in
`gentle-wolf-72`: `docker ps` shows `free_use`; `ContainerRemoveTool` refuses
with persistent-class message.

**Impact:** anyone reading the Docker labels (as the field agent did) concludes
the containers are reclaimable. They are not. The label is a lie in the safe
direction — suggests more freedom than exists — but still a lie.

**Two authorities on one fact.** Class is written at create (into the label)
and re-derived at removal (lifecycle gate). They disagree for these containers.
Read first: which site writes `container_type`, which derives `lifecycle_class`,
why do they disagree for these three specifically (drift? migration? creation
path mismatch?).

**Fix:** one class, one owner (Inv.1). Either the label is authoritative and
the removal gate reads it, or the lifecycle class is authoritative and the
label gets rewritten to match. Not both.

**Filed by:** Gen 46, 2026-10-20.
