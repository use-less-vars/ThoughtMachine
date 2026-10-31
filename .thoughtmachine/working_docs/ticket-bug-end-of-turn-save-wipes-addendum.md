# Ticket: End-of-turn save wipes addendum

**Status:** closed (fixed in `84a5412b`)
**Filed:** 2026-10-31
**Structural:** one-owner-per-fact

## Symptom
The bridge's end-of-turn save rebuilt the addendum from `user_history`, writing an empty addendum over the stored one. Before `84a5412b`, no addendum write could survive end-of-turn save.

## Evidence
Root cause of the "write not visible to live session" blocker. Fixed by carrying `summary` and `addendum` through by `deepcopy` under `_session_state_lock`.

## Related
#262, #263, phantom-config.
