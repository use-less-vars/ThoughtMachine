# Competitive Analysis: Claude Code (Anthropic)

## Where We Are Ahead
*   **The Host Port:** We structurally force execution through a gate; they rely on "risk-reduction" parsing.
*   **Control Plane Semantics:** We have explicit Pause=park, Stop=detach, Timeout=leave running. They guess with heuristics.
*   **The Claim Ledger:** We prove invariants before they break, rather than patching CVEs after.

## Where We Are Right (Validation)
*   Worktree isolation (C63b) is non-negotiable for parallel safety.
*   Context management via token limits (we force memory dumps).
*   Process isolation (OS-level) is mandatory for hard boundaries.

## Where We Need to Catch Up (The Steals)
*   **The `deny > ask > allow` Hierarchy:** A workspace ceiling `deny` must absolutely override a session grant `allow`. (See C11).
*   **Dynamic Workflows:** Agents writing orchestrator scripts to manage loops/subagents outside the context window.
*   **CWD-Drift:** Ensuring context compaction does not lose the `workspace_id` or worktree path.
