# Design Pass 2b — Permission Bins (vocabulary × bin): shape

Status: design ruling (shape only — no code, no values per resource).
Read at checkout SHA `6fa9c1f224dbcbfe48bc03be601680bb2d6fcf98`.
Method: D.1 template — question → current behavior (cited by symbol) → failure
family (evidence) → candidate shapes → ruling → consequences.
Companion: `design_layers_and_truth_ownership.md` (Pass 2a). **2a's ownership map
IS this document's substrate** — 2b does not re-derive ownership; it takes 2a's
six-layer ownership rulings as given and rules, on top of them, *what a bin is
and which layer owns it*. This file is SEPARATE from 2a and does not amend it.
Discipline: every claim is cited by **symbol + file**, never by line number —
line numbers drift on the next commit; symbols do not. (The one exception is a
quoted comment, marked as a quote.)

---

## 0. Intro — two promises, and which one this keeps

Inv.5 is a **state-layer** promise about legibility, not a security-layer promise
about force. The distinction is the operator's, stated verbatim:

> **Inv.5 (intent):** "An agent cannot be refused by a mechanism it cannot inspect."

> **Main's framing:** "A machine that is safe but opaque is a machine you cannot
> trust. The security layer is what makes the machine safe. The state layer is
> what makes it legible. Inv.5 sits on the second promise, not the first."

Inv.5 as written above is the **named invariant** — the intent. This pass adds its
**enforcement form**, the operation that makes the intent checkable:

> **Inv.5 (enforcement form):** "Any refusal names the mechanism and where the
> reason can be found."

Read as two rules: rule 1 (intent) says a refusal is only legitimate if the
refusing mechanism is *inspectable*; rule 2 (enforcement form) says a refusal
must *say which mechanism* refused and *where the reason lives*. Rule 2 is the
operationalization that lets rule 1 be tested rather than asserted. This document
holds both; the reason a bins shape is relevant to Inv.5 is that a one-dimensional
rank must be *reconstructed* to be inspected, while a bin can be *read directly*.

---

## 1. The question

Does the two-axis model — **vocabulary × bin** — dissolve the one-dimensional
model's residual problems (the three permission-layer "shape" tickets, plus
residue rows R13 and R6), or does it merely relocate them into one-off calls?

Concretely, today's model is **one axis**: each resource carries a single ordinal
level, and every interesting operation (ceiling composition, legacy migration,
defaulting) is an **order comparison** on that ordinal. The two-axis model splits
that single ordinal into (1) a *vocabulary* of concrete actions a resource
exposes, and (2) a *bin* (`deny` / `ask` / `allow`) chosen per action. The claim
to test: the three shape tickets and R13/R6/R9 are all artifacts of the ordinal,
so removing the ordinal (in steady state) removes them.

---

## 2. Current behavior (cited by symbol)

**Vocabulary is a set of ordinal levels, per resource.**
- `security/resource_catalog.py::RESOURCE_CATALOG` — session-grant values, e.g.
  `git: [banned, ask, read, write, write_on_feature_branch]`,
  `host_bash: [banned, ask, allow]`, `container: [True, False]`.
- `security/resource_catalog.py::WORKSPACE_CEILING_VOCAB` — the ceiling-side
  value set (a near-duplicate list; `container` absent, `git` keeps
  `write_on_feature_branch`, `full` dropped). `agent/config/resource_catalog.py::_PERMISSION_LEVELS_BY_RESOURCE`
  is now an **alias** of this dict (Pass-2a antecedent: the B2/M2 consolidation),
  so the ceiling vocab has one holder.

**Composition is a rank comparison.**
- `security/resource_catalog.py::GRANT_LEVEL_RANKS` and `WORKSPACE_CEILING_LEVELS_RANKS`
  — two *derived* ordinal projections of the one vocabulary (2a records this as
  the R11 "correct shape": one vocab, two facts).
- `security/security_gate.py::apply_workspace_ceiling` — reads the workspace
  ceiling, ranks it, ranks the session value, and rewrites the session value when
  the ceiling rank is lower. Special case: `host_bash` uses a **private** rank
  scale duplicated as module-level `_HOST_BASH_CEILING_RANKS` and function-local
  `_HOST_BASH_RANKS` (both `{"banned":0.0,"ask":1.0,"allow":2.0}`), because
  `host_bash` is absent from the shared rank tables.
- `security/security_gate.py::get_effective_permissions` — the composition entry
  point; also applies the workspace boolean `allow_host_resources` via
  `tools/host_resource_policy.py::workspace_allows_host_resources` **after** the
  ceiling, as a second independent gate on `host_bash`.

**Legacy migration is an ordinal fold.**
- `security/resource_catalog.py::fold_legacy_workspace_ceiling` — folds the
  removed git grains (`git_read`, `git_write`) onto the single canonical `git`
  ceiling. Its inner helper `_fold_git_ceiling` carries the docstring quote
  `"Merge a canonical git ceiling level into *result* (strongest wins)."` The
  fold order comes from `_GIT_FOLD_RANKS` (derived from
  `WORKSPACE_CEILING_LEVELS_RANKS`, restricted to the git vocab), and
  `GIT_BREADTH_RANKS` is the integer-breadth projection used so "the fold order
  can never drift from the ceiling scale" (quoted comment).
- `scripts/vault_permission_cleanup_dryrun.py::_merge_git` — a **second,
  independent** implementation of the same legacy git fold, to one ordinal git
  value via a precedence chain (`write_on_feature_branch` → `ask` → `write` →
  `read`). `SESSION_VOCAB` in the same script is now derived from
  `RESOURCE_CATALOG`, but `_merge_git` still re-implements the ordering.

**Defaults are per-resource ordinals, mirrored across languages.**
- `thoughtmachine/security.py::SAFE_DEFAULTS` — machine-wide fail-closed default
  levels (Python).
- `web_ui/frontend/src/store/useStore.js::PERMISSION_DEFAULTS` — the frontend
  mirror of the same defaults (JS), with no value link to the backend (2a: R6).

**The refusal surface is per-resource exact-match.**
- `tools/host_bash_tool.py` — declares `required_categories=["host_bash:allow"]`
  and reads only the executor-injected `effective_permissions` (`_effective_grain`);
  because `host_bash` has no shared-rank entry, the matched value is compared by
  **exact string** (`banned` denies, `ask` prompts, `allow` passes), absent →
  `banned`. A `grain == "ask"` under `is_worker_context` is denied **inside the
  tool**.

---

## 3. Failure family (evidence)

The three shape tickets and the residue rows are all the same family: **a fact
that is stored as an ordinal must be *reconstructed by a rule* to be inspected,
and every such rule is a place to drift or special-case.**

**(T1) `bug/host_bash-ask-ceiling-asymmetric`** — `security/security_gate.py::apply_workspace_ceiling`.
The `host_bash` branch caps `ask` to `banned` — unlike every other resource,
where an `ask` ceiling over a more-permissive session value yields `ask` (a
prompt). Cause is structural, not semantic: `host_bash` has no rank tier
*between* `banned` and `ask`, so the ceiling can only fall to the below-`ask`
tier. The ticket's own one-liner: "the host_bash ceiling branch caps `ask` to
`banned`, unlike every other resource."

**(T2) `design/b3-fold-strongest-vs-smallest`** — `security/resource_catalog.py::fold_legacy_workspace_ceiling`.
When several legacy git grains fold onto one canonical `git` ceiling, the fold
keeps the **strongest** (most permissive) value; the plan prose said
**smallest**. Ruling on record (from the strand close-out): "PRESERVE
strongest-wins. The 12 tests in the B3 set are the authority; the plan prose is
wrong. Do NOT change code." The ticket exists to reconcile *which* rule is
normative — a question that only arises because the target is a single ordinal.

**(T3) `scripts/vault_permission_cleanup_dryrun.py::_merge_git`** — the "ONE
mirror of the legacy fold that B3 chose NOT to move." It re-derives the
strongest-wins precedence to a single ordinal, so it can drift from
`fold_legacy_workspace_ceiling` at any commit.

**(R13) two resource catalogs, no shared vocabulary.** `security/resource_catalog.py::RESOURCE_CATALOG`
vs `agent/config/resource_catalog.json` (different keys, coarser values); the raw
JSON is published by `/api/resource-catalog` with no value link to the Python
catalog.

**(R6) cross-language safe-defaults mirror.** `thoughtmachine/security.py::SAFE_DEFAULTS`
(Python) vs `useStore.js::PERMISSION_DEFAULTS` (JS); manual mirror, nothing links
them.

**(R9) workspace-ceiling chain, no funnel.** The read/normalise/cap path
`thoughtmachine/permission_store.py::workspace_ceiling` →
`web_ui/backend/config_manager.py::normalize_legacy_workspace_ceiling` →
`security/security_gate.py::apply_workspace_ceiling` holds one fact (the ceiling)
across three sites with no single composition point.

(For completeness, the two remaining unowned container rows are **out of scope
here and named, not resolved**, in §6.)

---

## 4. Candidate shapes

**Shape A — vocabulary and bins as two explicit collections, keyed
`resource → action → bin`.**
Store per workspace and per session:
`{ "git": { "read": "allow", "write_to_feature_branch": "ask", "write_to_dev": "deny", "push": "deny" }, ... }`.
The vocabulary is the *key set*; bins are the *values*. Composition is a
per-action meet on the `deny < ask < allow` chain.
*Tradeoff:* directly per-action; the artifact an operator sets is the artifact
the tool reads (good for Inv.5). Costs a schema-shape change and requires the
vocabulary to have a single owner.

**Shape B — flat keys `"resource:action" → bin`.**
`{ "git:read": "allow", "git:push": "deny", ... }`.
*Tradeoff:* trivial to validate and wire; but grouping for the UI rollup becomes
key-parsing, and a resource's actions are no longer visibly one thing.

**Shape C — keep today's one value per resource; treat "rows" as UI only.**
*Tradeoff:* least churn, but there is still exactly one ordinal per resource, so
T1/T2/T3 survive unchanged. This shape **fails the question** and is rejected.

---

## 5. Ruling

**Adopt Shape A** (nested `resource → action → bin`), with the collapsed
per-resource row rendered as a **derived view** (never a stored fact). Details:

### 5.1 What a vocabulary ENTRY is

- **Data shape:** one entry = `{ action_id, label, description }` and nothing
  ordinal. `action_id` names a concrete action with a distinct blast radius
  (the operator's example for git: `read`, `write_to_feature_branch`,
  `write_to_dev`, `push`). There is **no rank, no total order, no "level"**.
- **Owner:** the **resource definition** (the Global/Resource layer of 2a's
  map) — authored **once** at resource creation, by the resource author.
- **Propagation:** a workspace that imports a resource imports its vocabulary
  entries verbatim; a session sees the same entries. Import is the only way a
  vocabulary enters the workspace/session layers.

### 5.2 What a bin IS

- **Data shape:** `bin ∈ { deny, ask, allow }` — a three-element chain, not a
  number. Stored as `(resource, action) → bin`.
- **Owner:** bins live with **workspace/session config**, split by layer:
  - **Workspace bins** — chosen per imported action at import time. This is the
    *ceiling* role.
  - **Session bins** — chosen per action for a session. This is the *narrowing*
    role.
  - The **fail-closed default** bin of an entry (`deny` until an operator bins
    it) is authored **with the vocabulary entry** (Global/Resource), so the
    default has one owner.
- **Propagation:** workspace bins → session bounds; session bins → effective
  bins; effective bins → the worker (read-only consumer). No layer re-derives a
  bin from an ordinal; every layer *reads a bin*.

### 5.3 Composition at each layer (2a's substrate, restated for bins)

| Layer (2a) | What it owns, in bins terms |
|---|---|
| **Global / Vault** | the capability **vocabulary** + each entry's fail-closed **default bin** |
| **Workspace** | **workspace bins** per imported action (the ceiling) |
| **Session** | **session bins** per action (the narrowing) |
| **Worker** | nothing new — consumes the **effective bins** |
| **Container / Resource runtime** | untouched — bins are a permission-layer fact, not a runtime fact |

### 5.4 Session narrowing — the intersection rule

Narrowing is the **same intersection shape as today's `ceiling ∩ grant`**, but
computed **per action** on the bin chain rather than via a numeric rank:

> **effective_bin(resource, action) = meet(workspace_bin, session_bin)**
> where `meet` is the greatest-lower-bound on the chain `deny < ask < allow`.

So `allow ∩ ask = ask`, `ask ∩ allow = ask`, `* ∩ deny = deny`, `allow ∩ allow = allow`.
Identity (session unspecified) = `allow`, so an unbinned session inherits the
workspace bin. A session can **narrow** (move toward `deny`) but can never
**widen** past the workspace bin. This is the operator's "same shape as today's
ceiling ∩ grant" made explicit — an intersection, not a re-ranked fold.

**Visibility (Inv.5).** The session enumerates the workspace's imported
vocabularies; an action whose effective bin is `deny` is shown tagged **"not
available this session"** rather than omitted. Visibility over force: the agent
can *see* the mechanism that refuses it.

### 5.5 How a refusal names its bin AND its mechanism

A refusal at the tool boundary must emit, together: (a) the `resource`+`action`
it was denied for; (b) the **contributing bin** — whether the `deny` came from
the session bin, the workspace bin, or the entry default; (c) the **owner** of
that bin (session config / workspace config / resource definition); (d) **where
to read it** (the config file / endpoint). This satisfies Inv.3 (reason traceable
to its owner) and the Inv.5 enforcement form (§0) in one move: the reason is a
bin *read directly*, not a rank *reconstructed by a rule*, so naming it is
trivial and cannot itself drift.

### 5.6 Authors edit vocabularies; operators set bins

- **Resource author:** edits the resource definition — adds/removes **vocabulary
  entries** and their **default bins**. Adding an action adds a new binnable item,
  defaulting fail-closed (`deny`) until binned.
- **Operator:** sets **bins** — per workspace (at import) and per session (narrow),
  using the tri-state control. An operator never edits the vocabulary.
- **UI:** default one row per resource ("`git`: [off / ask / full]", `off`≡`deny`,
  `full`≡`allow`) with an expand affordance; expanded = one row per action, each
  its own tri-state. The collapsed row is a **rollup view** of its actions, not a
  stored fact (Inv.2: the per-action bins are authoritative). Most operators never
  expand.

---

## 6. Consequences

### 6.1 Shape-ticket re-examination

- **T1 `bug/host_bash-ask-ceiling-asymmetric` — DISSOLVED (steady state).** The asymmetry is a
  property of the *ceiling-as-rank-fold* (only `host_bash` lacks a mid tier), not
  of `host_bash`'s semantics. With bins, `workspace_bin=ask` ∩ `session_bin=allow`
  = `ask` (a prompt) **uniformly for every resource, `host_bash` included**;
  there is no "no tier between banned and ask" gap to fall through. The second
  `host_bash` gate (`allow_host_resources`) becomes simply another bin row
  (resource-level availability) binned independently, not an asymmetric overlay.
- **T2 `design/b3-fold-strongest-vs-smallest` — DISSOLVED (steady state).** The
  question only exists because several legacy grains must fold onto one ordinal
  and the fold needs a tie rule (strongest vs smallest). Two-axis storage has no
  fold: `read` and `write` are two independent rows, each with its own bin, so
  nothing is combined and the tie rule is vacuous. *Caveat:* the **legacy→canonical
  migration** still needs *some* deterministic rule for its bounded lifetime — that
  is a migration detail (named in §7), not a steady-state shape question.
- **T3 `scripts/vault_permission_cleanup_dryrun.py::_merge_git` — DISSOLVED
  (steady state).** "No rank ordering to mirror": with per-action bins there is no
  single ordinal git value for a second implementation to reproduce. *Caveat:* the
  dry-run/migrate scripts still re-implement the legacy fold **only while the
  migration exists**; that mirror is bounded by the migration task (§7).

**Carry-forward (all three): not closed.** T1/T2/T3 are DISSOLVED **in steady
state only** — each ruling returns a forward reference to §7 (the bounded
legacy→canonical migration), and this document **closes no ticket**. While the
migration window is open, `security/resource_catalog.py::fold_legacy_workspace_ceiling`
and `scripts/vault_permission_cleanup_dryrun.py::_merge_git` remain two
implementations of the same legacy git fold and must be kept **in lockstep** — a
change to the fold order in one must land in the other. That mirror obligation is
the live residue the steady-state dissolution defers to §7.

### 6.2 Residue under the bins lens

- **R13 (two resource catalogs) — TRANSFORMED into a prerequisite.** Bins are
  useless without a vocabulary, so the model *forces* a **single vocabulary owner**
  (the resource definition, §5.1). R13 stops being a floating "two unowned copies"
  residue and becomes a **stated obligation**: collapse the two catalogs to the
  one owned vocabulary before bins can be authored. Bins do not auto-fix it; they
  make fixing it non-optional.
- **R6 (cross-language safe-defaults mirror) — TRANSFORMED, same shape as R13.**
  The safe default is now the **default bin of each vocabulary entry** (§5.2),
  owned with the entry and read by both backend and frontend. So R6 becomes the
  same single-owner obligation as R13, not a separate JS/Python mirror problem.
- **R9 (workspace-ceiling chain, no funnel) — FUNNELED.** The model names the
  single composition point that was missing: **read bins → meet**. The three-site
  read/normalise/cap chain (`permission_store.workspace_ceiling` →
  `config_manager.normalize_legacy_workspace_ceiling` →
  `security_gate.apply_workspace_ceiling`) collapses, in steady state, to one
  read plus one `meet`. The legacy normaliser survives only for the bounded
  migration.

### 6.3 Open questions, ruled

**(a) Bins per-workspace, per-session, or both? — BOTH.** Workspace bins express
"this workspace never allows X"; session bins express "this session is further
locked down." Effective = the **meet** (§5.4): the session may only narrow. This
is exactly the operator's stated shape (workspace-import bins + session narrowing),
composed by the same intersection as today's ceiling ∩ grant.

**(b) Does 2b touch `ask`'s effective behavior, or defer to `design/ask-semantics`? —
BOUNDARY NAMED: 2b defers it.** 2b rules only the *structure* of `ask` — that it is
a bin value, stored/owned/propagated like the other two, and that a refusal must
name it (§5.5). 2b does **not** rule what an `ask` bin *does* at runtime —
specifically the non-interactive case where `ask` currently collapses to deny
(`tools/host_bash_tool.py`: `grain == "ask"` under `is_worker_context` denies
in-tool). The resolution of an `ask` bin (prompt vs auto-deny in worker context)
belongs to **`design/ask-semantics`** and is out of scope here.

### 6.4 Named but deferred / out of scope (not resolved here)

- **R2** (container-limit × 3 resolvers: `infra/container_manager.py::_get_max_containers`,
  `security/admission_gate.py::_container_limit`, `thoughtmachine/container_registry.py::_get_max_containers`)
  — out of scope; fix folds into the **container strand** (name only).
- **R1** (container-count × 2: `infra/container_manager.py::_counts_toward_limit` /
  `_active_containers` vs `security/admission_gate.py::workspace_container_count`)
  — folds into **`bug/container-quota-two-counters-one-fact`** (name only).
- **Container lifecycle** → `design/container-lifecycle-and-reclamation`.
- **`ask` semantics** → `design/ask-semantics` (named strand; not yet written).
- **Credentials / Comm Gate / Modes / Resource Catalog** → their own passes.
- **Performance** → Phase C.
- **KB ownership** → a Pass-2 output, not a rule here.
- **Migration from today's rank tables** → post-2b (bounded; see the T2/T3 caveats
  and §6.2 R9).
- **Specific per-resource vocabularies** (which actions each resource exposes) →
  post-Pass-2. This document rules the *shape*, not any resource's action list.
- **UI widget implementation** (the tri-state control, the expand affordance, and
  the collapsed-row rollup semantics) → deferred; the shape fixes only that the
  collapsed row is a **derived view**, never a stored fact.
- **Ticket-location hygiene (Infra ruling, mentioned only):** permission/design
  tickets live in `working_docs/ticket-<category>-*.md`. T1 is `bug/host_bash-ask-ceiling-asymmetric`;
  T2 is `design/b3-fold-strongest-vs-smallest`; T3 is the `_merge_git` mirror.

---

## 7. Deferred

- Specific per-resource vocabularies (post-Pass-2).
- UI widget implementation.
- Migration path off the rank tables (`GRANT_LEVEL_RANKS`,
  `WORKSPACE_CEILING_LEVELS_RANKS`, `GIT_BREADTH_RANKS`, `fold_legacy_workspace_ceiling`,
  `_GIT_FOLD_RANKS`, and `_merge_git`).
- `ask` semantics (`design/ask-semantics`).

---

## Appendix — citation index (symbol → file, checkout `6fa9c1f`)

- `RESOURCE_CATALOG`, `WORKSPACE_CEILING_VOCAB`, `GRANT_LEVEL_RANKS`,
  `WORKSPACE_CEILING_LEVELS_RANKS`, `GIT_BREADTH_RANKS`, `_GIT_FOLD_RANKS`,
  `fold_legacy_workspace_ceiling` — `security/resource_catalog.py`
- `apply_workspace_ceiling`, `get_effective_permissions`, `_HOST_BASH_CEILING_RANKS`,
  `_HOST_BASH_RANKS`, `_DISK_FAIL_CLOSED_CEILING`, `_DISK_FAIL_CLOSED_SESSION` —
  `security/security_gate.py`
- `SAFE_DEFAULTS`, `PERMISSION_SCHEMA` — `thoughtmachine/security.py`
- `workspace_ceiling`, `read_session_permissions`, `write_session_permissions`,
  `migrate_session_permissions` — `thoughtmachine/permission_store.py`
- `normalize_legacy_workspace_ceiling`, `resolve_effective_permissions` —
  `web_ui/backend/config_manager.py`
- `_PERMISSION_LEVELS_BY_RESOURCE` (alias of `WORKSPACE_CEILING_VOCAB`),
  `validate_workspace_permissions` — `agent/config/resource_catalog.py`
- `RESOURCE_CATALOG` (JSON), `permission_grain_set` — `agent/config/resource_catalog.json`
- `PERMISSION_DEFAULTS` — `web_ui/frontend/src/store/useStore.js`
- `host_bash` tool (`required_categories`, `_effective_grain`) — `tools/host_bash_tool.py`
- `workspace_allows_host_resources` — `tools/host_resource_policy.py`
- `_merge_git`, `SESSION_VOCAB` — `scripts/vault_permission_cleanup_dryrun.py`
- `workspace_container_count`, `_container_limit` — `security/admission_gate.py`
- `_counts_toward_limit`, `_active_containers`, `_get_max_containers` —
  `infra/container_manager.py`
- `_get_max_containers` — `thoughtmachine/container_registry.py`
