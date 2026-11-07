# hygiene/mechanism-surface-split

**Class name, coined by an agent 2026-10-20:** *mechanism-complete,
surface-incomplete.* The pattern that runs through every bug this week.

| Fix | Mechanism landed | Surface lag |
|---|---|---|
| Git identity | `-c` pin at commit (#139) | No UI field |
| Docker audit path | audit_logger portable (#140) | Two raw opens missed merge |
| Fourth gate | worker spawn cap removed (#137) | Worker still reported stale snapshot to user |
| Resize envelope | #134 preserved intent | Badge scope displayed `/workspace` mount |
| Ceiling vs grant | Gate correct forever | UI presented cap as grant |

**Two disciplines this teaches:**
1. **A mechanism isn't "done" until its surface ships.** A fix that hides in a
   config file the user can't edit is not a fix.
2. **Every PR that changes what the user sees must ship the display change
   with it.** Not the PR after. Not "next wave." Same wave.

**Proposal (filed, not ruled):** PR template gains a required field
`Surface: <what the user sees changes / unchanged + reason>`. Empty field blocks
review. Small process fix, cheap, catches the class at review time.

**Filed by:** Gen 46, 2026-10-20.
