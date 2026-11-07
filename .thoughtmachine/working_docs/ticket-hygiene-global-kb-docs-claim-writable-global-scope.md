# Ticket: hygiene/global-kb-docs-claim-writable-global-scope

**Filed:** Gen 47, 2026-10-21, from the F24 global-KB pen-test follow-up (this session).
**Severity:** Low — docs-only text defect; no code path is affected. Escalates because the wrong claim is injected into every agent prompt and instructs agents to attempt writes that cannot succeed.
**Status:** OPEN — not dispatched. The shipped `resources/global_kb/*.md` present the global scope as a peer of the writable workspace scope and never state that it is read-only.

## Bearing

The global KB is **read-only for agents**. `agent/knowledge/global_kb.py` defines two
subdirectories with different privileges:

- `agent/knowledge/global_kb.py:28-30` — `GLOBAL_KB_DIR = Path.home()/".thoughtmachine"/"global"`;
  `SYSTEM_DIR = GLOBAL_KB_DIR/"system"` (synced **read-only** from `resources/global_kb/*.md`);
  `USER_DIR = GLOBAL_KB_DIR/"user"` (writable).
- `:146` — `USER_DIR.mkdir(...)` is the only directory the module writes into.

Every document in `resources/global_kb/` describes the global scope with no such
restriction. It is listed as an interchangeable alternative to `scope=workspace`, and
`mode=append` / `mode=create_domain` are shown without any scope qualifier. An agent reading
these docs will (a) believe it can persist notes globally and (b) tell the user it has done
so.

Scope note — this ticket is the **write-capability half only**. The same lines also carry
the wrong global path `~/.thoughtmachine/knowledge/` (the code uses
`~/.thoughtmachine/global/`); that is already filed separately in
`ticket-bug-knowledge-tool-reports-wrong-path.md`. Fix both in one pass if convenient, but
do not file this claim as a duplicate of the path defect: a doc can state the correct path
and still wrongly imply the scope is writable.

## Evidence — stale write claim at this checkout

### The primary wrong claim

- `resources/global_kb/capabilities.md:135` — `## Knowledge Tools`;
  `:137` — `### KnowledgeBaseTool`;
  `:138` — "Persistent project notebook for storing and retrieving information. Two scopes:";
  `:139` — "`- scope=workspace` — project-local KB in `.thoughtmachine/knowledge/`";
  `:140` — "`- scope=global` — user-wide KB in `~/.thoughtmachine/knowledge/`".
  No "read-only" / "not writable" adjective anywhere in the section. The two scopes are
  presented as interchangeable peers; the workspace one *is* writable, so the reader infers
  the same for global.
- `resources/global_kb/faq.md:53` — `## Knowledge Base`;
  `:56-58` — "A: `scope=workspace` accesses project-local knowledge
  (`.thoughtmachine/knowledge/` in your project). `scope=global` accesses the user-wide global
  knowledge base at `~/.thoughtmachine/knowledge/`." — access stated, write restriction not.
- `resources/global_kb/faq.md:65-67` — "**Q: Can I create custom KB domains?** A: Yes! Use
  `KnowledgeBaseTool mode=create_domain domain=my_topic` ..." — a **write** mode, presented
  with no scope qualifier, immediately after the two-scope answer above.
- `resources/global_kb/faq.md:15` — "Dual knowledge base — project-local and user-global
  persistent **storage**".
- `resources/global_kb/faq.md:69-70` — "A: Use `KnowledgeBaseTool mode=search query=...
  scope=workspace`" — the one place a scope is pinned, and it pins the *writable* one, which
  reads as an arbitrary example rather than the only correct choice for a persisted note.

### Same class of claim elsewhere in the synced docs

- `resources/global_kb/handbook.md:43-44` — "**Workspace KB**: `.thoughtmachine/knowledge/`
  — project-specific notes / **Global KB**: `~/.thoughtmachine/knowledge/` — user-wide notes".
  Symmetric phrasing ⇒ symmetric expectation.
- `resources/global_kb/handbook.md:42-47` — "Knowledge Discipline" section, which directs the
  agent to record knowledge without ever naming workspace as the only writable target.
- `resources/global_kb/handbook.md:69` — "6. KnowledgeBaseTool mode=append domain=bugs_and_fixes".
- `resources/global_kb/handbook.md:145` — "Use: `KnowledgeBaseTool mode=append
  domain=development_guides`". Both `:69` and `:145` are write-mode instructions with no scope
  qualifier.
- `resources/global_kb/overview.md:62-63` — "**KB (workspace)**: `.thoughtmachine/knowledge/`
  / **KB (global)**: `~/.thoughtmachine/knowledge/`". Peer framing again.
- `resources/global_kb/capabilities_reference.md:16` — "| **Knowledge Base** | Remember
  decisions, track bugs, keep notes | ..." — capability phrased as writing.
- `resources/global_kb/configuration.md:72-76` — knowledge-base configuration table (presents
  both locations as configurable storage without a privilege distinction).
- `resources/global_kb/onboarding_guide.md:49-58` — "🧠 The Knowledge Base — My Memory";
  `:53` — "📓 Project Notebook (Workspace KB)"; `:56` — "🌍 Global Notebook (Global KB)";
  `:84` — "If I stored things in the Global KB, yes — I'll remember preferences and notes
  across sessions and projects." **This is an explicit write claim** — it says the agent
  *stores* into the global KB and the result persists. `:93` — "Tell me *\"Remember that...\"*
  and I'll store it." — same, with no notion that the target is workspace-only.

### The truth these contradict

- `agent/knowledge/global_kb.py:28-30` — `SYSTEM_DIR` is the read-only synced doc tree;
  `USER_DIR` is the writable one; the agent has no write path into `system/`.
- `agent/knowledge/global_kb.py:146` — `USER_DIR.mkdir(...)`, not `SYSTEM_DIR`.
- Correct wording: **the global KB is read-only for agents** — reads/searches only. Its
  `system/` documents are synced from `resources/global_kb/*.md` and can never be edited by
  an agent. Only `scope=workspace` is writable (`mode=append`, `mode=update`,
  `mode=create_domain`). Any note the agent must persist goes to the workspace KB.

### Version gate — a text edit alone reaches nobody

- `agent/knowledge/global_kb.py:71-74` — `_read_resource_version()`;
  `:80` — `ensure_global_kb()`;
  `:106-113` — `needs_sync = stored_version != current_version` (and `not SYSTEM_DIR.is_dir()`);
  `:116-131` — copy `*.md`, then prune stale files;
  `:141` — version-marker write.
- `resources/global_kb/.version` currently reads `1.2.0`.
- Consequence: editing the shipped `.md` files **without bumping `.version`** leaves
  `needs_sync` False on every existing install and the old, wrong claim survives the upgrade.
  The bump is part of the fix, not a nicety.

## Why it matters

- The docs are the agent's own instructions, synced into `~/.thoughtmachine/global/system/`
  and read back as authoritative. A wrong write claim does not stay in the repo — it becomes
  agent behaviour and then agent-authored text.
- `onboarding_guide.md:84` and `:93` promise the user persistence that the implementation
  cannot deliver. The user is told "I'll remember this across projects", the write lands in
  the workspace KB (or fails), and the loss is discovered sessions later with no error.
- The failure is silent by construction: read-only enforcement rejects the write, and a
  rejected write looks to the model like a note it may have mis-scoped, not like a
  documented capability that does not exist.
- Hygiene class, but same root cause as the path ticket: prose describing a storage system
  that nothing executes, surviving review because no test reads the docs.

## Fix sketch

Docs-only; no code change beyond the version bump.

- Rewrite the sites above so that the **global scope is explicitly described as read-only**:
  reads/searches only, `system/` synced from `resources/global_kb/*.md` and never editable by
  an agent. State that `scope=workspace` is the only writable scope for `append`, `update`,
  and `create_domain`.
- Qualify the unqualified write-mode examples: `handbook.md:69`, `handbook.md:145`,
  `faq.md:65-67` must name `scope=workspace` (or state "workspace scope only").
- Fix `onboarding_guide.md:84` / `:93` to say the agent stores notes in the **workspace** KB,
  and that the global KB is something it reads, not something it writes.
- Keep the two scopes distinct rather than merging them into one sentence; the asymmetry is
  the actual product behaviour, not an inconsistency to smooth over.
- Drop the peer framing in `capabilities.md:138-140`, `handbook.md:43-44`,
  `overview.md:62-63` — "two scopes, two privilege levels", not "two notebooks".
- Bump `resources/global_kb/.version` (currently `1.2.0`) in the same commit, or no existing
  install re-syncs and the claim survives indefinitely.
- Do **not** "fix" this by making the global scope writable. The read-only split is the
  intended design (`global_kb.py:28-30`); the docs are what is wrong.

## Verification

1. `grep -rn -iE 'knowledge|kb|notebook' resources/global_kb/*.md` — every remaining
   description of the global scope carries the read-only qualifier; no unqualified
   `mode=append` / `mode=update` / `mode=create_domain` example remains.
2. `grep -rn -iE 'Global (KB|Notebook)' resources/global_kb/*.md` — no site claims storage /
   remembering into the global KB. `onboarding_guide.md:84` and `:93` are the specific
   regressions to re-read.
3. `cat resources/global_kb/.version` — bumped above `1.2.0`.
4. Upgrade path, on a machine with an existing install: after the bumped build runs,
   `cat ~/.thoughtmachine/global/.version` equals the new bundled value **and** the corrected
   sentence is present in `~/.thoughtmachine/global/system/onboarding_guide.md` — i.e. the
   re-copy fired, not just the source edit landing.
5. Cross-check against `agent/knowledge/global_kb.py:28-30` and `:146` — the docs now name
   the same writable directory the code does.
6. Path cross-check (shared with the path ticket): remaining `~/.thoughtmachine/knowledge/`
   hits in these files are gone.

**Not established:** whether `system_prompt.txt`, `resources/default_system_prompt.txt`, or
`docs/`, `web_ui/` repeat the write claim. This sweep covered `resources/global_kb/*.md`
only; the prompt files were in scope of the path ticket but were not re-read here for the
write-capability claim.

## MUST NOT TOUCH
Same list as the container tickets.
