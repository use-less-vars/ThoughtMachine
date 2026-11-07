# Vault Ownership Manifest

Ownership ledger for the 50 unverified facts of `docs/workspace_isolation_matrix.md`.

Status: design / proposal - no code, no resolver, no gate.
Generation: Gen48.
Source of rows: `docs/workspace_isolation_matrix.md` (310 lines).

## 0. What this document is, and what it is not

This document converts the 50 rows of `docs/workspace_isolation_matrix.md` whose `Verification` column reads `NONE` into an ownership manifest. For each such fact it records the namespace key, the realm, the writer site, the reader site(s), the deleter site, the owning test, and a status. Every entry carries a `file:line` reference so that it can be checked against the tree rather than believed.

It is explicitly not: a code change; a vault resolver design; an admission gate; a test plan; a replacement for the matrix; or a commitment to any of the above. Nothing described here is executable, and no behaviour is changed by writing it down.

The matrix stays the verification ledger. It records entry points, whether a fact is scoped, where the code sits (`Owner site`), and whether anyone has demonstrated the behaviour (`Verification`). This document becomes the ownership ledger. It records who writes a fact, who may read it, who may delete it, and under which namespace key and realm.

Keeping the two ledgers separate is better than extending the matrix in place. The matrix is evidence about what has been demonstrated; ownership is a claim about who is answerable. Merging them lets an untested row look owned because it carries an `Owner site`, and lets an unowned row look merely untested because it carries `Verification: NONE`. Separated, "owned but untested" and "owned by nobody" are visibly different states, and the second cannot hide behind the first.

The conversion also changes the unit of the claim. A matrix row asserts that a fact exists at a site and has or has not been demonstrated. An ownership entry asserts something narrower and more falsifiable: this key, in this realm, is written by this line, read by these lines, deleted by that line, and pinned by this test - or names the verb for which no such line was found. The `NONE` rows are exactly the rows where the second kind of claim was never made.

Status, in this document, is one of three values and nothing else: `owned-and-verified` (a writer site exists and a test demonstrates it), `owned-untested` (a writer site exists and no test does), `no-owner` (no site could be named for the write). The third value is the one the matrix cannot express, because an absent owner is recorded there as the literal string `UNOWNED` sitting inside a column that otherwise holds code locations - a difference that is easy to read past when scanning the table.

The intended use is narrow. When a change touches a vault fact, the manifest should answer which realm and namespace key that fact lands in, which line writes it, and who is answerable - without re-reading the tree. The matrix is then consulted for the separate question of whether the behaviour has been demonstrated.

An entry that names no writer is still useful, and is the main reason the ledger is worth keeping. A `no-owner` entry says in one place that a fact reaches the vault with no assigned line behind it, which is a reviewable statement. The corresponding matrix row says only `NONE` in a column about testing, so the same gap reads as test debt and is absorbed by whoever is looking at tests rather than by whoever owns the fact.

## 1. Why the existing matrix could not answer the ownership question

All three tables in `docs/workspace_isolation_matrix.md` share one header, byte-identical: §5.1 WebSocket at L130, §5.2 HTTP at L166, §5.3 tools at L200.

```
| Entry point | Fact | Scoping status | Owner site | Verification |
|---|---|---|---|---|
```

The by-the-numbers picture:

| Measure | Value |
|---|---|
| Data rows across the three tables | 65 (§5.1 L132-160 = 29; §5.2 L168-192 = 25; §5.3 L202-212 = 11) |
| Rows where `Verification` is the literal `NONE` | 50 of 65 |
| Rows where `Owner site` contains `UNOWNED` | 4 (L188, L189, L191, L209) |
| Rows where `Scoping status` is `not scoped` | 10 |

The five columns answer two questions and only two: where the code for a fact lives (`Owner site`) and whether anyone has demonstrated the behaviour (`Verification`). The three verbs that decide ownership - write, read, delete - have no column at all. `Scoping status` describes the fact, not the responsibility for it: a row can read `not scoped` on 10 occasions with no indication of who would scope it, and `Owner site` names the line that executes, which is not the same thing as the party answerable for the write.

Counting method matters here, because the schema makes the count easy to get wrong. The header appears three times - once per table - so a count anchored on a single header match sees only one table's rows. The 65 figure is the sum of the three data ranges: §5.1 L132-160 (29 rows), §5.2 L168-192 (25 rows), §5.3 L202-212 (11 rows). The `NONE` count is likewise a per-row string comparison across those three ranges, not a property of the header or of either section.

The two weak signals are worth separating, because neither is a synonym for "no writer". `UNOWNED` (L188, L189, L191, L209) is an explicit admission that the row's `Owner site` cell names no site. `not scoped` (10 rows) is a different claim: the fact has a site and simply does not carry the workspace key. An unscoped fact can still have a very definite writer, and an unowned fact can still be verified. The manifest records all three dimensions separately for exactly that reason.

The realisation, stated once: ownership and verification had been collapsed into a single `Verification` column, so the table answered "who tested this" when it was asked "who owns this", and the 50 `NONE` rows are what that collapse looks like at scale. A `NONE` is not evidence that nobody owns the fact; it is evidence that the column was never the place where ownership was recorded.

The mapping below is not one-to-one. A single old column expands into three, and that asymmetry is the point: `Owner site` was carrying the write, the read and the delete at once, and it named only one line.

The per-fact replacement schema, mapped against what it replaces:

| Old column | Replacement column(s) |
|---|---|
| Entry point | Entry point |
| Fact | Fact |
| Scoping status | Realm + Namespace key |
| Owner site | Writer site + Reader site(s) + Deleter site |
| Verification | Owner test + Status |

Column list, in order, for a single fact:

`Entry point | Fact | Namespace key | Realm | Writer site | Reader site(s) | Deleter site | Owner test | Status`

The replacement schema does not, by itself, fix anything. It makes the responsibility triple explicit so that a missing verb is a visible gap rather than an absent column; it does not create the writer, add the test, or validate the namespace key the whole ledger depends on. Where the key itself is not enforced, the entry records that the key is nominal rather than protected.

The 50 rows with `Verification == NONE` are exactly the set this document converts.

## 2. The namespace key is workspace_id, and nothing validates it

Every workspace-scoped fact in this manifest is keyed by a `workspace_id`, so the key has to be trustworthy before any per-fact ownership claim means anything. It is minted server-side, which is the right design, and it is then never validated at the layer that turns it into a filesystem path. The two halves of that sentence are set out separately below.

### 2.1 Where the id comes from (server-side only)

| Step | Site | What it does |
|---|---|---|
| Mint | `thoughtmachine/workspace_registry.py:370` | `ws_id = generate_human_id()`; `generate_human_id()` at `:51-57` produces `{adjective}-{noun}-{digits}` |
| Dedupe | `workspace_registry.py:372` | rejects a generated id that already exists |
| Delegate | `workspace_registry.py:381` | `register_by_root` hands off to `register_workspace` |
| Registry write | `workspace_registry.py:306` | `WorkspaceRegistry.register_workspace`, the only registry writer: `raw[workspace_id] = entry.to_dict()` at `:342`, then `self._save(raw)` at `:343` |
| HTTP route | `web_ui/backend/workspace_routes.py:1700` | `create_workspace`, POST `""` at `:1699` |
| Route body | `workspace_routes.py:1738` | `entry = registry.register_by_root(confined)` |
| Directory bootstrap | `workspace_routes.py:1739` | `ensure_workspace_dirs(entry.id)` |

`register_by_root` is at `workspace_registry.py:346`. The client cannot choose an id: `WorkspaceCreateBody` at `workspace_routes.py:1580-1585` carries `path`, `purpose` and `settings`, and has no `workspace_id` field. That is the one part of this design that is correct, and it is what makes the gap below a code-path gap rather than a remote-input gap - a caller inside the process can supply any string, a browser cannot.

### 2.2 What validation exists

| Guard | Site | Coverage |
|---|---|---|
| `RESERVED_WORKSPACE_IDS: frozenset = frozenset({"global"})` | defined `workspace_registry.py:44-48` | one reserved id, one entry |
| Membership check | `workspace_registry.py:319` | exactly one call site |
| Workspace-id regex | none | zero hits for `re.match`, `re.fullmatch` or `re.compile` combined with `workspace`, tree-wide |
| `"default"` comparison | `workspace_registry.py:376` | a label comparison, not an id check |

Tree-wide, `RESERVED_WORKSPACE_IDS` has exactly four hits: the definition at `:48`, the check at `:319`, and two tests. So the only guard on the namespace key is a single frozenset membership test, on one of several code paths, covering one value.

### 2.3 Where it actually creates directories

`ensure_workspace_dirs` is defined at `thoughtmachine/workspace_capabilities.py:279`. It computes `base = _workspace_dir(workspace_id)` at `:308`, calls `base.mkdir(parents=True, exist_ok=True)` at `:312-313`, then seeds `capabilities.json` at `:317-322` and a `Dockerfile` at `:326`.

The path builder is `_workspace_dir` at `workspace_capabilities.py:176-178`:

```
return _user_dir() / "workspaces" / workspace_id
```

There is no validation in it, and `RESERVED_WORKSPACE_IDS` is not consulted anywhere on this path. The docstring at `:299-300` says no subdirectories are created inside the workspace directory, which is consistent with the seeding above, since `capabilities.json` and `Dockerfile` are files; the docstring says nothing about validating the id, and nothing under it does either.

The consequence is direct: any caller can create `~/.thoughtmachine/workspaces/global/`, colliding with the single reserved id, and can create it with whatever content the seeder writes. This is reachable by a typo in an id argument, not only by an attacker, and it leaves no record that a reserved namespace was used. Twenty-plus unvalidated call sites reach these functions:

| File | Lines |
|---|---|
| `web_ui/backend/workspace_routes.py` | 365, 379, 394, 444, 794, 819, 912, 922, 951, 1226, 1297, 1339, 1447, 1622, 1663, 1739 |
| `web_ui/backend/server.py` | 836, 1655, 3129, 3140, 3331, 3538 |
| `web_ui/backend/session_routes.py` | 141 |
| `setup_workspace.py` | 94 |

### 2.4 The validation model already exists in this repo

`_validate_credential_name` at `web_ui/backend/global_routes.py:339` rejects `/`, `\`, `.`, `..` and names longer than 128 characters for credential names. A workspace id becomes a path segment in exactly the same way a credential name does, under the same root, and carries the same hazards. The check does not need to be invented, only reused: applied at `_workspace_dir` it would be inherited by every call site in the table above, and applied at `ensure_workspace_dirs` it would cover the bootstrap path directly.

### 2.5 Why this section comes before the clusters

If the namespace key can be `global`, `..` or an empty string, then "workspace-scoped" is not a property any row in this manifest can honestly claim, and the clusters in section 3 would be describing a boundary that the key does not enforce. Ownership attribution assumes a key that identifies exactly one namespace; here the key identifies one namespace only as long as nobody passes a surprising string.

This is also the cheapest finding in the document to act on and the most reversible: it is a single validation point under a function every caller already routes through, it changes no schema, and it can only reject ids that were never valid.

## 3. The eight ownership clusters (50 rows)

The 50 rows with `Verification == NONE` are not 50 independent facts. Grouped by the code path that owns them they collapse to 8 clusters: 23 + 5 + 3 + 9 + 3 + 1 + 3 + 3 = 50. The cluster is the right unit of ownership because a single writer can be answerable for many rows, and because a cluster can be adopted by one owner while a row cannot meaningfully be adopted on its own. Each subsection below gives the matrix lines, the owner site as recorded, and then the five fields the matrix does not have.

Where a field could not be established from the code, it reads `unknown`. That word is doing real work in this section: it marks the limits of this pass rather than a blank to be filled by assumption.

### 3.1 C1 - WebSocket dispatcher (23 rows)

| Matrix lines | Count | Fact | Owner site |
|---|---|---|---|
| L132, L134-L147 | 15 | as recorded in the matrix (not restated here) | `websocket_endpoint server.py:1231` |
| L150, L152-L154 | 4 | as recorded in the matrix | `websocket_endpoint server.py:1231` |
| L157-L160 | 4 | as recorded in the matrix | `websocket_endpoint server.py:1231` |

The owner site is a dispatcher, not a writer: it owns the entry point, while each command's write lands elsewhere, mostly in the session store. Routing ownership and write ownership are therefore different things, and the cluster's writer column is honestly `unknown` for most rows.

| Field | Value |
|---|---|
| Writer site | `unknown` (per command; the dispatcher at `server.py:1231` routes rather than writes) |
| Reader site(s) | `unknown` |
| Deleter site | `unknown` |
| Realm | vault, via the session store |
| Namespace key | workspace_id + session_id |
| Status | 22 owned-untested, 1 missing |

L137 is one of the 6 MISSING rows (see section 4.2).

### 3.2 C2 - session HTTP (5 rows)

| Matrix line | Fact / entry point | Owner site |
|---|---|---|
| L174 | create_session | `session_routes.py:107` |
| L175 | list_sessions | `session_routes.py:207` |
| L176 | get_session | `session_routes.py:261` |
| L178 | session permissions | `session_routes.py:430` |
| L179 | session permissions | `session_routes.py:478` |

This is the only cluster where the writer/reader split is clean. `create_session` writes; `list_sessions` and `get_session` read; the permission pair writes through `write_session_permissions` at `thoughtmachine/permission_store.py:380` and reads through `read_session_permissions` at `:268`.

| Field | Value |
|---|---|
| Writer site | `create_session session_routes.py:107`; permissions via `write_session_permissions permission_store.py:380` |
| Reader site(s) | `list_sessions session_routes.py:207`, `get_session session_routes.py:261`, `read_session_permissions permission_store.py:268` |
| Deleter site | `unknown` at route level; a store-level `delete_session` exists at `session/store.py:851` |
| Realm | `<vault>/workspaces/<id>/sessions/` |
| Namespace key | workspace_id + session_id |
| Status | 2 existing (L178, L179), 3 owned-untested |

### 3.3 C3 - worker control (3 rows)

| Matrix line | Fact / entry point | Owner site |
|---|---|---|
| L170 | pause_worker | `workspace_routes.py:1305` |
| L171 | resume_worker | `workspace_routes.py:1417` |
| L172 | stop_all_workers | `workspace_routes.py:1205` |

These three have an owner site but the fact they depend on does not: where the worker registry key lives and who may write it has no owner at all. That is the same fact recorded as `UNOWNED` at matrix L209 (`Worker registry key`). So this cluster is 3 of the 6 MISSING rows for one shared reason, not three separate ones.

| Field | Value |
|---|---|
| Writer site | `workspace_routes.py:1305`, `:1417`, `:1205` (control surface), over an unowned registry key |
| Reader site(s) | `unknown` |
| Deleter site | `unknown` |
| Realm | `<vault>/workspaces/<id>/workers.json` |
| Namespace key | workspace_id |
| Status | 3 missing |

### 3.4 C4 - containers (9 rows)

| Matrix line(s) | Fact / entry point | Owner site |
|---|---|---|
| L180-L187 | container lifecycle and record writes (per-row text in the matrix) | `server.py:4662`, `server.py:4764`, `server.py:4795`, `server.py:4837`, `server.py:5641`, and `_record_user_action server.py:5179`, `server.py:5187`, `server.py:5195` |
| L188 | container record route | `container_record_routes.py:130`, marked `UNOWNED` |

The 8 owner sites above are recorded against L180-L187 in the matrix; that per-row mapping is not restated here. The realm is host Docker plus the container record store, not the vault - this is the one cluster whose writer sits outside the vault entirely, which is why the manifest needs a realm column and not just a namespace key.

| Field | Value |
|---|---|
| Writer site | `server.py:4662`, `:4764`, `:4795`, `:4837`, `:5641`; `_record_user_action server.py:5179`, `:5187`, `:5195` |
| Reader site(s) | `unknown` |
| Deleter site | `unknown` |
| Realm | host Docker daemon + container record store (outside the vault) |
| Namespace key | workspace_id + container id |
| Status | 8 owned-untested, 1 missing (L188) |

### 3.5 C5 - global, health and credentials (3 rows)

| Matrix line | Fact / entry point | Owner site |
|---|---|---|
| L190 | `/health` | `server.py:3893` |
| L191 | `/api/global/summary` | `global_routes.py:322`, marked `UNOWNED` |
| L192 | `/api/credentials` | `list_credentials global_routes.py:391` |

This cluster mixes the one row with no owner (L191) with a row that has a real test (L192) in the same three rows. It is the clearest single illustration that row-level status is not cluster-level status: adopting the cluster is not the same task as adopting its rows.

| Field | Value |
|---|---|
| Writer site | `/health`: none (read-only); `/api/global/summary`: `unknown`; credentials: `_write_credential global_routes.py:375` |
| Reader site(s) | `global_routes.py:322`, `list_credentials global_routes.py:391` |
| Deleter site | credentials: `delete_credential global_routes.py:445` (`target.unlink()` at `:456`); others `unknown` |
| Realm | global (credentials under `<vault>/credentials/global`), plus process health |
| Namespace key | none - this cluster is global |
| Status | 1 existing (L192), 1 missing (L191), 1 owned-untested (L190) |

### 3.6 C6 - event log (1 row)

| Matrix line | Fact / entry point | Owner site |
|---|---|---|
| L203 | event_log writer | `_on_event agent/logging/event_logger.py:142` |

| Field | Value |
|---|---|
| Writer site | `_on_event agent/logging/event_logger.py:142` |
| Reader site(s) | `unknown` |
| Deleter site | `unknown` |
| Realm | vault-global log file |
| Namespace key | none - one file shared by every session |
| Status | 1 owned-untested |

The namespace key is not a workspace at all, so this row is workspace-visible without being workspace-scoped. Section 6 takes that up.

### 3.7 C7 - CheckSystem (3 rows)

| Matrix line | Fact / entry point | Owner site |
|---|---|---|
| L206 | `_query_workers` | `check_system.py:765` |
| L207 | `_query_containers` | `check_system.py:492` |
| L208 | `_check_path_allowed` | `check_system.py:268` |

The cluster is read-only: no writer, no deleter, three readers. A read-only fact still needs an owner, because the leak surface is the read.

| Field | Value |
|---|---|
| Writer site | none (read-only) |
| Reader site(s) | `check_system.py:765`, `check_system.py:492`, `check_system.py:268` |
| Deleter site | none (read-only) |
| Realm | `unknown` - the three queries span the vault and the host |
| Namespace key | `unknown` |
| Status | 3 owned-untested |

### 3.8 C8 - tool boundaries (3 rows)

| Matrix line | Fact / entry point | Owner site |
|---|---|---|
| L210 | git boundary | `_validate_repo_root git_info_tool.py:474` |
| L211 | host bash cwd | `HostBashTool.execute tools/host_bash_tool.py:227` |
| L212 | KB scope | `KnowledgeBaseTool.execute:244` |

These three validate rather than write, so the writer column is `none`. L210 is one of the 4 EXISTING rows. L211 is the one row where the honest verdict is that there is no scoping to test: `tools/host_bash_tool.py` contains zero occurrences of cwd, `dir=`, directory, workdir or chdir, so the tool inherits the host process cwd.

| Field | Value |
|---|---|
| Writer site | none (all three validate) |
| Reader site(s) | `git_info_tool.py:474`, `tools/host_bash_tool.py:227`, `KnowledgeBaseTool.execute:244` |
| Deleter site | none |
| Realm | repo on disk; host process; KB filesystem |
| Namespace key | repo root (git); none (host_bash); workspace_id (KB) |
| Status | 1 existing (L210), 2 owned-untested |

### 3.9 Cluster summary

| Cluster | Rows | Writer site | Reader site(s) | Deleter site | Realm | Namespace key | Status |
|---|---|---|---|---|---|---|---|
| C1 WebSocket dispatcher | 23 | `unknown` (dispatcher routes at `server.py:1231`) | `unknown` | `unknown` | vault, via session store | workspace_id + session_id | 22 owned-untested, 1 missing |
| C2 session HTTP | 5 | `session_routes.py:107`; `permission_store.py:380` | `session_routes.py:207`, `:261`; `permission_store.py:268` | `unknown` (store-level only) | `<vault>/workspaces/<id>/sessions/` | workspace_id + session_id | 2 existing, 3 owned-untested |
| C3 worker control | 3 | `workspace_routes.py:1305`, `:1417`, `:1205` | `unknown` | `unknown` | `<vault>/workspaces/<id>/workers.json` | workspace_id | 3 missing |
| C4 containers | 9 | `server.py:4662`, `:4764`, `:4795`, `:4837`, `:5641`, `:5179`, `:5187`, `:5195` | `unknown` | `unknown` | host Docker + record store | workspace_id + container id | 8 owned-untested, 1 missing |
| C5 global/health/creds | 3 | `global_routes.py:375` (credentials); others none or `unknown` | `global_routes.py:322`, `:391` | `global_routes.py:445` | global | none | 1 existing, 1 missing, 1 owned-untested |
| C6 event log | 1 | `agent/logging/event_logger.py:142` | `unknown` | `unknown` | vault-global log file | none | 1 owned-untested |
| C7 CheckSystem | 3 | none (read-only) | `check_system.py:765`, `:492`, `:268` | none | `unknown` | `unknown` | 3 owned-untested |
| C8 tool boundaries | 3 | none (validate only) | `git_info_tool.py:474`, `tools/host_bash_tool.py:227`, `KnowledgeBaseTool.execute:244` | none | repo, host process, KB filesystem | repo root / none / workspace_id | 1 existing, 2 owned-untested |

Two clusters have a row with no writer at all (C5 L191, C4 L188), and three clusters have nothing deletable to own (C1, C6, C7), because their facts are reads or an append-only log.

## 4. Conversion verdicts: 4 existing, 40 needs-new, 6 missing

Of the 50 rows, **4 already have adequate coverage, 40 need a new test, and 6 cannot be tested as scoped today because nothing owns the fact**.

The three verdicts are not degrees of the same thing. "existing" means an owner site and a demonstrating test both exist. "needs-new" means the owner exists and the demonstration does not. "missing" means there is no owner to attribute the behaviour to, so there is nothing for a test to pin down.

### 4.1 The 4 with existing coverage

| Matrix row | Fact | Existing owner test |
|---|---|---|
| L178 | session permissions (write) | `tests/test_session_permissions_persistence.py` |
| L179 | session permissions (read) | `tests/test_session_permissions_persistence.py` |
| L192 | `/api/credentials` | `tests/test_credentials_crud.py` |
| L210 | git boundary | `tests/test_git_permissions.py` |

### 4.2 The 6 missing

| Matrix row | Fact | Why missing |
|---|---|---|
| L137 | `Bridge.stop bridge.py:1674` | no owner established for the stop path in this pass |
| L170 | pause_worker | same unowned fact as matrix L209 `Worker registry key` |
| L171 | resume_worker | same unowned fact as matrix L209 |
| L172 | stop_all_workers | same unowned fact as matrix L209 |
| L188 | `container_record_routes.py:130` | the matrix records the site as `UNOWNED` |
| L191 | `/api/global/summary global_routes.py:322` | the matrix records the site as `UNOWNED` |

L170, L171 and L172 are the same unowned fact as matrix L209 `Worker registry key`, so the 6 rows are four distinct ownership gaps, not six. "Missing" here means no owner, not no test: writing a test first would encode current behaviour as correct and call the result ownership.

### 4.3 The 40 needs-new

| Cluster | Rows needing new tests | Nearest existing suite to extend | Note |
|---|---|---|---|
| C1 WebSocket dispatcher | 22 | `unknown` (no WebSocket dispatcher suite among the suites named in this pass) | 23 rows minus the missing L137 |
| C2 session HTTP | 3 | `tests/test_session_store.py` | 5 rows minus the 2 existing |
| C3 worker control | 0 | none - all three are missing | adopt an owner before writing a test |
| C4 containers | 8 | `unknown` (container lifecycle has no suite named in this pass) | 9 rows minus the missing L188 |
| C5 global/health/creds | 1 | `unknown` for `/health`; `tests/test_credentials_crud.py` cannot extend to it | 3 rows minus L191 and the existing L192 |
| C6 event log | 1 | `tests/test_event_logger.py` (writes), `tests/test_event_log_scoped.py` (scoping) | the scoping suite's docstring states it is one vault-global file shared by every session |
| C7 CheckSystem | 3 | `unknown` - no dedicated per-query suite | the three queries read; the leak surface is the read |
| C8 tool boundaries | 2 | `tests/test_wave10a_kb_filesystem_grain.py` for L212 | L211 host_bash cwd has no suite to extend, because `tools/host_bash_tool.py` never sets cwd |

Arithmetic: 22 + 3 + 0 + 8 + 1 + 1 + 3 + 2 = 40, and 4 existing + 40 needs-new + 6 missing = 50.

Other suites relevant to neighbouring rows, none of which cover these 40 directly: vault gc `tests/test_vault_gc.py` and vault seeding `tests/workspace/test_vault_seeding.py`; registry and lifecycle `tests/test_workspace_registry.py` and `tests/test_workspace_lifecycle.py`; the tool executor gate `tests/test_tool_executor.py` (448 lines, gate integration); credentials `tests/test_credentials_crud.py`.

`tests/` holds 301 entries across 16 subdirectories, so 40 new tests is a real number rather than a rounding error. It is also a test-debt count and not a correctness claim: it says nothing about whether the code is right, only that nobody has demonstrated that it is.

## 5. Realm map: what actually lives where

Section 3 uses a `Realm` field for all 8 clusters without ever defining it, and section 3.4 says explicitly that the manifest needs a realm column and not just a namespace key. This section defines the term against the tree.

A realm is the storage boundary a fact is persisted into. The namespace key is what distinguishes two facts inside one realm. The two are independent, and the interesting cases in this codebase are the ones where they come apart: a realm can hold facts for every workspace with no key at all (`<vault>/logs/`), and a key can be present on the record while absent from the path that holds it (`host_bash_audit.jsonl`).

### 5.1 The vault root and its seven compartments

`vault_root()` is defined at `thoughtmachine/vault.py:29`. It honours `THOUGHTMACHINE_VAULT_ROOT` at `:34-37` and otherwise returns `~/.thoughtmachine` at `:37`. It is resolved at call time by every consumer examined in this pass, so the root is not an import-time constant.

`VAULT_SUBDIRS` at `vault.py:45-53` names seven compartments: `system`, `user`, `credentials`, `workspaces`, `global`, `state`, `logs`. `ensure_vault_structure` at `vault.py:56` creates them, at `vault.py:85` (`mkdir`) and `:86` (`chmod 0o700`); the root itself is `0o700` at `:78-79`.

Which of the seven this pass found a writer for:

| Compartment | Writer established in this pass | Namespace key |
|---|---|---|
| `workspaces/<id>/` | `ensure_workspace_dirs workspace_capabilities.py:279` | workspace_id |
| `state/` | `WorkspaceRegistry._save workspace_registry.py:343`; `SessionRegistry._save session_registry.py:57-60`; `save_open_sessions session/store.py:961-975` | none (section 6.2) |
| `logs/` | `EventLogger._writer_loop agent/logging/event_logger.py:176`; `tools/host_bash_tool.py:162-163` | none (section 6.1) |
| `credentials/global/` | `_write_credential web_ui/backend/global_routes.py:375` | none - global by construction (`:352-354`) |
| `system/`, `user/`, `global/` | `unknown` - no writer established in this pass | `unknown` |
| `<vault>/sessions/` (legacy, not in the seven) | `session/store.py:517` | none (section 6.3) |

The last row is the first finding of this section: the directory that holds unscoped session records is created and written by the session store but is not a declared compartment, so `ensure_vault_structure` does not create it and nothing in `VAULT_SUBDIRS` names it. It is created lazily on the first unscoped write, by the store's own `_ensure_dirs`, from the default at `session/store.py:129`.

### 5.2 The per-workspace realm

The path builder is `_workspace_dir` at `workspace_capabilities.py:176-178`, returning `<vault>/workspaces/<workspace_id>`. There is no other component: the workspace id is the last segment and there is no session segment below it in the config realm.

Seeded contents, all written by `ensure_workspace_dirs` at `:279`: the directory itself at `:312-313`, `capabilities.json` at `:317-322`, `Dockerfile` at `:326`, and - per the function docstring at `:289-297` - `requirements.txt`, `domain_allowlist.json`, `workers.json`, `mcp_servers.json` and `config.json`.

The session realm hangs below the same directory but is not seeded by it:

| Path | Site |
|---|---|
| `<vault>/workspaces/<ws>/sessions/<session_id>.json` | `session/store.py:167` |
| `<vault>/workspaces/<ws>/sessions/<name>_<short_id>.json` | `session/store.py:308` |
| `<vault>/workspaces/<ws>/sessions/<session_id>/permissions.json` | `thoughtmachine/permission_store.py:150-156` |
| `<vault>/workspaces/<ws>/config.json` | `thoughtmachine/permission_store.py:305` |

The docstring at `workspace_capabilities.py:299-300` states that no subdirectories such as `sessions/` are created inside the workspace directory, and that is accurate about what the seeder does. The consequence is worth stating plainly: the workspace directory's contents are defined by whichever module writes first, not by one owner. The seeder owns six files and the store owns the seventh entry.

### 5.3 Realms outside the vault

Four facts in this manifest do not land in the vault at all. They are carried here because section 3's realm column would otherwise be a synonym for "vault", which it is not.

| Realm | What holds it | Evidence already recorded |
|---|---|---|
| host Docker daemon | container lifecycle and records | `server.py:4662`, `:4764`, `:4795`, `:4837`, `:5641` (section 3.4) |
| host process | working directory of `host_bash` | `tools/host_bash_tool.py:227` (section 3.8); the tool never sets cwd |
| repo on disk | git operations | `_validate_repo_root git_info_tool.py:474` (section 3.8) |
| KB filesystem | `KnowledgeBaseTool` | `tools/knowledge_base.py:125`; workspace scope is rooted in the workspace repo, global scope in the vault global directory, per the `scope` field of the tool's request model |

None of these four has a namespace key that the vault can enforce; the container and git cases key on an id or a path supplied to them, and `host_bash` keys on nothing.

### 5.4 What the map shows

Three observations, none of which is a code change.

First, realm and key are independent. `workspaces/<id>/` is keyed and per-workspace; `state/` and `logs/` are unkeyed and shared; `credentials/global/` is unkeyed by design and says so in its path. A manifest that recorded only the namespace key would have filed the credential store and the event log under "global", which is a name for the reserved id in `RESERVED_WORKSPACE_IDS` at `workspace_registry.py:44-48` and not a description of either.

Second, the boundary is the directory, not the key. The comment inside `_find_session_path` at `session/store.py:210-220` states the position directly: workspace directories are the isolation boundary, and the legacy directory is shared by construction. That is the most precise statement of the isolation model found in this pass, and it is a comment rather than a guard.

Third, three of the realms above are written without a workspace key in the path at all: `logs/` (section 6.1), `state/` (section 6.2) and the legacy `<vault>/sessions/` (section 6.3). They are the subject of section 6, because a writer into such a realm cannot be attributed to one workspace by its path and must carry the attribution in the record or not at all.

## 6. Cross-workspace writers that no realm owns

A writer is cross-workspace here when the path it writes is the same for two different workspace ids. The test is the path, not the intent: a writer that is careful about the record it writes still writes every workspace's record into one file. Four groups qualify, and one of them writes an attribution field that is always `None`.

### 6.1 The event log: one file, session key only

There is exactly one event log path for every workspace. `EventLogger.__init__` resolves `vault_log_dir = str(get_log_root())` at `agent/logging/event_logger.py:56` and fixes `event_log.jsonl` under it at `:58`, with the comment at `:55` reading "always use vault path". `get_log_root` at `agent/_log_root.py:22-30` returns `<root>/logs` and takes no workspace argument. The constructor accepts `workspace_path` at `:42`; it is not used to build the path.

The record carries a session key and a workspace key. `_on_event` at `:142` builds `"session_id": getattr(event.metadata, "session_id", None)` at `:163` and `"workspace_id": getattr(event.metadata, "workspace_id", None)` at `:167`. The comment at `:161-165` states that `workspace_id` is always `None` for now because `EventMetadata` has no such field.

That comment is checkable, and it is correct: `EventMetadata` at `agent/events.py:103-109` declares `event_id`, `timestamp`, `source`, `session_id` and `turn`. The string `workspace_id` does not occur anywhere in `agent/events.py`. So the write side has a slot for a workspace key, the key is always empty, and the slot's presence makes the record look attributable when it is not.

Rotation is per file: `_maybe_rotate` at `:81-86` renames at 5 MB and keeps one backup `event_log.jsonl.1`. Because the file is shared, a busy workspace can rotate another workspace's events out of the live file. Status: writer owned (`_on_event :142`), workspace attribution `no-owner`, deleter `unknown` (rotation at `:86` is a replace, not a delete).

### 6.2 The two state registries

`session_registry.json` lives at `<vault>/state/session_registry.json` (`session/session_registry.py:22-23`). Its module docstring at `:1-6` describes it as tracking "every session across all workspaces" for "cross-workspace lookup". `_save` at `:57-60` writes the whole dictionary, so every save rewrites every workspace's entries.

`rebuild_from_disk` at `:111-140` is the clearest cross-workspace writer in the tree: it scans the legacy directory at `:115` and `:120-124`, scans every workspace directory at `:116` and `:127-134`, and then writes the combined result at `:140`. One call reads across all workspaces and persists one file with no workspace segment in its path.

`open_sessions.json` lives at `<vault>/state/open_sessions.json` (`session/store.py:938-940`) and is written by `save_open_sessions` at `:961-975`. Its content is a bare list of session ids, so it holds ids belonging to every workspace with nothing marking which workspace an id came from.

Neither file appears in any cluster in section 3. That is not an oversight in the conversion: no row of `docs/workspace_isolation_matrix.md` names either file, so neither could be among the 50. They surfaced by following the `state/` compartment in section 5.1, which is the reason the realm map was written before this section rather than after it.

### 6.3 The legacy session realm

The store has two write targets for one record type. `_resolve_session_path` returns the workspace-scoped path at `session/store.py:167` and the legacy path under `<vault>/sessions/` when no workspace id is given. The save path makes the same choice in one expression at `session/store.py:517`: the workspace directory when `workspace_id` is truthy, `self.sessions_dir` otherwise.

Reads are asymmetric by design. `_find_session_path` at `store.py:210-220` gives a scoped caller this workspace's directory and then the legacy directory, and the comment there states that no other workspace's directory is enumerated. The unscoped branch at `:219-225` enumerates `workspaces/*/sessions` for every workspace. The path cache is keyed on `(workspace_id, session_id)` at `:117-119`, so an unscoped resolution is never served to a scoped caller.

Records in the legacy directory carry no workspace id at all, as the `_find_session_path` docstring at `:187` states. So the realm holds session facts for an unspecified workspace, and the only thing distinguishing them from a scoped record is which directory they are in.

### 6.4 What this leaves unowned

| Writer | Realm | Key on path | Key on record | Status |
|---|---|---|---|---|
| `_on_event agent/logging/event_logger.py:142` | `logs/` | none | `session_id` `:163`; `workspace_id` always `None` `:167` | owned-untested; attribution `no-owner` |
| `tools/host_bash_tool.py:162-163` | `logs/` | none - `host_bash_audit.jsonl` at `:153` | `workspace_id` at `:156` | owned-untested; attribution owned |
| `SessionRegistry.rebuild_from_disk :111-140` | `state/` | none | `unknown` (per-entry field list not established) | owned-untested |
| `save_open_sessions session/store.py:961-975` | `state/` | none | none | owned-untested |
| `SessionStore` legacy write `session/store.py:517` | `<vault>/sessions/` | none | absent (docstring `:187`) | owned-untested |
| `vault_gc` categories `stale_workspaces` (`vault_gc.py:172`) and `orphan_sessions` (`:174`) | all of the above | n/a | n/a | deleter not established in this pass |

Two rows in that table are the ones that matter for the ledger. The event log and the legacy session directory both hold facts for every workspace, both are written by an owned line, and neither can answer "which workspace is this fact from" from the path. The audit log answers it from the record; the event log has the field and fills it with `None`. That difference is the whole of the cross-workspace attribution gap at the write side, and the read side is where it becomes visible.

### 6.5 `agent_config.json` is still written, and no realm owns it

The code claims the file is retired. The `set_default_config` docstring at `web_ui/backend/server.py:1973-1977` states that "the legacy `~/.thoughtmachine/agent_config.json` write was removed; that file is now read-compat only". The handler is `set_default_config` at `server.py:1966`, and it persists only `GLOBAL_DEFAULT_KEYS` into `user/defaults.json`.

The claim does not hold repo-wide. A second write path reaches the same file:

| Step | Site | What it does |
|---|---|---|
| Entry | `agent/presenter/state_bridge.py:84` | `StateBridge.save_config` |
| Target selection | `state_bridge.py:109` | `save_path = path or self.config_path` |
| Hand-off | `state_bridge.py:163` | `save_config(overlay, save_path)` |
| Second caller | `state_bridge.py:193` | `reset_config_to_factory` |
| Terminal writer | `agent/config/loader.py:382` | `def save_config(...)` |
| Temp file | `loader.py:412` | `tmp_path = config_path + '.tmp'` |
| Open | `loader.py:413` | `open(tmp_path, 'w')` |
| Serialise | `loader.py:414` | `json.dump` |
| Commit | `loader.py:415` | `os.replace` |
| Default target | `state_bridge.py:28` | `vault_root() / 'agent_config.json'` |

So the read-compat claim holds for the config-apply path only. On the presenter and bridge path the file is still live-written, and the default target carries no workspace component at all - it is one file at the vault root - which makes it a cross-workspace writer that no realm owns. It is not a member of `VAULT_SUBDIRS` (`vault.py:45-53`), so `ensure_vault_structure` does not create it and section 5.1 does not list it.

This is the case that most clearly justifies the whole document. The matrix records it as a `NONE` row with a plausible owner site, so a per-fact verifier would have marked it `NONE` and moved on without ever noticing that a docstring contradicts the writer that is still reachable.

### 6.6 host_bash inherits the host process cwd

`HostBashTool.execute` at `tools/host_bash_tool.py:227` never sets a working directory. The file contains zero occurrences of cwd, `dir=`, directory, workdir or chdir, so the tool inherits the host process cwd and there is no workspace boundary to test. This is matrix row L211.

Its audit sink is the same pattern as section 6.4. `host_bash_audit.jsonl` is fixed at `tools/host_bash_tool.py:153`, its record carries `workspace_id` at `:156`, and the append happens at `:162-163`.

This is the sharpest contrast in the document. The record knows which workspace it came from, the file it lands in does not, and the process it ran in was never told either. It is also the only matrix row whose correct status is neither `owned-untested` nor `no-owner` but "no boundary exists", which is why it is counted in section 4.3 as needing a new test rather than being listed in section 4.2, and why no test can close it.

## 7. Open questions

These are recorded as questions, not findings. Each one is a decision someone has to make, and none is resolved by this document.

1. **`agent_config.json` (section 6.5).** Is the presenter-path write intended to survive? If it is, which realm owns it - `user`, a workspace, or one vault-global file with an explicit scope field? If it is not, the docstring at `web_ui/backend/server.py:1973-1977` is currently the only thing telling a reader the write was removed, and per section 6.5 it is wrong.

2. **The credential realm (section 5.4).** `<vault>/credentials/<workspace_id>/` is read but has no writer anywhere in the tree: `agent/credentials/injector.py:68` reads it, and `web_ui/backend/global_routes.py:354` writes only the `/global` child. Is that deliberate external provisioning, or an unfinished realm? A read-only realm that nothing populates is indistinguishable from a bug at the read site.

3. **Credential grain at injection.** `agent/core/tool_executor.py:606-628` injects credential values with no `credential` grain check inside that block; the only guard is `if ws_id:` at `:618`. Meanwhile `security/security_gate.py:97` defaults the `credential` resource to banned and `security/resource_catalog.py:124` declares it. Is the bypass intentional, on the grounds that injection is not a tool call, or is it an oversight? This is recorded as a question and not as a claim.

4. **host_bash cwd (section 6.6).** Should `HostBashTool` pin a working directory, and to what - the workspace root, a task directory, or nothing? Any answer also decides whether matrix L211 can ever be verified, because the row currently describes an absence rather than a behaviour.

5. **The four `UNOWNED` rows** - matrix L188, L189, L191 and L209. Who adopts them? L209 in particular is the shared cause of the L170, L171 and L172 entries in section 4.2, so adopting one row closes four.

6. **Two vault-root resolvers.** `vault_root()` at `thoughtmachine/vault.py:29-39` and the hand-rolled mirror `_resolve_vault_root` at `infra/resource_container_manager.py:154`. Which is authoritative, and does the mirror track the `THOUGHTMACHINE_VAULT_ROOT` override that `vault_root()` honours? Two resolvers that disagree by one directory is the kind of defect no per-fact test would name.

7. **`sessions/` at the vault root.** `thoughtmachine/workspace_lifecycle.py:204` creates it, and it is not a member of `VAULT_SUBDIRS` at `vault.py:45-53`. Should it become a declared child, or should that creation site stop creating it at the root? Either answer changes which realm the legacy fallback at `session/store.py:168` belongs to.

## 8. What this changes, and the smallest first steps

This is not a plan of record and nothing here is a commitment. It is the ordered set of moves that would make the rest of the manifest true, smallest and most reversible first.

1. **Validate the namespace key.** Apply a workspace-id check modelled on `_validate_credential_name` at `web_ui/backend/global_routes.py:339` at `_workspace_dir` in `thoughtmachine/workspace_capabilities.py:176-178`, so that every caller inherits it rather than each route re-checking. This comes first because every other row in the manifest claims to be workspace-scoped, and until the key is validated none of those claims is enforced. It is also the cheapest change in the document: one function, no call-site edits, and it is reversible.

2. **Backfill the two ledgers rather than merge them.** Add `Writer site`, `Reader site(s)` and `Deleter site` columns to the matrix header and backfill only the 8 clusters in section 3, not all 65 rows. The reason to backfill by cluster is that a cluster has one writer site and 23 rows, so the marginal cost per row collapses; the reason to keep the matrix and this document separate is section 1.

3. **Adopt the missing rows before testing them.** Name an owner for the 6 rows in section 4.2 - which is 4 distinct gaps, since L170, L171 and L172 are all the worker registry key at L209 - before anyone writes a test. A test written against an unowned fact encodes current behaviour as correct, which is the opposite of verification.

4. **Treat the 40 as grouped test-debt, not a backlog of 40 items.** The grouped table in section 4.3 is the work list; C1's 22 rows are one dispatch into the session store suite, and C3 is zero rows because it is blocked on step 3.

### 8.1 Sibling work already filed

Two adjacent tickets were filed in the same generation and are relevant here.

(a) Residuals A and B were appended to `.thoughtmachine/working_docs/ticket-bug-paginate-tool-bypasses-required-categories.md` (now 126 lines) on branch `docs/gen48-paginate-residuals` at commit `ebfe2657`. Residual A records `_NEVER_FORWARD = ("workspace_id",)` at `tools/paginate_tool.py:53`, where the injected context comes only from `_CONTEXT_FIELDS`, so `workspace_id` never reaches a paginated tool. Residual B records that `apply_workspace_ceiling` has exactly one non-test call site, `security/security_gate.py:435` inside `cap` (`:416-435`), reached only from `get_effective_permissions` at `:1030-1032`; that `check_required_categories` at `:1483` onward never calls it; and that `_WORKSPACE_RESOURCE_MAP` at `:201-210` has no `ci` key while `_WORKSPACE_CEILING_KEYS`, derived from `security/resource_catalog.py:123`, does, so `ci` is dropped silently at `:297-299`.

(b) `bug/session-workspace-attribution-unresolved` was filed on branch `docs/gen48-session-attribution` at commit `041611d7`, 96 lines.

Residual A is the same class of defect as section 2: a key that is structurally absent from the path it is supposed to scope. The attribution ticket is the same class as section 6: a record that cannot name the workspace it belongs to.

### 8.2 What this document is not

Nothing here is a gate, a resolver, an admission rule or a commitment to a schedule; the deliverables are the cluster table in section 3, the verdict counts in section 4 and the open questions in section 7. The single sentence that should survive: an unverified fact and an unowned fact look identical in a table that has no writer column, and telling them apart is the whole point of the next pass.
