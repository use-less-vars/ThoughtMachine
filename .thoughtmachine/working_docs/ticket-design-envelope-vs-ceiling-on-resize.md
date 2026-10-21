# design/envelope-vs-ceiling-on-resize

**Question:** a container was created with `network: bridge, workspace: rw`.
Since then, the workspace ceiling tightened (e.g., operator set
`workspace_ceiling.network=banned`). The container keeps running with the old
wider envelope. That's a drift class — envelope wider than the current ceiling.

Resize today would preserve the original envelope (per Gen 46 ruling in
`wave/2026-10-19-container-fixes`), so the drift persists across resize.

**Not this wave.** Two mechanisms likely needed:
1. On workspace ceiling change, existing containers enter a "drifted envelope"
   state — surfaced in the panel (Fact 5), refusable at use-path (existing
   `verify_container_integrity` may already handle this for some cases).
2. On resize, if the re-resolved envelope would be *tighter* than intent,
   require operator acknowledgment before preserving intent (or downgrade
   with explicit confirmation).

Design pass needed. Not a fix-forward.
