# Ticket — `security.security_gate` transitively requires the pydantic app stack

**Filed:** 2026-10-25 (Gen 49)
**Corrected:** 2026-10-25 (Gen 49, post-filing verification — see CORRECTION)
**Status:** OPEN — **rescoped**; the original recommended fix is not viable
**Severity:** low as a layering issue (see CORRECTION); one *medium* real bug found inside it (T3)
**Discovered by:** diagnosis of the recurring `ModuleNotFoundError: No module named 'pydantic'` pre-commit failure

---

## ⚠ CORRECTION — the filed premise does not hold

The ticket as originally filed assumed `security/security_gate.py:35` was *the* pydantic
edge, and recommended making the gate pydantic-free. Code verification contradicts both.
Read this section before doing anything from the original filing.

### E1. Line 35 is not the only pydantic edge — and not the first one

`security/security_gate.py` imports, in source order:

```
:29  from thoughtmachine.workspace_capabilities import ...
:33  from thoughtmachine.security import SessionPermissions, PERMISSION_SCHEMA, ...
:34  from agent.config.defaults import PROMPT_TIMEOUT, RESOURCE_REGISTRY
:35  from agent.events import SecurityPromptEvent, EventType, NullEventBus
```

`thoughtmachine/security.py:19` is `from pydantic import BaseModel, Field, ConfigDict, field_validator`,
and `SessionPermissions` is itself a pydantic model (`model_dump()`, `model_fields` are used
throughout the suite, e.g. `tests/test_credential_resource_grain.py:162`). **Line 33 precedes
line 35 and already requires pydantic.** Deleting the `agent.events` import entirely would
leave `import security.security_gate` failing with the exact same `ModuleNotFoundError`.

Confirmed empirically at filing time: the original acceptance criterion — "imports successfully
on an interpreter without pydantic (bare venv, stdlib + PyYAML)" — **cannot be satisfied by
Option A or any variant of it.** It would require decoupling `SessionPermissions` /
`PERMISSION_SCHEMA` / the vault permission store from pydantic as well: a separate, much larger
epic, not this ticket.

### E2. The dependency floor is even higher than pydantic

`thoughtmachine/security.py:11` is `import docker` — module level. So `security.security_gate`
transitively requires **docker** too. The "import-free security core" invariant was already
broken before pydantic was noticed; pydantic was simply the dependency that happened to be
missing on the day.

**Conclusion:** `security_gate` is a *hub*, not a leaf. The leaf discipline is real but it
lives in `security/gate_helpers.py` (and `security/resource_catalog.py`), not in the gate. The
gate is expected to sit on top of `thoughtmachine.*`. This is a documentation bug as much as a
code one — the layering story should say so explicitly.

### E3. Of the three names imported from `agent.events`, only one is used

| Name | Used in `security_gate.py`? | pydantic? |
|---|---|---|
| `EventType` | **NO — dead import.** Zero references anywhere in the `security/` package (a case-insensitive scan for `EventType` across `security/*.py` returns only line 35 itself) | — (plain `enum.Enum`, `agent/events.py:23`) |
| `NullEventBus` | Yes, one use: `isinstance(event_bus, NullEventBus)` at `:1612` | **No** — plain class, `agent/events.py:603-646`, stdlib only |
| `SecurityPromptEvent` | Yes, one use: constructed at `:1626` | Yes — `agent/events.py:269-279` |

So the coupling is one import line wide, and **only one of its three names is pydantic-backed**.
`NullEventBus`'s only role is an identity check, which means it must stay a *single* definition
with `agent.events` re-exporting it — duplicating the class would silently break the worker
"deny immediately" path at `:1612` (two distinct classes ⇒ `isinstance` False ⇒ a worker prompt
would block instead of denying).

---

## Revised scope

The original "make the gate third-party-free" goal is **WONTFIX as scoped** (see E1/E2).
What remains is four small, independently landable items.

### T1 — Drop the dead `EventType` import *(trivial, do first)*

```python
# security/security_gate.py:35
- from agent.events import SecurityPromptEvent, EventType, NullEventBus
+ from agent.events import SecurityPromptEvent, NullEventBus
```

Verify nothing downstream does `from security.security_gate import EventType`:

    grep -rn "security_gate import.*EventType" --include=*.py .

Expected: no hits (the suite imports `EventType` from `agent.events` directly — that is the
documented path and must keep working). No behaviour change; removes a misleading suggestion
that the gate cares about event typing.

### T2 — Migrate `agent/events.py` off the pydantic-v1 `@validator` API *(small, mechanical)*

`agent/events.py` uses `from pydantic import BaseModel, Field, validator` (`:20`) with
**21 `@validator("data")` decorators** under pydantic **2.14.1**. These run on the
deprecation shim; they raise `PydanticDeprecatedSince20` and break in v3.

Precedent for the target style already exists in-repo — follow `thoughtmachine/security.py:19`
(`field_validator`) and `agent/config/session_config.py:15` (`model_validator`):

```python
- from pydantic import BaseModel, Field, validator
+ from pydantic import BaseModel, Field, field_validator

-     @validator('data')
+     @field_validator('data')
      @classmethod
      def validate_data(cls, v):
```

Note the v2 requirement that the method be a `@classmethod` (v1 injected it implicitly) and
that `field_validator` runs **after** type coercion by default — the `data` field is
`Dict[str, Any]`, so coercion is a no-op here and behaviour is preserved. Keep the 21 bodies
verbatim; this is a decorator/import change only.

Regression guard worth adding: assert the module imports with `-W error::DeprecationWarning`
in a subprocess, so the shim cannot creep back.

### T3 — The real bug in this area: `thoughtmachine/security.py` swallows `ImportError` at import time *(medium)*

```python
# thoughtmachine/security.py:35-42
try:
    from agent.events import global_event_bus, EventType, create_event
    EVENT_SYSTEM_AVAILABLE = True
except ImportError:
    EVENT_SYSTEM_AVAILABLE = False
    global_event_bus = None
    EventType = None          # <-- pinned to None permanently
    create_event = None
```

This is the *actual* transitive-dependency hazard, and it is already biting. The comment at
`tools/workspace/worker_thread.py:122-124` records it:

> "`thoughtmachine/security.py`'s try/except ImportError during pytest collection — while
> `agent.events` was mid-load — permanently pinning its EventType to None and breaking
> security-prompt tests later in the session."

A partially-initialised module raises `ImportError`, the handler treats it as "event system
absent", and the `None` is baked into a module that outlives the condition. Every
security-prompt path then fails for the rest of the process, with no diagnostic. This is a
failure-masking pattern in the **security** layer — worse than the layering smell the ticket
was filed about.

Direction (needs its own small design pass, not a drive-by):
- distinguish `ModuleNotFoundError` for the *expected-absent* module from a genuine
  partial-init / dependency failure, which must **not** be swallowed;
- or resolve the names lazily at call time so a stale `None` cannot be pinned;
- either way, leave a WARNING naming the cause when the event system is degraded — silence
  here currently reads as "prompts are wired".

### T4 — Document the real dependency floor

At minimum, `security/gate_helpers.py`'s leaf-discipline docstring should state that
`security_gate` itself is *not* a leaf and requires `thoughtmachine.security` (⇒ pydantic +
docker). Otherwise the next person re-files this ticket from the same wrong premise. That is,
in fact, what happened here.

---

## Why the original failure presented as an import-order bug

Kept for the incident record. `tests/security/test_gate_import_order.py` spawns a child
interpreter to verify import-order independence. It used to delete `PYTHONPATH` wholesale to
keep the repo root off the child's path — which also removed the *only* route to third-party
deps for a system-python interpreter. The child then died with `ModuleNotFoundError: pydantic`,
which read as an import-order regression in `security_gate`. Five commits were blocked and
bypassed with `--no-verify`.

**Both causes are fixed in this wave:**

1. `tests/security/test_gate_import_order.py` — `_clean_env()` strips only `PYTHONPATH` entries
   resolving to the repo root (plus the empty entry = cwd). Verified:
   `/workspace:/opt/some-lib::/workspace/tests/../` → `/opt/some-lib`.
2. `tests/conftest.py` — `pytest_configure` probes a **child interpreter**
   (`_importable_without_pythonpath`): hard `UsageError` if a required third-party dep is
   missing entirely; `warnings.warn` (naming the interpreter) if it is reachable only through
   `PYTHONPATH`. A parent-side `find_spec` would not have caught the original bug — the parent
   could always import pydantic; only the child could not.

Root fix was environmental: the suite now runs under `.venv/bin/python`, not system python.
Full suite 5479 passed / 0 failed.

---

## Acceptance criteria (revised)

- **T1:** `EventType` gone from `security_gate`'s `agent.events` import; full suite unchanged.
- **T2:** zero `@validator` remaining in `agent/events.py`; module imports cleanly with
  deprecation warnings escalated to errors; event round-trip tests unchanged.
- **T3:** a simulated partial-init `ImportError` from `agent.events` does **not** leave
  `EventType`/`global_event_bus` pinned to `None`; the degraded state is logged with a cause.
- **T4:** `gate_helpers.py` docstring states the gate's actual dependency floor.
- **Explicitly NOT an acceptance criterion:** importing `security.security_gate` without
  pydantic. That is not achievable within this ticket (E1) and should not be re-filed.

## Related

- `hygiene/load-bearing-working-docs-not-in-reinclude` — working docs that carry decisions are
  gitignored and outside the re-include set.
