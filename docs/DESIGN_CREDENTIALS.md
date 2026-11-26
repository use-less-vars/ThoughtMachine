# Design: credentials — one secret, one gate, one record

**Status:** design document, decision-driven. Branch `docs/design-credentials`, base `dev` `a2daec9e`. Third of four: turn lifecycle → resources → **credentials** → workers-as-things.
**Consistency obligation:** every structural claim here must agree with `docs/evidence/resources-bindings.md` (sections B and F) and with `docs/evidence/resources-ceilings.md`. Those are MEASURED readings of the code. Where this document disagrees with them, this document is wrong and must be corrected. The rulings R1–R9 it inherits live in `docs/DESIGN_RESOURCES.md` and are referenced, never restated.
**Legend:** `MEASURED` = the code was read and is quoted with a `path:line` anchor. `INFERRED` = reasoning from names, docstrings or adjacent code. `UNMEASURED` = not establishable from this repository. A sentence without an anchor or one of those two words is a rumour.

---

## 1. What a credential is, and what people conflate

A credential is the one object in this system that is **both a resource and an authority**. It carries the right to act against something outside the agent (a capability, a permission grain), and it carries a quota over rate and lifetime (a resource, a ceiling). That is why it comes third in the sequence rather than last: the resource model was needed to describe it, and the worker model cannot be built on top of it until it is describable.

Three different things are called "a credential" in ordinary speech, and conflating them is the origin of most of section 5:

**The secret** — the bytes. A key, a token, a password. It has exactly one legitimate consumer (the client that will present it) and no legitimate reader (the agent that owns it).

**The binding** — which service, which account, which scope, which environment. It is the part a human needs to see in order to know what is configured, and the part that must be nameable, listable and revocable without exposing the bytes.

**The grant** — whether this agent, in this turn, may cause the secret to be presented. It is an authority decision, and like every other authority decision in this repository it belongs at the moment of use, in the consuming code, checked against inputs read at that moment.

Three shape rules follow directly, and every ruling in section 4 is one of them applied to a specific failure:

1. the secret bytes never leave the type that holds them — redaction is a property of the type, not a habit at each log site;
2. the binding is named and enumerable, so a human can answer "what is configured" without the value and an operator can revoke one binding without touching the others;
3. the grant is checked at use against live state, never against a value captured when the process started.

---

## 2. Evidence

Ten surfaces carry secret material in this tree: the eight paths distinguished in `resources-bindings.md` B.11 plus the store's two scopes. **Exactly one of them is gated by a permission grain** (`credential:use`); the other nine are gated by a presence check, a class flag, file permissions, or by nothing at all. Anchors below are as re-measured at HEAD; where a line number moved against the evidence document, section 6.2 records the move and the evidence anchor is the older of the two.

| Path | Carrier of the value | Gated by | Recorded by | (7) can the agent read it back? | Breaks | Source |
|---|---|---|---|---|---|---|
| **P1** `{{credential:<key>}}` in tool arguments | `Secret` (the injector's: masks to `***`, no `reveal()`) inside a deep-copied `tool_args` dict | `credential:use` through `check_required_categories(['credential:use'], effective, …)`, keyed off the **collected placeholder names**, fail-closed on an unavailable gate (`agent/core/tool_executor.py:910-920`, `:922-932`) | one `CREDENTIAL_USE` per distinct name, best-effort (`:952-973`); the refusal is prose in the return dict (`:933-940`) | No value: the type has no `reveal()` (`agent/credentials/injector.py:33-48`). The placeholder **names** are model-visible by construction (`tool_executor.py:881-889`) and arguments are logged pre-injection (argument-log site not re-verified at HEAD; evidence B.10.1) | C7, C1 | B.2 |
| **P2** `{{credential:<key>}}` in `config.api_key` | the injector's `Secret`, written straight onto the field at `agent/core/agent.py:1015` (pydantic assignment bypass, `agent/config/secret.py:25-34`) | **no `credential:use` check at all**; the only precondition is that a session/workspace exists (`agent.py:990-994`) | silent on success; `WARNING 'credential'` with the key name and exception text on failure (`agent.py:1010-1013`) | No: masked by `Secret`, and the provider layer re-wraps a plain key (`llm_providers/base.py:141-151`). One in-process reveal exists at `llm_providers/openai_compatible.py:241-244` | C4, C5, C6 | B.3, F.1 |
| **P3** `inherits_parent_api_key` handover | the live `self.config.api_key` object placed into `tool_args['agent_config']` (`agent/core/tool_executor.py:815-817`) | **a class flag only** (`tools/base.py:130`; the single `True` at `tools/workspace/worker_thread.py:6497`) | `CREDENTIAL_USE` with `credential_name='parent_api_key'`, `source='tool_executor'` (`:818-835`) | Yes for a plain-`str` key: it is a plain dict field the tool owns (INFERRED from the plain assignment at `:815-817`) | C4, C5, C7 | B.4 |
| **P4** provider key in `~/.thoughtmachine/providers.json` | cleartext `str` on disk at the vault root (`agent/config/provider_profile.py:18-31`, `:66-78`) | **nothing at write**: `json.dump(…, indent=2)`, no `0o600` (`:66-78`). Read-by-file-tool is blocked by the vault prefix test (`thoughtmachine/security.py:447`) | no write record; redaction elsewhere is by key name (evidence B.5.5 — the audit sites were not re-verified at HEAD) | No through file tools (`security.py:447`) — except via a workspace **symlink** into the vault, which `os.path.abspath` does not resolve (`security.py:431-437`; LEAK CANDIDATE, INFERRED) | C3, C5 | B.5 |
| **P4b** provider key from the process environment | `os.environ` `str` | the key waterfall plus two refusals that name the owner (`llm_providers/factory.py:88-91`, `:94-95`, `:97-101`) | nothing at read; the refusal names the provider and the env var | Yes, by anything in-process: the value is ambient (`factory.py:88-95`). No agent-facing read path measured | C5, C7 | B.5, F.1 |
| **P5** git credential | dict of plain `str` env values, token in exactly one value (`thoughtmachine/git_auth_env.py:72-131`, `:96`) | presence of `TM_GIT_ASKPASS_SECRET` (`:47`) plus the push refusal (`tools/git_write_tool.py:1213-1219`), `Secret`-rejection (`git_auth_env.py:104-125`), `http.extraheader` refused (`:52`) and `credential.helper=` pinned (`:118`) | the refusal message names the env var only (`git_write_tool.py:1213-1219`); no log site in the module (evidence B.6.5) | Yes in-process: a plain `str` dict, not a `Secret` (`:72-131`). No agent-facing read path measured | C1, C5 | B.6 |
| **P6** MCP `api_key` | plain `str` in the client's `headers` | `api_key_env_var` read, then **warn-if-unset and proceed with `api_key=None`** (`tools/mcp_manager.py:46`, `:267-277`) | the warning line only | Yes in-process (plain `str`). The egress anchors (`tools/mcp_client.py`) were not re-verified at HEAD | C9, C5 | B.7 |
| **P7** ambient environment into child processes | the env dict handed to the child | `CREDENTIAL_SHAPED` strip of inherited keys, and refusal of credential-shaped caller keys unless `credential_inject=True` (`thoughtmachine/host_port.py:103-120`, `:567-571`) | PRE/POST JSON lines in the audit file, names only (`host_port.py:123`) | Yes — that is the egress: the child receives them. The two sibling chokepoints (sandbox `extra_env`, MCP stdio inheritance) were **not re-verified at HEAD** | C4, C7 | B.8 |
| **store** `credentials/<workspace_id>/<key>` | one plain-text file per key; the **filename is the credential name** (`agent/credentials/injector.py:1-9`) | caller-side only; no authorisation inside the store; key validation and per-scope symlink containment (`injector.py:161`, `:142-148`) | nothing — the module imports no logger (evidence B.1.5) | No through file tools (vault block, `security.py:447`), subject to the symlink caveat above | C2, C3, C6, C7 | B.1 |
| **store** `credentials/global/<key>` | as above | as above, plus the UI write boundary (`web_ui/backend/global_routes.py:346-353`, `:379-403`) | nothing in the store; the writer sets `0o700` directories and a `0o600` file (`:388-389`, `:396-398`) and installs it with `os.replace` (`:402`) | No through file tools; listing is a bare name array over the UI route (`:413-428`) | C2, C6, C7 | B.1 |

Every row is `MEASURED` in the evidence document and re-anchored at HEAD, except the four cells marked INFERRED or not-re-verified inline. The model/provider end of the same secrets (factory waterfall, the snapshot, the per-call `update(kwargs)` merge with no caller) is section F and is cited only where a credential path reaches it.

---

## 3. The seven questions

The six questions of `DESIGN_RESOURCES.md` section 3 apply unchanged — declared once, owner, reader, enforcement, record, and whether the gate re-reads at the moment of use. Credentials add a seventh, because they are the only resource whose value must never be shown:

| # | Question | A bad answer sounds like |
|---|----------|--------------------------|
| 7 | Can the agent that uses this secret ever read the value back? | "only through the debug view" |

A credential surface that cannot answer 7 with "no, and here is the code that prevents it" is not a credential surface; it is a secret with a permissions problem.

---

## 4. Rulings

### C1 — One secret type, one mask, one API

There is exactly one representation of secret material in the process, and every consumer takes that type. Two types with two masks and two APIs is two contracts, and the one with no `reveal()` will eventually be given one by someone who needs it.

### C2 — A binding is named by scope, not by filename

A store addressed by directory plus filename, where the file's name *is* the credential's name, has no index, no listing, no lifecycle and no revocation — it has a convention. A binding has an identity that survives being renamed, moved or re-encrypted, and the store can enumerate its bindings without reading any of their values.

### C3 — At rest it is protected, or it is not stored

A secret written to disk in cleartext with default permissions is a decision to treat the filesystem as the security boundary. That may be a legitimate decision, but it must be made explicitly, with the permissions set by the code that writes the file, and it must be visible in the design rather than discovered in an incident. "The vault should be safe" is not a mechanism.

### C4 — The gate is one gate, and it is at the moment of use

Every path that causes a secret to be presented outside the process passes the same grant check, read at that moment. A band where one path is gated and three are not is not a partially-secured system; it is a system whose security is decided by which code path a caller happens to take.

### C5 — No agent reads a secret back

Reveal is a capability: it must be named, gated by its own grain, and recorded. An agent that can read back the secret it uses can exfiltrate it in the same turn it uses it, and every argument about the store's protection at rest becomes irrelevant. If a human needs the value, a human uses a human channel.

### C6 — Rotation and revocation are transitions

A credential captured at bind time cannot notice its own revocation (R4, R8). A binding therefore has a lifetime the code can re-check, and revocation is an operation with an owner and an observer, not the absence of a file.

### C7 — The record is mandatory, and it does not depend on optional infrastructure

An audit event emitted only when the event bus happens to be available is not an audit; it is a best-effort log with a schema. The record for a use, a refusal and a revocation must land somewhere durable and be readable without the process that wrote it.

### C8 — Errors name the credential, never the value

A refusal names the binding and where its secret was expected to come from. It never quotes the secret, or a prefix of it, or its length. This is C1 applied to the error path, and it is the reason C1 exists: a type that cannot print its value cannot leak it into an exception, a config echo or a telemetry field.

### C9 — A missing credential refuses and names an owner

Fail closed, and name the layer that owns the declaration plus the remedy. This shape already exists in one place in this tree and is the model to copy: an unknown provider type and an absent key both refuse with the owner named, instead of falling back to another provider.

---

## 5. Today, path by path

### The store

The store is a directory tree, not a store object: two `Path` objects per injector instance computed from `vault_root()`, a workspace-id **string** as the only key, and one plain-text file per credential whose *filename is its name* (`agent/credentials/injector.py:1-9`). Reads hit the filesystem every call — there is no cache, so a rotation written by `os.replace` is visible on the next call — and `inject()` rewrites only top-level string values that *entirely* match `{{credential:…}}`, leaving an embedded placeholder verbatim (`:118-126`, `:121`, `:53`). The only writer in the tree writes the **global** scope: `0o700` directories, a temp file opened `0o600` and `fchmod`ed, installed with `os.replace` (`web_ui/backend/global_routes.py:379-403`, `:388-389`, `:396-398`, `:402`), while `_credentials_dir()` is hard-coded to `credentials/global` (`:356-358`); name validation is one path segment (`:346-353`), listing is a bare array of names (`:413-428`), deletion is a `DELETE` route (`:448-465`). So the per-workspace scope is declared, readable and documented, and **no code in `agent/`, `tools/`, `security/` or `web_ui/` writes it** — it is populated out of band only (evidence B.1, search set given). The store records nothing: the module imports no logger, and every failure is a `CredentialError` carrying the key name alone (`:159`, `:161`). Breaks C2 (identity is a filename), C3 (the global scope declares its modes, the workspace scope has none because nothing creates it), C6 (revocation is `unlink`) and C7.

**Evidence:** `agent/credentials/injector.py:1-9`, `:53`, `:118-126`, `:142-148`, `:161`; `web_ui/backend/global_routes.py:346-353`, `:356-358`, `:379-403`, `:413-428`, `:448-465`; `resources-bindings.md` B.1.

### P1 — `{{credential:<key>}}` in tool arguments: the one gated path

The names are collected from the arguments **before** replacement, because a placeholder is unrecoverable once replaced (`tool_executor.py:881-889`), and the gate is then keyed off those names rather than off the tool's `required_categories` — the stated reason is that a declaration-based gate lets every tool that declares nothing through (`:891-903`). Enforcement is `check_required_categories(['credential:use'], effective, …)` against the effective profile (`:910-920`), fail-closed when the gate is unavailable or the profile is `None` (`:922-932`), and neither the refusal path nor the exception path constructs an injector (`:946-947`); a `CredentialError` becomes a tool-result string (`:948-950`). What it records is one `CREDENTIAL_USE` per **distinct** name, inside a `try/except` that on failure only logs `ERROR 'core.credentials'` while the injection still stands (`:952-973`), and the refusal itself is prose in the return dict with no structured record, although the module has both a refusal logger name and a refusal-advice table (`:33-34`, `:42-50`). The value handed to the tool is the injector's `Secret`, whose `str()` is `***` and which has no `reveal()` at all (`agent/credentials/injector.py:33-48`). Breaks C7 (best-effort record, no refusal record) and C1 (this is the second `Secret`, not `agent/config/secret.py`'s).

**Evidence:** `agent/core/tool_executor.py:33-34`, `:42-50`, `:881-889`, `:891-903`, `:910-920`, `:922-932`, `:933-940`, `:946-950`, `:952-973`; `agent/credentials/injector.py:33-48`; `resources-bindings.md` B.2.

### P2 — `{{credential:<key>}}` in `config.api_key`

`_resolve_credential_placeholder` (`agent/core/agent.py:978-1015`) logs an `INFO` and returns when there is no placeholder (`:984-988`), raises `CredentialError` when there is no session or workspace (`:990-994`), matches the regex (`:999-1002`), resolves through `CredentialInjector(workspace_id)` (`:1005-1008`) and assigns to `config.api_key` (`:1015`). **There is no `credential:use` check on this path** — `check_required_categories` does not appear in `agent/core/agent.py`, only in the tool executor — so the only precondition is that a workspace exists; the INFERRED consequence is that a session whose `credential` grain is `banned` still has its provider key pulled from the store. Success is silent; failure logs `WARNING 'credential'` with the key name and the exception text and re-raises (`:1010-1013`), never the value. The assignment at `:1015` bypasses pydantic validation, so the field can hold whatever `resolve()` returned, and the value is a snapshot: it is taken at construction and at restart only (`:593`, `:1067`, `:1118`), so rotating the vault file does not change a live agent's key. Breaks C4 (ungated path, same secret), C6 (snapshot) and — at the reveal site, `llm_providers/openai_compatible.py:241-244` — C5.

**Evidence:** `agent/core/agent.py:978-1015` (`:984-988`, `:990-994`, `:999-1002`, `:1005-1008`, `:1010-1013`, `:1015`), callers `:593`, `:1067`, `:1118`; `agent/config/secret.py:25-34`; `resources-bindings.md` B.3, F.1.

### P3 — `inherits_parent_api_key` handover

The declaration is a boolean class attribute, `False` on the base (`tools/base.py:126-130`), and exactly one tool sets it `True` (`tools/workspace/worker_thread.py:6497`); the value handed over is the live `self.config.api_key` placed into `tool_args['agent_config']` **before** the P1 gate block runs (`agent/core/tool_executor.py:814-817`), and the record is a `CREDENTIAL_USE` with `credential_name='parent_api_key'`, `source='tool_executor'` (`:818-835`). There is no gate: no name resolution, no store read, no grain — if the flag is set, the key is in the args, and P1 only inspects `{{credential:…}}` placeholders, so it cannot see this. The child-agent side now documents the asymmetry: the key is present only on inheritance, and a missing key fails with an error naming the provider and `<PROVIDER>_API_KEY` (`worker_thread.py:7178-7187`). The value is snapshotted at dispatch and the child keeps its own copy. Breaks C4 (third ungated path), C5 (a plain dict field the tool owns) and C7 (the record exists but there is nothing to gate).

**Evidence:** `tools/base.py:126-130`; `tools/workspace/worker_thread.py:6497`, `:7178-7187`; `agent/core/tool_executor.py:814-817`, `:818-835`; `resources-bindings.md` B.4.

### P4 and P4b — the provider key on disk, and in the environment

`~/.thoughtmachine/providers.json` holds `ProviderProfile.api_key: str = ''` in **cleartext**: `save()` is `json.dump(…, indent=2)` on `model_dump()` with no mode applied (`agent/config/provider_profile.py:18-31`, `:66-78`), in contrast to the vault writer's `0o600` (`web_ui/backend/global_routes.py:396-398`); the file sits at the vault **root**, which the file-tool block covers by a `startswith` prefix test (`thoughtmachine/security.py:447`) — so the agent's file tools cannot read it, while the web UI and any host shell can. The block is computed from `os.path.abspath` **without following symlinks** (`:431-437`), which is why section 6 lists a symlink as a leak candidate. The manager reads the file once at `__init__` (evidence B.5.6: `_load` has no other caller), so a GUI edit is invisible to a live process. The second source is the process environment: `api_key = api_key or config.get("api_key") or os.getenv(env_var)`, then the `OPENAI_API_KEY` fallback, then a refusal (`llm_providers/factory.py:88-91`, `:94-95`, `:97-101`) — a refusal that names the provider *and* both remedies, which is the shape C9 asks for. Breaks C3 (cleartext, undeclared mode) and C5/C7 on the environment source, which has no record and is ambient to everything in the process.

**Evidence:** `agent/config/provider_profile.py:18-31`, `:66-78`; `thoughtmachine/security.py:431-437`, `:447`, `:459-461`; `llm_providers/factory.py:88-101`; `resources-bindings.md` B.5, F.1.

### P5 — the git credential

One environment key carries it — `ASKPASS_SECRET_ENV = "TM_GIT_ASKPASS_SECRET"` (`thoughtmachine/git_auth_env.py:47`) — inside a **dict of plain `str`** built per call by `build_git_auth_env` (`:72-131`), with the token in exactly one value (`:96`). The structural defences are real and worth naming, because they are the ones the rest of the tree lacks: a `Secret` is rejected by `_require_plain_str` so the mask can never be sent as the token (`:104`, `:111`, `:120`, `:124-125`), `http.extraheader` is refused as a config key so the credential cannot land in argv (`:52`), `credential.helper=` is pinned (`:118`), and `push` refuses outright when the env key is absent (`tools/git_write_tool.py:1213-1219`). What records it: the refusal message names the env var and nothing else, and the module contains no log site at all (evidence B.6.5; INFERRED — no logger call in the file). Lifetime is the moment of use: the dict is built at the call site and nothing is cached. Breaks C1 (a fourth representation — a bare `str` dict with no type) and C5 (in-process code can read it). It does **not** break C4: this is the closest thing in the tree to a gate.

**Evidence:** `thoughtmachine/git_auth_env.py:47`, `:52`, `:55-69`, `:72-131`, `:96`, `:104-125`, `:118`; `tools/git_write_tool.py:1213-1219`; `resources-bindings.md` B.6.

### P6 — the MCP `api_key`

`api_key_env_var` is declared per server (`tools/mcp_manager.py:46`) and read from the process environment at start-up; when it is unset the code logs a warning and **proceeds with `api_key=None`** (`:267-277`) — the one place in this inventory that does not fail closed. The value then travels as a plain `str` into a `headers` dict, so nothing masks it and nothing records the use. Lifetime is the manager object, i.e. a snapshot of the config as loaded once per process. Breaks C9 (an unset key is a warning plus a later remote failure, not a named local refusal), C7 and C1. The egress anchors in `tools/mcp_client.py` were not re-verified at HEAD and are cited from the evidence document only.

**Evidence:** `tools/mcp_manager.py:46`, `:267-277`; `resources-bindings.md` B.7 (egress `tools/mcp_client.py:317-325`, `:429-436`, `:222-224` — not re-verified at HEAD).

### P7 — ambient environment into child processes

The host port is the chokepoint with a declared posture: inherited keys matching `CREDENTIAL_SHAPED` are stripped (`thoughtmachine/host_port.py:103-120`), caller-supplied credential-shaped keys are refused unless `credential_inject=True` (`:567-571`), and the audit file records PRE/POST lines with **names only** (`:123`). It is also the only subprocess path in this tree whose env posture is both written down and enforced in one place; the two sibling paths measured in the evidence document — the sandbox runner's `extra_env` merge and the MCP stdio spawn's `os.environ.copy()` — have no scan at all, and were not re-verified at HEAD. So whether a child process sees the parent's provider key depends on which API spawned it, which is C4's failure exactly. The agent reads these values back trivially: it supplies them.

**Evidence:** `thoughtmachine/host_port.py:103-120`, `:123`, `:567-571`; `resources-bindings.md` B.8 (sibling chokepoints `security/sandboxed_execution.py:236-237`, `tools/mcp_client.py:222` — not re-verified at HEAD).

---

## 6. Gaps, and what they cost

### 6.1 The gaps

| # | Gap | Where (anchor) | What the user or operator would observe | Ruling | Source |
|---|---|---|---|---|---|
| G1 | `credential:use` is enforced on one path; three other paths present the same secret | `agent/core/tool_executor.py:910-920` (gated) vs `agent/core/agent.py:990-994` (P2) and `tool_executor.py:815-817` (P3) | a session whose `credential` grain is `banned` still gets its provider key from the store and still hands it to a child agent; it is refused only when a model writes `{{credential:…}}` in an argument | C4 | B.2, B.3, B.4 |
| G2 | Two `Secret` types, two masks, and the credential-side one has no `reveal()` | `agent/config/secret.py:44`, `:52-60` (`<secret>`) vs `agent/credentials/injector.py:33-48` (`***`, no `reveal`) | which mask a consumer sees, and whether it can get the value at all, depends on which importer it used; a tool handed an injected credential can only ever print `***` | C1 | B.9.1-2 |
| G3 | A reveal capability with no grain and no record | `llm_providers/openai_compatible.py:241-244` (`reveal_secret(config.api_key)` inside a DEBUG f-string), `agent/config/secret.py:72` | DEBUG-level provider logs contain the raw key; the mask is bypassed by one function call that any module can import | C5, C8 | B.10.4 |
| G4 | The masking type has documented holes | `agent/config/secret.py:25-34` (`json.dumps`, `str.__str__`, `.encode()`, `"" + secret`, pydantic assignment bypass) | a config dump or a JSON serialisation of any object holding the key writes it in cleartext; assigning after construction skips the field validator | C1, C8 | B.9.1 |
| G5 | The provider key is on disk in cleartext with default mode | `agent/config/provider_profile.py:66-78` | `cat ~/.thoughtmachine/providers.json` shows the key; every host user who can read the vault can read it | C3 | B.5 |
| G6 | The record of a use is best-effort | `agent/core/tool_executor.py:952-973` | with the event system unavailable, credentials are injected and **nothing durable records it**; the trace is an `ERROR` line naming a logger, not the credential | C7 | B.10.2 |
| G7 | A refusal is a prose string, not a record | `agent/core/tool_executor.py:933-940` against the refusal machinery at `:33-34`, `:42-50` | a denied credential use reaches the model and the human as text and appears in no refusal record; there is nothing to count, alert on or replay | C7 | B.2 |
| G8 | A missing credential is a tool *result*, not a denial | `agent/core/tool_executor.py:948-950` (`CredentialError` → tool-result string, key name inside) | a model can tell a configured binding from an unconfigured one by asking for it and reading the error; a typo in a name looks like an outage instead of a misconfiguration | C8, C9 | B.2 |
| G9 | Bindings cannot be enumerated except as filenames | `agent/credentials/injector.py:1-9`; `web_ui/backend/global_routes.py:413-428` | "what is configured" is answered by listing a directory; renaming a file is a new credential; no scope, owner or lifetime travels with a binding | C2 | B.1 |
| G10 | Rotation is `os.replace`, revocation is `unlink`, and neither is recorded | `web_ui/backend/global_routes.py:402`, `:448-465` | after a revoke, a live agent keeps using the old value until restart (P2, P4 are snapshots); no record says when, or by whom | C6 | B.1, B.3 |
| G11 | `credential:read` has no consumer | evidence B.2 (grep over the tree, worktrees excluded; not re-searched at HEAD) | the "names only" grain cannot be exercised: the only listing surface is the UI route, not a gated tool | C2, C5 | B.2 |
| G12 | MCP proceeds with `api_key=None` | `tools/mcp_manager.py:267-277` | a misconfigured MCP server starts unauthenticated and fails later at request time with a remote error, instead of a local refusal naming `api_key_env_var` | C9 | B.7 |
| G13 | Ambient-environment posture differs per chokepoint | `thoughtmachine/host_port.py:103-120`, `:567-571` (strip and refuse) vs the two siblings read in evidence B.8, **not re-verified at HEAD** | whether a child process sees `OPENAI_API_KEY` depends on which API spawned it; one path redacts, the others may not | C4 | B.8 |
| G14 | The vault block cannot see through a symlink | `thoughtmachine/security.py:431-437` (`os.path.abspath`, comment "without following symlinks"), `:447` | a link created inside a workspace pointing at the global credential directory is not blocked by the vault test and then passes containment (INFERRED, not exercised) | C5, C3 | B.10.7 |
| G15 | The provider key is a snapshot, and its "restart required" classification has no measured reader | set at `agent/core/agent.py:1015`; re-taken only at `:593`, `:1067`, `:1118`. `agent/config/models.py:91-94` declares `provider_type`/`model`/`api_key`/`base_url` `RESTART_REQUIRED` | rotating the vault file changes nothing until restart; the operator's edit looks ignored, and the UI's field category is the only statement that anything was supposed to happen. Whether any runtime reader consumes `FIELD_CATEGORIES` was **not re-verified at HEAD** | C6 | B.3, F.1 |

Two measured counter-examples belong next to the table, because they are what C9 asks for and the band should copy them rather than invent a shape: an unknown provider and an absent key both refuse with the owner named and the remedy enumerated (`llm_providers/factory.py:82-86`, `:97-101`), and the child-agent path now fails with an error naming the provider and `<PROVIDER>_API_KEY` (`tools/workspace/worker_thread.py:7178-7187`).

### 6.2 Anchors that moved against the evidence document

Line numbers in `resources-bindings.md` are readings taken at an earlier revision. Each row below is a claim in this document whose anchor **moved**, or whose meaning changed, and what a reader re-checking it should expect.

| # | What changed | Evidence anchor | Anchor at HEAD | Consequence for this document |
|---|---|---|---|---|
| M1 | `agent/core/tool_executor.py` grew from 1140 lines to 1204; the whole P1/P3 region shifted | B.2 `:850-851`, `:854-857`, `:858-866`, `:868-882`, `:883-895`, `:897-913`, `:923-924`, `:925-927`, `:929-960`; B.4 `:791-793`, `:795-812` | `:873-874`, `:876-880`, `:881-889`, `:891-903`, `:910-920`, `:922-940`, `:946-947`, `:948-950`, `:952-973`; `:814-817`, `:818-835` | every P1 and P3 anchor in sections 2 and 5 is the HEAD number; the deltas run +21 to +27, and the ~55-line block that was `:897-913` is now the two blocks `:922-932` + `:933-940` |
| M2 | A new structural success verdict is forwarded: `if 'success' in tool_execution_result: tool_entry['success'] = …` | not described in B (the region was prose-only there) | `agent/core/tool_executor.py:429-437`; the state-level rejection entry at `:363` | does not change any credential gate; it does mean a refusal's outcome is now legible in `executed_tools` even though the credential refusal itself still has no structured record (G7) |
| M3 | The single `inherits_parent_api_key = True` moved (intervening commit), and its docstring changed | B.4 `tools/workspace/worker_thread.py:6441` | `tools/workspace/worker_thread.py:6497`; new docstring `:7178-7187` | the flag is unchanged; the child-agent error message is now measured, which is why section 6 cites it as a C9 model |
| M4 | `agent/config/provider_profile.py` shifted by +3 | B.5 `:36-40`, `:44-61`, `:63-75` | `:39-43`, `:47-64`, `:66-78` | the cleartext write and the absent `0o600` hold (G5), at `:66-78` |
| M5 | Small shifts with no meaning change | `agent/config/secret.py:19-34`, `:33-70`, `:60-68`, `:72-81`; `thoughtmachine/security.py:434-439`, `:446-448`; `llm_providers/base.py:185-190`; `tools/mcp_manager.py:269-278`; `llm_providers/openai_compatible.py:244`, `:493` | `agent/config/secret.py:25-34`, `:47-72`; `thoughtmachine/security.py:431-437`, `:447`, `:459-461`; `llm_providers/base.py:168-169`; `tools/mcp_manager.py:267-277`; `llm_providers/openai_compatible.py:241-244` (the `:493` reveal site was not found at HEAD) | C1, C3, C5 and C9 claims are unaffected; the reveal-site count for G3 is **one verified site at HEAD, two claimed by the evidence document** |
| M6 | Claims cited here from the evidence document that were **not** re-measured at HEAD | B.2 (`credential:read` grep), B.7 (`tools/mcp_client.py:317-325`, `:429-436`, `:222-224`), B.8 (`security/sandboxed_execution.py:236-237`, `tools/mcp_client.py:222`), B.9.3 (the `agent/logging/*` redaction sites), B.10.1 (the tool-argument log site), B.10.2 (the `_EVENT_SYSTEM_AVAILABLE` gate), B.5.5 (`agent/config/audit.py:31-46`) | UNMEASURED at HEAD | G3, G6, G11 and G13 rest on the evidence document alone; where a HEAD anchor exists it is given, and where none does the row says so instead of guessing |

Anchors that were re-measured and **hold** unchanged: `agent/credentials/injector.py` (all of it), `web_ui/backend/global_routes.py` (all of it), `llm_providers/factory.py:28-41`, `:82-114`, `tools/base.py:126-130`, `tools/git_write_tool.py:1213-1219`, `thoughtmachine/git_auth_env.py:47-131`, `agent/config/models.py:90-140`, `:462-478`, `llm_providers/openai_compatible.py:477-485`, `:764-772`, `:1041`, `agent/core/agent.py:978-1015`.

---

## 7. Order, and what the first commits are

The band order stays as the resources document set it: credentials, then workers-as-things, then the resource band. Within credentials, the order is chosen so that each commit is independently observable and none of them requires the store redesign to be finished first:

1. **C8 + C7** — redaction as a property of the type, and a record that does not depend on the event bus. Cheap, immediately visible, and they make the rest of the band's failures legible instead of silent.
2. **C4** — one gate for every path that presents a secret. This is the security commit proper, and it wants the record from step 1 to prove it fired.
3. **C2 + C3** — one store with an index and declared permissions at rest.
4. **C5** — reveal as a named, gated, recorded capability, or its removal.
5. **C6** — lifetime and revocation, which is where credentials stop being a snapshot and start obeying the invariant.

### 7.1 The invariant, specialised

The resources document's invariant holds here without modification: **no snapshot of authority may outlive the moment it was true.** For a credential it has a second face, which is why this band exists at all: a secret that was valid when it was bound is not thereby valid now, and the code that presents it is the only place that can find out.

---

## 8. What is not measured

This section exists so the next reader does not mistake silence for completeness.

- Keyring and OS-level secret storage: not inventoried in this pass. Whether any code path already delegates to the platform keyring is UNMEASURED.
- Secret handling inside MCP server processes: the parent passes environment and arguments; what a child process does with them is outside this repository.
- The vault's own access control at the filesystem level (ownership, mode bits on the directories) is only partially measured.
- Rotation: no rotation mechanism was found. NOT FOUND is recorded with the search set, which is not the same as absent.

---
