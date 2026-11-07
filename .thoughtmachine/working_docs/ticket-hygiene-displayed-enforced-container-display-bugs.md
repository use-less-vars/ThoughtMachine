# Ticket: hygiene/displayed-enforced-container-display-bugs

**Filed:** Gen 44, 2026-10-15, from field agent probes across this session.
**Severity:** Low — display-only; the enforcement side is correct.
**Status:** filed, not dispatched. Batch wave.

## Members

1. **`workspace_info.host_bash: "allow"`** while enforcement says `banned`.
   The panel shows a grain the gate doesn't grant. Confirmed field reproduction.
2. **`runtime_state.container_status: "stopped"`** while two containers run.
   The key is unqualified; it reads as "the workspace's containers are stopped"
   but probably means "the session-sandbox container is stopped." Scope-qualify
   or rename.
3. **`thoughtmachine.build_hash` label** on containers reports the *image
   build*, not the *runtime commit*. Three identifiers, one visible, and the
   visible one is named "build_hash". Rename the label (`sandbox_image_build`)
   and add a `runtime_commit` field.

## Related fix (not part of this ticket)

`feat/checkSystem-runtime-version` — surfaces the runtime commit to the agent.
Filed separately; this ticket covers the *container label* and the two display
fields above.

## MUST NOT TOUCH
Same list as the container tickets.
