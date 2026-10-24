# MANDATORY STOP — read on entry

Read-on-entry for every new Main engineer. Reviewed at every handoff.
Items promoted from observations → design decisions → migration strands → closed get moved to an "archive" section, not deleted.

1. Read Gen 26's handoff, the baseline doc, the current-state doc.
1a. Read `security_architecture.md` §Escape-to-host. The four boundaries are named there; the "security boundary held" sentence is the bug class at the narrative layer.
2. Read `operator_observations.md` — the operator's repeated points, the things that keep getting lost through migration cycles.
3. Read `working_agreements.md`.
4. Read the design decisions (1 and 2 when 2 lands).
5. Read the recon map.
6. Verify live state: dev HEAD, branch protection, open branches, open strands.
7. Report to operator: what is current, what is blocked, what is next.
<!-- Housekeeping note (Gen 48): the header above advertises an archive section that does not exist. Not added this pass; tracked for a later turn. -->
