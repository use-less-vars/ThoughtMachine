# Ticket: bug/worker-stop-reset-unscoped-to-session

**Filed:** 2026-10-21, from the worker-command pass at `9d5ed869`, while comparing `_action_stop` / `_action_reset` against `_action_spawn`'s explicit session scoping.
**Severity:** HIGH — `stop` and `reset` fall back to a name-only sweep across *every* session; `reset` then deletes the victim's `context.json` / `status.json` / `command.json`.
**Status:** open; verified by read at `9d5ed869`, not exercised at runtime. NOT blocking, NOT to be fixed in this wave.

## Finding

The scoping machinery exists and is documented. `_find_all_worker_threads` (`tools/workspace/worker_thread.py:3761-3777`) takes an optional scope:

```python
def _find_all_worker_threads(self, worker_name: str,
                             session_id: str | None = None) -> list[tuple[str, Any]]:
    """
    By default the search spans the entire registry, regardless of
    session_id.  When *session_id* is given the lookup is session-scoped.
    """
```

`_action_spawn` uses it correctly, with a comment stating why (`tools/workspace/worker_thread.py:3889-3897`):

```python
# Force is a restart of *my* worker, never a global name sweep: a worker of the
# same name owned by another session (and therefore by another workspace) is
# not this caller's property and must survive untouched. Session scoping is
# what makes force workspace-safe.
session_key = self.session_id or _NO_SESSION_KEY
if self.force:
    stale_instances = self._find_all_worker_threads(self.worker_name, session_id=session_key)
```

`_action_stop` (`:5238`) does the opposite. Its docstring states the fallback as a feature (`:5241-5242`): "First tries the current session; if not found, searches across all sessions and stops any matching instances." The gate and call (`:5255-5257`):

```python
if self.instance_id is None and "is not running" in msg:
    # Not found in current session → search across all sessions
    all_instances = self._find_all_worker_threads(self.worker_name)   # no session_id
```

and the sweep then does real work on each hit: label + log (`:5280-5282`), `t.stop()` (`:5284`), a join retry budget (`:5286-5297`), and — in the `finally` — `t._save_context()` (`:5304`) and `t._cleanup_worker_containers()` (`:5305`), then a registry pop keyed by `(sid, name, thread identity)` (`:5306-5313`) and a per-victim report carrying the *victim's* session id (`:5315`: `stopped_info.append({"session_id": sid, "status": t.status})`, returned as "Stopped %d instance(s) ... across sessions" with `stopped_instances` at `:5317-5325`). Writing the victim's context file and destroying the victim's worker containers are not reporting; they are consequences.

`_action_reset` (`:5392`) is the destructive twin. Docstring (`:5393-5399`): "Mirrors ``_action_stop`` resolution (current session first, then a cross-session fallback), but instead of persisting the final context the worker's state files (context.json / status.json / command.json) are DELETED so the next spawn starts completely fresh." The deleter (`:5403-5411`):

```python
for fname in ("context.json", "status.json", "command.json"):
    (wdir / fname).unlink(missing_ok=True)
```

applied per instance by `_reset_one` (`:5413-5447`: `t.stop()` `:5415`, `t._cleanup_worker_containers()` `:5435`, `_drop_persisted_files(t)` `:5446`, returns `{"session_id": sid, "status": "reset"}` `:5447`), with the same unscoped lookup at `:5461`.

For completeness, the third unscoped call site is not a defect: `_action_check` (`:4560`) uses the name-only form at `:4582` and only *reports*, with an explicit caveat (`:4583-4596`: `"session_id": sid, "note": "Worker is from a different session"`). It reads; it does not mutate.

So of three consumers of the name-only lookup, one is read-only and correctly caveated (`check`), one is scoped with a comment explaining that cross-session workers "must survive untouched" (`spawn`), and two are destructive and unscoped (`stop`, `reset`).

## Why it matters

Worker names are caller-supplied and not unique across sessions — the on-disk leaf is `workers/<instance_label>` under the workspace directory (`tools/workspace/worker_thread.py:947-948`), and the registry key that would disambiguate is `(session_key, name, instance_id)` (`:4410`). The lookup above deliberately discards the session component. The practical result: any session that knows a worker name can stop a same-named worker in a different session/workspace, and `reset` additionally erases three of its state files — with `context.json` being the worker's entire persisted conversation (`:905`, `:2967-2968`).

The asymmetry is the strongest evidence that this is unintentional: the codebase already decided that a same-named worker in another session "is not this caller's property and must survive untouched" (`:3889-3892`), and `force=True` — the most aggressive path — is the one that respects it. `stop` and `reset` are the paths that do not.

## Disposition options

- **A. Drop the fallback for `stop` and `reset`.** Match `spawn`: pass `session_id=self.session_id or _NO_SESSION_KEY` at `:5257` and `:5461`. When the worker exists only elsewhere, answer with the mechanism that already exists at `:4583-4596` (status + `"note": "Worker is from a different session"`) instead of acting on it. *This is the recommendation* — smallest diff, and it makes the three consumers agree.
- **B. Keep the fallback, require consent.** Accept an explicit `cross_session: true` (or `confirm_session: "<sid>"`) field; without it, return the cross-session hit as a report and do nothing. Preserves the operator workflow for a human-driven "kill that other worker" without letting a name alone authorise it.
- **C. Split by destructiveness.** Leave `stop` unscoped (stopping is recoverable: context is saved at `:5304`) and make only `reset` scoped (its three-file delete at `:5407-5409` is not recoverable). Half-measure; still lets one session write `_save_context()` into another's directory and destroy its worker containers.

Recommendation: **A**. If the operator uses cross-session stop deliberately today, **B** is the version that keeps the capability while making it explicit.

## MUST NOT TOUCH

- `_find_all_worker_threads`'s default (`:3766-3776`) and the compatibility note about one-argument wrappers/test doubles. `_action_check` depends on the name-only shape.
- The `_action_check` report block (`:4577-4596`) — it is the model answer for what the other two should do.
- The registry pop keying `(sid, name, thread identity)` at `:5306-5313` — it is already correct and is what makes per-instance cleanup safe.
- The generation guard / `_allocate_generation` machinery (`:2970-2980`, `:3193-3197`) — it protects `context.json` from stale writers and is orthogonal.
- `action=reset`'s *current-session* semantics (`:5393-5399`): resetting your own worker to a clean state is the documented, intended behaviour and must keep working.
