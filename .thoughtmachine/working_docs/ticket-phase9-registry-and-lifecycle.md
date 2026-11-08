# Phase 9 — Empty registries, vault repair, and workspace lifecycle

Branch: `fix/phase9-registry-recovery`. Sources cited: `session/session_registry.py` (163 lines),
`thoughtmachine/workspace_registry.py` (456), `thoughtmachine/vault_repair.py` (1843), `thoughtmachine/vault_gc.py` (878).

## 1. Status

This is a PLAN. No destructive code has been written; no recovery script has been executed against any vault.

Verified from source:

- The session registry write is atomic (tmp file + `os.replace`, `session/session_registry.py:57-61`), so a torn write is not the likely vector.
- `_load()` returns `{}` on any decode/OSError/non-dict payload with only a `logger.warning` (`:44-55`) — no quarantine, no backup, no rename-aside; the next successful write destroys the evidence.
- `rebuild_from_disk()` writes unconditionally, including when its scan matches nothing (`:139-141`), and two read paths auto-trigger it (`web_ui/backend/session_routes.py:222-225`, `web_ui/backend/server.py:6374-6376`).
- There is no rebuild/reconcile/rescan path at all for the workspace registry (grep: zero matches).
- `vault_gc.run_gc()` has zero production callers (`thoughtmachine/vault_gc.py:784`); `vault_repair.run_repair()` deletes nothing and cannot move a directory (`:1420-1438`).
- The health banner is inspection output rendered twice (`web_ui/backend/vault_repair_routes.py:49-71`): config drift, not session or sidecar damage.

Still unknown (source cannot answer): which of H-A / H-B / H-C (§4) is the cause; current size, mtime and parseability of both registry files; whether the session files under the ~33 workspace dirs carry a top-level `session_id`; where the operator's `ORPHANED SIDECAR DIR` lines come from (the string does not exist in this tree); which items make up the "21 issues / 88 findings"; whether any pre-incident copy of either registry file exists.

## 2. Symptom

- `~/.thoughtmachine/state/session_registry.json` reports **0 sessions** (`session/session_registry.py:23`); `~/.thoughtmachine/state/workspace_registry.json` reports **0 workspaces** (`thoughtmachine/workspace_registry.py:95`).
- ~33 workspace directories exist under `~/.thoughtmachine/workspaces/`, each with `sessions/` subdirectories holding session files.
- The UI shows **"0 sessions, 0 workers"** for every workspace, including workspaces that definitely have sessions.
- UI health banner: **"Vault integrity issues detected. 21 issues found. 88 findings detected."**
- The operator's own diagnostic script printed dozens of `ORPHANED SIDECAR DIR` lines.

The disagreement is between what is on disk and what the two registry files assert. Both assert empty.

## 3. Verified mechanism

| Mechanism | Site | Consequence |
|---|---|---|
| Session registry file | `session/session_registry.py:23` | `~/.thoughtmachine/state/session_registry.json` is the file reporting 0 sessions |
| On-disk schema | `session/session_registry.py:78-87` | Flat dict keyed by `session_id`; entry holds session_id, workspace_id, name, mode, last_active, is_open, created_at, updated_at |
| Write atomicity | `session/session_registry.py:57-61` | tmp file + `os.replace`; a torn/partial write is NOT the likely vector |
| Load failure policy | `session/session_registry.py:44-55` | `{}` + `logger.warning` on JSONDecodeError/OSError/non-dict; no quarantine, no backup, no rename-aside, so the next write destroys the evidence |
| Guarded mutators | `session/session_registry.py:90-99`, `:101-109` | `set_open()` / `remove()` gated on `if session_id in data:`; neither truncates |
| Unconditional writers | `session/session_registry.py:88`, `:139-141` | `register()` and `rebuild_from_disk()` are the only writers that save without condition |
| Rebuild scan roots | `session/session_registry.py:111-141` | Scans `~/.thoughtmachine/sessions/*.json` and `~/.thoughtmachine/workspaces/<ws>/sessions/*.json`; skips `_meta_*` and `open_sessions.json` |
| Rebuild derived fields | `session/session_registry.py:111-141` | `workspace_id` taken from the DIRECTORY NAME, not the file; `is_open` hardcoded `False` |
| Rebuild persistence | `session/session_registry.py:139-141` | `self._save(registry)` unconditional inside `with self._file_lock:`, returns `len(registry)`; a scan matching nothing persists `{}` |
| Session-file gate | `session/session_registry.py` `_read_session_file` | `sid = data.get("session_id", "")` then `if not sid: return` — a file without a top-level `session_id` is skipped with no log |
| Workspace registry: no rebuild | `thoughtmachine/workspace_registry.py` (grep `rebuild\|reconcile\|rescan`: zero matches) | An emptied workspace registry cannot self-heal; no code path re-derives it |
| Workspace registry failure policy | `thoughtmachine/workspace_registry.py:35-40`, `:248-260` | FAIL-CLOSED, the opposite of the session registry: raises `WorkspaceRegistryUnavailable`; a MISSING file is treated as legitimately empty |
| Auto-trigger (route) | `web_ui/backend/session_routes.py:222-225` | `if not sessions:` → `registry.rebuild_from_disk()`; a GET/read request can rewrite the registry file |
| Auto-trigger (server) | `web_ui/backend/server.py:6374-6376` | Same unconditional rebuild from a second path |
| GC reachability | `thoughtmachine/vault_gc.py:784` | `run_gc()` has ZERO production callers; only `tests/test_vault_gc.py` references it |
| GC blast radius (if it ran) | `thoughtmachine/vault_gc.py:406`, `:584-587` | `_rmtree_one_shot` on whole workspace dirs; unlinks session files plus `_meta_` sidecars; gated by `open_sessions.json` ids and file age, never by `session_registry.json` |
| Repair deletes nothing | `thoughtmachine/vault_repair.py` (`run_repair`) | No registry dependency; no listdir/iterdir/glob over the `workspaces/` or `sessions/` trees |
| Repair's only disk move | `thoughtmachine/vault_repair.py:1420-1438` | `_quarantine_root_file` is a plain `shutil.move` of an unknown FILE at the vault ROOT and raises if the source is not a file — it can never move a directory |
| Repair write actions | `thoughtmachine/vault_repair.py:1134-1139` | `rewrite_with_backup` (sibling backup, aborts if the backup fails); create a missing file from its schema default; quarantine a removed JSON node as an artifact |
| Repair quarantine dir | `thoughtmachine/vault_repair.py:120` | Default `<vault_root>/.quarantine` |
| Repair CLI | `thoughtmachine/vault_repair.py:1770-1791` | `--apply`, `--dry-run`, `--quarantine-dir`, `--restore-seeds`; `run_repair` has NO `dry_run` parameter — dry-run is enforced CLI-side only |
| Banner source | `web_ui/backend/vault_repair_routes.py:49-71` | `run_inspection` output rendered twice (issues list + flattened findings list); config drift, not session or sidecar damage |
| Operator's string | repo-wide grep, excluding `.worktrees` | `ORPHANED SIDECAR` / `orphaned sidecar` / `sidecar dir`: **NOT FOUND** |

## 4. Why the registries are empty — ranked hypotheses

One shared fault suffices to explain both registries reading 0. Three candidates survive source reading; source alone cannot decide between them, so the read-only audit (§5) runs first.

**H-A — path resolution (STRONGEST).** The server resolves a different `Path.home()` / `HOME` than the operator's shell, so both registries read a tree that does not exist and the session auto-trigger persists `{}`.
- Predicts: the files the operator inspects and the files the server writes are different (or the server-side `state/` does not exist), while the operator's own tree still holds the session files.
- Discriminating check: print the server process's resolved home/vault root and compare with the shell-side values from the audit.
- Why first: **one** path-resolution fault explains both registries at once; two independent schema faults would be a coincidence.

**H-B — sidecar files lack a top-level `session_id`.** The files exist but every one is skipped at `if not sid: return`, so a rebuild scan finds nothing and persists `{}`.
- Predicts: files parse, none has a top-level `session_id`, and their count is non-zero.
- Discriminating check: the per-file parse / `session_id` / `workspace_id` report in the audit.

**H-C — the files were emptied earlier and the auto-trigger keeps re-persisting `{}`.** An earlier event (migration, manual reset, disk event) truncated both registries; each later read sees "no sessions" and rewrites `{}`, so the emptiness is self-sustaining.
- Predicts: session files on disk are valid and complete, the registry files parse as `{}` (not corrupt), and their mtimes track server activity rather than the workspace tree.
- Discriminating check: registry mtime vs. newest session-file mtime, both printed by the audit.

## 5. Read-only audit script

Run on the host as the SAME user that runs the server. It only reads: no writes, no deletes, no mkdir, no `os.replace`.

```python
#!/usr/bin/env python3
"""Phase 9 registry audit — strictly read-only (no writes, deletes, mkdir, os.replace)."""
import json, os, time
from collections import Counter
from pathlib import Path

def stat_line(p):
    try:
        st = p.stat()
        return f"exists size={st.st_size} mtime={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(st.st_mtime))}"
    except FileNotFoundError:
        return "MISSING"
    except OSError as exc:
        return f"UNREADABLE {exc.__class__.__name__}"

def registry_report(p):
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "MISSING"
    except Exception as exc:
        return f"UNPARSEABLE {exc.__class__.__name__}"
    return f"NON-DICT {type(data).__name__}" if not isinstance(data, dict) else f"entries={len(data)}"

home = Path.home()
vault = Path(os.environ.get("THOUGHTMACHINE_VAULT", str(home / ".thoughtmachine")))
sess_reg = vault / "state" / "session_registry.json"
ws_reg = vault / "state" / "workspace_registry.json"
print(f"Path.home()             : {home}   (resolve: {home.resolve()})")
print(f"HOME env                : {os.environ.get('HOME', '<unset>')}")
print(f"effective vault root    : {vault}  is_dir={vault.is_dir()}")
print(f"state/                  : {stat_line(vault / 'state')}")
print(f"session_registry.json   : {stat_line(sess_reg)}  -> {registry_report(sess_reg)}")
print(f"workspace_registry.json : {stat_line(ws_reg)}  -> {registry_report(ws_reg)}")

ws_root = vault / "workspaces"
try:
    ws_dirs = sorted(d for d in ws_root.iterdir() if d.is_dir())
except OSError as exc:
    ws_dirs = []
    print(f"workspaces/             : UNREADABLE {exc.__class__.__name__}")
files = []
for d in ws_dirs:
    try:
        files.extend(sorted((d / "sessions").glob("*.json")))
    except OSError as exc:
        print(f"  {d.name}/sessions     : UNREADABLE {exc.__class__.__name__}")
print(f"workspace dirs          : {len(ws_dirs)}   session files found: {len(files)}")

no_sid = no_wsid = unparseable = 0
hist = Counter()
for f in files:
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:
        unparseable += 1
        print(f"  {f}: UNPARSEABLE {exc.__class__.__name__}")
        continue
    sid = data.get("session_id") if isinstance(data, dict) else None
    wsid = data.get("workspace_id") if isinstance(data, dict) else None
    no_sid += 0 if sid else 1
    no_wsid += 0 if wsid else 1
    hist[wsid or f.parent.parent.name] += 1
    print(f"  {f}: parses session_id={'yes' if sid else 'NO'} workspace_id={'yes' if wsid else 'NO'}")
print(f"without top-level session_id  : {no_sid}/{len(files)}")
print(f"without top-level workspace_id: {no_wsid}/{len(files)}")
print(f"unparseable session files     : {unparseable}")
print("workspace_id histogram (file value, else dir name):")
for k, v in hist.most_common():
    print(f"  {k}: {v}")

if not vault.is_dir():
    verdict = "H-A (vault root does not exist at the resolved home)"
elif os.environ.get("HOME") and Path(os.environ["HOME"]).resolve() != home.resolve():
    verdict = "H-A (Path.home() and $HOME disagree; the server may resolve yet another root)"
elif files and no_sid == len(files):
    verdict = "H-B (every session file lacks a top-level session_id, so a rebuild scan finds nothing)"
else:
    verdict = "H-C (session files are readable: the registries were emptied earlier and auto-trigger keeps re-persisting {})"
print(f"VERDICT: {verdict}")
```

## 6. Recovery plan

Ordered; each step names its rollback. Nothing here runs before §7 guards exist.

1. **Quarantine both registry files first.** Copy `state/session_registry.json` and `state/workspace_registry.json` into `<vault>/.quarantine/phase9-<utc>/` (the convention of `thoughtmachine/vault_repair.py:120`) with a `stat` listing of both. Rollback: copy back; nothing else has changed yet.
2. **Freeze the write paths.** Stop the server, or block the two auto-trigger sites (`web_ui/backend/session_routes.py:222-225`, `server.py:6374-6376`), so no read re-persists `{}` during recovery. Rollback: restart the service; registry files untouched.
3. **Rebuild the session registry from disk** with `rebuild_from_disk()` (`session/session_registry.py:111-141`), ONLY with the §7(b) guard in place so a zero-yield scan cannot overwrite the quarantined file. Rollback: restore the step-1 copy.
4. **Rebuild the workspace registry.** No such code path exists (grep: zero matches) — new work, sketched below, not committed here. Rollback: restore the step-1 workspace copy.
5. **Verify by counting**, not by "it did not error": registry entry count vs. `workspaces/*/sessions/*.json` file count, and the UI must show a non-zero session count for at least one workspace. If they disagree, roll both files back from step 1 and stop.
6. **Only then** re-enable the auto-trigger paths, with §7(c) active.

**Explicitly forbidden:** calling the existing `rebuild_from_disk()` as "the fix". Unguarded, it persists an empty registry without complaint (`:139-141`) and destroys the only evidence that the prior state was non-empty.

Sketch only — do NOT add to the repo:

```python
def rebuild_workspace_registry_from_disk(self) -> int:
    """SKETCH. Returns a candidate map; writes only when guarded."""
    candidate = {}                       # workspace_id = directory name, the rule the
    for d in sorted((self._vault_root / "workspaces").iterdir()):   # session rebuild uses
        if d.is_dir() and not d.name.startswith("_"):               # session/session_registry.py:111-141
            candidate[d.name] = {"workspace_id": d.name, "path": str(d)}
    # GUARD: empty scan + existing non-empty file => raise, keep the file
    # otherwise: quarantine the current file, then atomic tmp + os.replace
    return len(candidate)
```

## 7. Hardening required before recovery runs

**(a) `_load()` must quarantine, not silently reset** — `session/session_registry.py:44-55`. On JSONDecodeError/OSError/non-dict: rename the corrupt file aside into `<vault>/.quarantine/`, log that path at warning level, and only then return `{}`. Change: the evidence survives the next successful write.

**(b) `rebuild_from_disk()` must refuse a zero-yield overwrite** — `session/session_registry.py:139-141`. If the scan yields 0 entries and the existing file is non-empty, raise instead of `self._save(registry)`. Change: a transient empty scan can no longer persist `{}` over real data.

**(c) The auto-trigger must not write on a read** — `web_ui/backend/session_routes.py:222-225` (and `server.py:6374-6376`). Return the empty state instead of calling `registry.rebuild_from_disk()` inline, and move the rebuild behind an explicit operator action. Change: a `GET` can no longer rewrite the registry file.

## 8. Data-safety correction — vault_repair and vault_gc

The operator's assumption — that repair/GC with `--apply` will delete orphaned session data — is **not supported by source**.

- `run_gc()` (`thoughtmachine/vault_gc.py:784`) is unreachable: zero production callers, only `tests/test_vault_gc.py`. If invoked it would delete whole workspace dirs (`:406`) and session files with their `_meta_` sidecars (`:584-587`), gated by `open_sessions.json` ids and file age — never by `session_registry.json`.
- `run_repair()` deletes nothing: no registry dependency, no listdir/iterdir/glob over the `workspaces/` or `sessions/` trees. Its only disk move is `_quarantine_root_file` (`:1420-1438`), a `shutil.move` of an unknown FILE at the vault ROOT that raises if the source is not a file, so it cannot move a directory.
- What `--apply` CAN do: create a missing file from its schema default; rewrite a file in place with a sibling backup (`:1134-1139`, aborts if the backup fails); quarantine a removed JSON node as an artifact.

Revised recommendation: keep the freeze if it is cheap, but it is **not** protecting against the feared failure mode. The real vector is the unconditional `_save()` in `rebuild_from_disk()` (`:139-141`).

`ORPHANED SIDECAR` / `orphaned sidecar` / `sidecar dir`: **NOT FOUND** anywhere in this tree (repo-wide grep, excluding `.worktrees`). Ask the operator where their diagnostic script came from and what it actually scanned.

## 9. Workspace lifecycle and the Delete Workspace API

DESIGN ONLY. No code written, not implemented, awaiting review.

Constraints: the workspace folder name IS the workspace id wherever the id is derived (`session/session_registry.py:111-141` derives `workspace_id` from the directory name), so renaming a workspace folder is an id change, and both `workspace_registry.json` (`thoughtmachine/workspace_registry.py:95`) and the in-folder `capabilities.json` key off that id. The workspace registry has no rebuild path, so any inconsistency introduced here is permanent.

Endpoint: `DELETE /api/workspaces/{ws_id}` with an explicit confirmation body — `{"confirm_workspace_id": "<ws_id>", "quarantine": true, "actor": "<who>", "reason": "<why>", "dry_run": true}`. The request is rejected unless `confirm_workspace_id` equals the path id and a `dry_run` pass has already been run.

Ordered safe-deletion sequence (move-first):

1. Preflight: workspace exists, registry entry matches, no open sessions, the target is not the vault root.
2. Copy (not move) the workspace tree into `<vault>/.quarantine/phase9-<utc>/<ws_id>/` and verify the copy.
3. Deregister the workspace from `workspace_registry.json`.
4. Only on a later explicit operator action, purge the quarantined copy.

Ordering invariant: **move/copy first, deregister second.** If the copy fails, the registry is untouched and the workspace is still live and indexed. If deregistration fails after a successful copy, re-running step 3 repairs it and no data is lost.

What breaks under the other order (deregister-first): a failure between the two steps leaves a live workspace directory with **no** registry entry, and because there is no rebuild/reconcile/rescan path for the workspace registry, nothing in the codebase can re-derive it — the workspace becomes permanently invisible to the API and the UI.

Must never happen:

- No recursive delete before deregistration.
- No deletion while that workspace has open sessions (check the open-session ledger — the `open_sessions.json` the GC itself gates on, `thoughtmachine/vault_gc.py:584-587`).
- No deletion without a verified quarantine copy.
- Never delete the vault root itself.

## 10. Open questions for the operator

1. Which user/systemd unit starts the server, and what is that process's `HOME`?
2. Does `~/.thoughtmachine/state/` on your side hold both registry files, and what are their sizes and mtimes?
3. Do your session files carry a top-level `session_id` key?
4. Where does your diagnostic script come from, given `ORPHANED SIDECAR` exists nowhere in this tree?
5. Do you hold any pre-incident copy or backup of either registry file?
6. For the Delete Workspace API: is a quarantined copy acceptable as the deletion contract, or do you require immediate purge?
