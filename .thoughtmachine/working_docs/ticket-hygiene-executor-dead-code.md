# Ticket: hygiene/executor-dead-code

**Filed:** Gen 45, 2026-10-17, from round-1 recon.
**Type:** hygiene. No behaviour change.
**Status:** filed, not dispatched.

## Items

1. `infra/containers/executor.py` line 1 module docstring still reads
   `"""docker_executor.py — Docker container lifecycle and integrity verification.`
   Stale self-reference left by the 10b move.

2. VCI's destroy branch in `infra/containers/executor.py` is dead code.
   VCI's hard-coded intent has `workspace_owned=True`; `_decide` step (11)
   `stop` is gated behind `not workspace_owned`; so VCI can only return
   `use` or `refuse`. The direct `stop`/`remove`, `_reap_bound_record`/
   `_emit_container_removed` calls, `action_taken="removed"`, and the
   caller's `== 'removed'` log branch in
   `agent/presenter/session_lifecycle.py` are all unreachable.

3. VCI's own docstring ("the container is stopped and removed so it will
   be recreated") is factually wrong per item 2 and per §9.

## Relation to §9

Item 2/3 will be reshaped by `design/ensure-container-refuse-sibling`.
Consider folding this into that mini-spec's delivery, or shipping it
standalone if the mini-spec is delayed. Item 1 is independent and can ship
anytime.

## Reference

Recon round 1 at HEAD 586aa2df:
`.thoughtmachine/working_docs/container-close-residuals.md`
