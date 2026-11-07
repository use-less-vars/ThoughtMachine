# Ticket: bug/host-bash-cwd-wrong-workspace

**Filed:** Gen 46, 2026-10-20 at `44a638b8`
**Severity:** High — every `host_bash` command runs in an undocumented directory
**Status:** filed, not dispatched
**Fix type:** one-line-behavioural, plus a resolution step the tool does not have

## The defect

`tools/host_bash_tool.py:303`:

```python
result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
```

There is **no `cwd=`**. The shell therefore inherits the *server process's*
working directory — whichever directory the server happened to be launched
from. That directory has no guaranteed relationship to the calling workspace.
It is not a workspace; it is whatever the operator's shell was in at start time.

## How it was found

An agent working in a second workspace (an EE project) ran a probe through
`host_bash`. The probe executed against the **ThoughtMachine-dev** checkout —
because the server had been started from that directory. The command did not
fail, did not warn, and produced a plausible-looking result about the wrong
tree. That is the worst failure shape: a command that answers confidently about
something the caller did not ask about.

## Why it matters

1. **Silent cross-workspace execution.** In a multi-workspace install, every
   host shell lands in one directory: the first workspace the operator started
   the server from. Workspace A's agent can read and write workspace B's files
   by accident, with no permission check in the path — the permission system
   answers *may this run*, never *where does it run*.
2. **It is not covered by the Envelope (Fact 2).** The Envelope
   (`security_gate.resolve_container_config`) bounds *containers*. `host_bash`
   is host execution: it has an envelope in principle and none in practice —
   nothing in the composed decision carries a path, a mount, or a scope. This is
   a Fact 2 gap, not a Fact 4 control gap: adding more permission vocabulary
   cannot fix it, because the missing dimension is location.
3. **The audit cannot reconstruct it.** The JSONL record has exactly six fields
   — `timestamp`, `workspace_id`, `session_id`, `command`, `outcome`, `reason`.
   There is no `cwd`. After the fact, the log says *which workspace asked*, not
   *which directory answered*. The two were different in the incident above.
4. **It is documented as expected behaviour.** `docs/ARCHITECTURE_MACHINE_MODEL.md`
   §5 states it plainly: *"It does not run in the workspace. The execute path is
   `subprocess.run(cmd, shell=True, ...)` with no `cwd=`, so the shell inherits
   the server process's working directory."* The doc is honest; the code is
   wrong. §5's framing is a **description of the defect**, and once this ticket
   is fixed, §5 must be corrected in the same wave.

## Fix

Preferred — **bind the command to the caller's workspace**:

- Resolve the workspace root from `_workspace_id()` (the tool already resolves
  the id at `tools/host_bash_tool.py:107-125`; it has no path resolution today —
  that helper is the real work of this fix).
- Pass `cwd=<workspace_root>` to `subprocess.run`.
- When the id cannot be resolved: **refuse**, do not fall back to the inherited
  cwd. Falling back is how this bug happened.

Acceptable interim — **refuse on divergence**: if the resolved workspace root
differs from the inherited cwd, deny with a named reason. Under-serves the
single-workspace case (commands still land wherever the server started) but
closes the cross-workspace hazard immediately.

Either way, add `cwd` to the audit record. That changes the record from six
fields to seven — a documented shape change, not an accident:

- Update the module docstring (`tools/host_bash_tool.py:24-28`), which currently
  promises *exactly* six fields.
- Any consumer asserting six fields is part of the blast radius; find them
  before merging.
- Keep redaction unchanged; `cwd` is operator-supplied and may contain a home
  directory name, which is no worse than `command` already is.

## Test

1. `host_bash` from workspace A while the server was started in workspace B →
   `pwd` prints A's root (or the call is refused), never B's.
2. Unresolvable workspace id → refusal, not inherited-cwd execution.
3. Audit record contains `cwd` and the docstring agrees with the number of
   fields.
4. Existing ask/allow/deny paths unchanged: `TestCheckRequiredCategoriesHostBash`
   in `tests/security/test_security_gate.py` still passes.

## Bearing

Fact 2 (Envelope). The permission layer is the machine's strongest asset and it
is strong about *authority*; it is silent about *scope of effect*. This is the
smallest instance of that silence, and it is one `cwd=` argument wide.

## MUST NOT TOUCH

- `thoughtmachine/security.py::SAFE_DEFAULTS`
- `security/resource_catalog.py`, `agent/config/resource_catalog.json`
- `security_gate.apply_workspace_ceiling` + call sites
- `.thoughtmachine/knowledge/project/architecture_audit_2026-09.md` (parked)
