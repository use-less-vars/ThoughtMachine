# security/workspace-vault-isolation

Operator question 2026-10-19: *"can workspaces leak into each other, like one workspace can read any content of another? All look into the same vault. Explicitly the checkSystem tool."*

**Security-adjacent — Main rules.**

**Questions for the read:**
1. What is the exact boundary that keeps workspace A from reading workspace B's data? (path confinement, vault schema, filesystem grain scope, container mount scope?)
2. `checkSystem` tool — what can it read? Does it respect workspace boundary or is it a cross-workspace window by design?
3. Is there a test asserting workspace A cannot read B's `config.json`, session files, container records?

**Check before filing:** `ticket-bug-checksystem-containers-denied-by-allowlist.md` already exists — related. `ticket-bug-checksystem-vault-status-cross-tenant-leak.md` also exists. The `checkSystem` surface may already have known gaps.

**Not urgent to fix; urgent to know.** A design/audit read named by Main, output filed.

**Note:** "one workspace can read any content of another" is not the same as "vault has no isolation" — the vault is the machine's memory, workspaces are views onto it. The question is whether the *container* (workspace A's execution) can reach workspace B's data. Different layer.
