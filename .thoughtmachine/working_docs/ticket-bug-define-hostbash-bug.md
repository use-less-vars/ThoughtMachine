# Ticket: bug/define-hostbash-bug — RETIRED stub

**Filed:** 2026-10-22, bookkeeping chunk A
**Status:** RETIRED — superseded (never filed as a single defect)
**Severity:** n/a — no defect is tracked here

## Why this file exists

A search for an existing "define hostbash bug" ticket found none. The search was:

    FileSearchTool, regex: define-hostbash|define_hostbash|define hostbash|hostbash-bug
    scope: .thoughtmachine (923 files)
    result: No matches found

So there is **no prior ticket body to carry forward**. This stub exists only so the name
resolves to somewhere, instead of being silently absent.

## Retirement notice

The "define hostbash bug" was **never one defect; it was two**, now tracked separately:

1. **Double prompt on `ask`** —
   `.thoughtmachine/working_docs/ticket-bug-host-bash-double-prompt-on-ask.md`
   One approved act produces two independent approval transactions: the generic outer gate
   prompts, then `HostBashTool._request_approval` prompts again. Operator approves twice;
   the audit trail records two approvals for one act.

2. **Panel display may echo declared, not enforced** —
   `.thoughtmachine/working_docs/ticket-hygiene-panel-host-bash-display.md`
   The panel can show the `host_bash` grant as configured rather than as enforced, and no
   red banner exists anywhere, so `host_bash: allow` looks ordinary.

A third strand that was also sometimes folded into the same phrase is already tracked:

3. **Wrong cwd** —
   `.thoughtmachine/working_docs/ticket-bug-host-bash-cwd-wrong-workspace.md`
   `host_bash` commands run in an undocumented directory.

## Do not reopen

Do not use this file as a home for new `host_bash` findings. File them against the
specific ticket above, or open a new one. This id is retired.
