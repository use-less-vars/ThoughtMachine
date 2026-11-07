# Ticket: hygiene/execution-grain-status

**Filed:** 2026-10-25, Gen 49 tidy pass
**Severity:** MEDIUM — no bypass; the operator is shown two approval prompts for
one act and cannot tell which grain each belongs to
**Classification:** UX annoyance + doctrine drift. **Not** a security hole.
**Status:** open, report-only. The brief that produced this ticket said: report
before any fix. This ticket authorises nothing.
**Related but not duplicate:** `bug/host-bash-double-prompt-on-ask` (filed
2026-10-22) owns the two-prompt defect itself. Read that ticket before this one.

## The hypothesis this ticket was briefed with — REFUTED

The brief said: test first whether `HostBashTool` declares **both**
`host_bash:allow` **and** `execution:full`, both resolve to ASK, both publish a
prompt, and the fix would be to declare only `host_bash:allow`.

On the tree at `e0670545` that is false. `tools/host_bash_tool.py` declares one
category:

```python
required_categories: ClassVar[List[str]] = ["host_bash:allow"]
```

and the comment above it explains why the value part is mandatory:

> ``check_required_categories`` SKIPS any entry lacking ``":"``
> (security_gate.py:1483-1485), so a bare ``["host_bash"]`` would gate nothing.
> ``host_bash`` is absent from the shared rank tables … so the value must be the
> literal ``"allow"``: required ``"allow"`` resolves via the gate's
> exact-string-match fallback -- banned denies, ask prompts, allow passes.

No `execution` category is declared anywhere in the class. So **the proposed fix
is already the state of the tree**, which is itself the finding: the duplicate
prompt cannot be fixed by narrowing declarations.

**Where the second prompt actually comes from** — already traced, already filed
(`bug/host-bash-double-prompt-on-ask`, 2026-10-22), and worth restating because
it retires this ticket's original shape:

1. `security/security_gate.py::check_required_categories` (def at L1483) splits
   `["host_bash:allow"]` into category `host_bash`, required value `allow`.
2. `security/gate_helpers.py::_value_satisfies` finds `allow` absent from
   `GRANT_LEVEL_RANKS` (`security/resource_catalog.py`), so `required_level is
   None`, and the `allowed_str == "ask"` branch returns its ask sentinel →
   the outer gate prompts.
3. `tools/host_bash_tool.py::_request_approval` then publishes its own prompt,
   carrying the actual command text.
4. The first prompt looks empty because `HostBashTool` defines no
   `describe_action`, so the executor passes `""` as the description.

## What survives as this ticket

Not the two prompts — the **status layer** and the **doctrine**:

- **`execution` has no visible status.** There is no operator-facing statement of
  which grain a given `host_bash` call was authorized under, and no statement of
  what the *enforced* value is versus the declared one. Two prompts with two
  different descriptions, neither naming a grain.
- **Doctrine drift across three homes.** The `host_bash:allow` literal is
  explained in a code comment, restated in `docs/host-port.md` §4 (the catalog has
  no `full` level for `network`; `host_bash` accepts only `allow`), and
  re-derived a third time in the 2026-10-22 ticket. Three explanations, no single
  authority — and the §4 statement is the only one a reader outside the code will
  find.
- **The declaration cannot express intent.** `host_bash:` has no level scale, so
  a tool author writing `["host_bash:allow"]` is writing a magic string whose
  behaviour depends on a fallback in a helper they will not read. That is the
  drift the next tool author inherits.

## Five questions

1. **Should `execution` appear in the approval event at all?** Every approval
   event names a category; if the grain is the thing being approved, should the
   event carry `host_bash:allow` (declared) or the resolved level (enforced), and
   which one does the UI label it with today?
2. **Is "no level scale for `host_bash`" a deliberate model or an unfinished
   migration?** The catalog has levels for `banned/read/ask/connect/write/
   write_on_feature_branch/outbound/full` but not `allow`. Either `allow` is a
   level (`banned < ask < allow`) and belongs in `GRANT_LEVEL_RANKS`, or it is a
   keyword and every declaration site should say so. The current state is both.
3. **Where should the one authoritative explanation live?** `docs/host-port.md`
   §4 is the only outside-the-code home. Should the code comment shrink to a
   pointer at it, or should §4 shrink to a pointer at the comment? (Note: §4
   cannot be edited before the W4 PR merges — `docs/host-port.md` is one of its
   nine modified files.)
4. **Does the operator need a visible "enforced grain" line for `host_bash`
   specifically?** There is an adjacent, already-filed display defect
   (`hygiene/panel-host-bash-display`: the panel can show the declared value
   rather than the enforced one, and no banner marks `allow`). If that ticket
   lands, does this one dissolve?
5. **What stops a fourth explanation being written?** Three homes today. If the
   answer is "nothing", this ticket is a doctrine problem, not a UX one, and
   belongs next to the `no-tool-wrapping` census rather than in the panel backlog.

## Why it is MEDIUM and not higher

No authority is widened. `_value_satisfies` misrouting `("allow","ask")` to ASK
makes the gate **stricter** than intended for that pair — the failure direction
is an extra prompt, never a missing one. The `banned` and `deny` paths are
unaffected, and the worker-context denial in `host_bash_tool.execute()` still
fires before any approval is offered.

## MUST NOT TOUCH (inherited from the 2026-10-22 ticket, still binding)

- `security/gate_helpers.py::_value_satisfies` — the `("allow","ask") -> "ASK"`
  behaviour is load-bearing for other tools' declared categories.
- `security/resource_catalog.py::GRANT_LEVEL_RANKS` — adding `allow` changes
  every category that declares it, far beyond `host_bash`. That is why Q2 is a
  question for the design pass and not a fix in this ticket.
- The worker-context denial in `tools/host_bash_tool.py::execute()` — the fix
  must not make an in-worker `ask` reachable.
