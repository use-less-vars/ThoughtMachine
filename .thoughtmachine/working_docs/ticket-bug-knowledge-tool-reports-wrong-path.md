# Ticket: bug/knowledge-tool-reports-wrong-path

**Filed:** Gen 47, 2026-10-21, from the F24 global-KB pen-test (this session).
**Severity:** Low — path drift in prompt/doc text; no read or write path is affected. Escalates only because the wrong path is injected into every agent prompt.
**Status:** OPEN — 4 of 11 sites fixed in commit `8ddf93ed` (branch `fix/global-kb-write-gate`); 7 prompt / resource / manifest sites still advertise a directory the code never creates or reads.

## Bearing

Everything that *describes* the global knowledge base says `~/.thoughtmachine/knowledge/`. The code that *implements* it uses `~/.thoughtmachine/global/`. The code wins.

- Truth, single source: `agent/knowledge/global_kb.py:28` — `GLOBAL_KB_DIR = Path.home() / ".thoughtmachine" / "global"`; `:29` `SYSTEM_DIR = GLOBAL_KB_DIR / "system"`; `:30` `USER_DIR = GLOBAL_KB_DIR / "user"`.
- Same file, module docstring `:3-14` documents `~/.thoughtmachine/global/` holding `.version`, `system/` (read-only, synced from `resources/global_kb/*.md`), `user/` (writable, `my_notes.md`).
- `~/.thoughtmachine/knowledge/` is never created by that module and never read. It is a displayed path only.

Dividing line to keep straight while reading this ticket: **workspace** KB is `.thoughtmachine/knowledge/` (correct as written everywhere), **global** KB is `~/.thoughtmachine/global/`. Only the `~` half is wrong.

## Evidence — still wrong at this checkout (branch `fix/global-kb-write-gate`)

- `system_prompt.txt:18` — live agent prompt: "A project notebook is available in `.thoughtmachine/knowledge/` (workspace scope) or `~/.thoughtmachine/knowledge/` (global scope)". Global half wrong; workspace half correct. Highest-leverage site: injected into agent context every session, so the wrong path propagates into agent-authored notes and tickets.
- `resources/default_system_prompt.txt:72-73` — item "12. **Knowledge Base**: ... `.thoughtmachine/knowledge/` ... or `~/.thoughtmachine/knowledge/` (global scope)". Same defect in the shipped default prompt.
- `resources/global_kb/architecture.md:195` — "Dual scope: workspace (`.thoughtmachine/knowledge/`) and global (`~/.thoughtmachine/knowledge/`)". Workspace half correct.
- `resources/global_kb/capabilities.md:140` — "`scope=global` — user-wide KB in `~/.thoughtmachine/knowledge/`" (`:139` workspace half correct).
- `resources/global_kb/faq.md:58` — `~/.thoughtmachine/knowledge/` (`:56-57` workspace half correct).
- `resources/global_kb/handbook.md:44` — "**Global KB**: `~/.thoughtmachine/knowledge/` — user-wide notes" (`:43` workspace half correct).
- `resources/global_kb/overview.md:63` — "**KB (global)**: `~/.thoughtmachine/knowledge/`" (`:62` workspace half correct).
- `resources/MANIFEST.json:70` — note: "Global knowledge base (resources/global_kb/*.md → ~/.thoughtmachine/knowledge/system/) ... ensure_global_kb()".

### Already fixed — do not re-open as work

Commit `8ddf93ed` corrected the four sites in `tools/knowledge_base.py`: class docstring `:131`, `scope` field description `:237`, `mode=list scope=global` response header `:578`, and the usage-hint block `:613-618` (write-mode hints removed). Those now agree with `global_kb.py`. Not part of this ticket.

### Refuted — do NOT "fix" this

`agent/config/models.py:125` — `kb_path: Optional[str] = Field(default=None, description="Path to knowledge base directory (None = default .thoughtmachine/knowledge/)")`. That is the **workspace** KB path (`kb_path`), so `.thoughtmachine/knowledge/` is correct there. It is also dead config — no readers. Leave it alone.

## Why it matters

- An agent or operator following the documented path inspects a directory that does not exist, and may conclude the KB is empty, then re-create content that already exists under `global/`, or file the feature as broken.
- Prompt text is the propagation vector: `system_prompt.txt:18` reaches every agent, and agent-written notes/tickets/reports then repeat `~/.thoughtmachine/knowledge/` as fact. This ticket itself exists because that loop ran.
- Hygiene class, but the failure mode is exactly the one that hides real problems later: "documented storage location ≠ actual storage location" survives reviews, because nothing executes the docs.

### Version gate — a text edit alone changes nothing

- `agent/knowledge/global_kb.py:71-74 _read_resource_version()` reads the bundled `resources/global_kb/.version`; `:80 ensure_global_kb()`; `:101 current_version`; `:102 version_marker = version_file or (GLOBAL_KB_DIR / ".version")`; `:106 stored_version`; `:110 needs_sync = stored_version != current_version`; `:113 if needs_sync or not SYSTEM_DIR.is_dir():` → copies `*.md` (`:116-119`), prunes stale files (`:126-131`), then writes the refreshed marker (`touched.append(str(version_marker))` at `:141`).
- `resources/global_kb/.version` currently reads `1.2.0`.
- Consequence: editing the shipped `.md` files **without bumping `.version`** leaves `needs_sync` False on every existing install, and the old, wrong text survives the upgrade. The bump is part of the fix, not a nicety.

## Fix sketch

- Rewrite the seven sites above to `~/.thoughtmachine/global/` (with the `system/` and `user/` subdirectories that actually exist).
- Keep both halves explicit and asymmetric — workspace `.thoughtmachine/knowledge/`, global `~/.thoughtmachine/global/`. Do not "harmonise" the workspace path to match the global one; the asymmetry is the source of this bug, not a typo to be smoothed over.
- Bump `resources/global_kb/.version` (currently `1.2.0`) in the same commit, or none of the doc edits reach an existing install.
- Do **not** add a compatibility symlink at `~/.thoughtmachine/knowledge/`. That would legitimise the wrong path and start writing user notes into it.

## Verification

1. `grep -rn 'thoughtmachine/knowledge' --include='*.md' --include='*.txt' --include='*.json' --include='*.py' .` — remaining hits are workspace-scope only (`.thoughtmachine/knowledge/`, no `~`) plus the refuted `agent/config/models.py:125` description.
2. `python -c "from agent.knowledge.global_kb import GLOBAL_KB_DIR, SYSTEM_DIR, USER_DIR; print(GLOBAL_KB_DIR, SYSTEM_DIR, USER_DIR)"` → three paths under `~/.thoughtmachine/global/`, `system/`, `user/`.
3. Upgrade path, on a machine with an existing install: after the bumped build runs, `cat ~/.thoughtmachine/global/.version` equals the new bundled value **and** `grep -c 'thoughtmachine/global' ~/.thoughtmachine/global/system/faq.md` ≥ 1 — i.e. the re-copy actually fired, not just the source edit landing.
4. Fresh install: `~/.thoughtmachine/knowledge/` is not created.

**Not established:** whether `docs/` or the web UI repeat the wrong global path. This ticket's sweep covered `system_prompt.txt`, `resources/**`, and `tools/knowledge_base.py` only; `docs/` and `web_ui/` were not re-searched for this subject.

## MUST NOT TOUCH
Same list as the container tickets.
