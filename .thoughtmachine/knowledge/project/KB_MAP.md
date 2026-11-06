# KB_MAP — where content lives

*One table. When you write something, find its class and put it in its home.
When a class has no home, add a row — do not create a second site.*

**Owner:** Infra. **Cadence:** extended when a new content class appears.
**Rule:** one class, one home. A second site for the same class is a bug.

| Content class | Home | Cadence | Owner |
|---|---|---|---|
| Current state (what is true now) | `project/status.md` | strand close | whoever closes the strand |
| Phase orientation (where we are) | `project/current_map.md` | phase boundary | Main |
| Machine model (what it promises) | `project/machine_model.md` | phase boundary | Main |
| Design decisions | `project/design_*.md` (pinned, sha256) | one-shot | Infra |
| Architecture invariants (already-true rules a re-derivation would break; pointer-only, never a second home) | `project/architecture_invariants.md` | on earning | Infra |
| Container trust contract (what a container promises) | `project/design_container_trust_contract.md` (pinned, sha256 `d1d27c2e15f7ac9480088967c0c38c16fc884f964495d854f4562826b29ef122`) | on ruling | Infra |
| Binding rulings | `working_docs/rulings-*.md` | append-only | Main rules, Infra records |
| How we work (rules) | `project/working_agreements.md` | on earning | Infra encodes, Main owns intent |
| Reading index (what to read) | `project/INFRA_ONBOARDING.md` | when a file's role changes | Infra |
| Tickets | `project/operator_observations.md` | on filing | Infra |
| Raw operator notes (unfinished thoughts) | `project/operator_notes.md` | on thought | operator |
| Structural debt | `project/structural_debt_register.md` | on earning | Infra |
| Open items | `project/open_items_register.md` | on discovery | Infra |
| Roadmap / vision | `project/v3_*.md`, `project/roadmap.md` | on phase | Main |
| Recon / audit output | `project/audits/*.md` | one-shot | the auditor |
| Machine facts | the design doc for that subsystem | on discovery | Infra |
| Proposals (not yet ruled) | `project/open_items_register.md` | on drafting | the proposer |
| Session handoff | pasted, not persisted | per session | outgoing engineer |
| Patterns / lessons | `personal/lessons_learned.md` | on repeat | Infra |
| Bugs fixed | `personal/bugs_and_fixes.md` | on fix | whoever fixes |
| Task history | `personal/task_tracker.md` | automatic | — |
| Deferred ideas | `personal/parking_lot.md` | on idea | whoever |
| Test / CI disposition | `docs/ci/` (tracked) | per change | Infra |

| Container controls findings (pre-brief) | `project/container_controls_findings.md` | one-shot | Infra |
| Core-feature backlog | `project/core.md` | on discovery | Infra |
| Historical state snapshots | `knowledge/archive/status_*.md` | on rewrite | Infra |
| Superseded recon / retired docs | archive with tombstone | on retire | Infra |
## Cadences

- **strand close** — `status.md`
- **phase boundary** — `current_map.md`, `machine_model.md`
- **on earning** — `working_agreements.md` (a new rule), `structural_debt_register.md`
- **on discovery** — `open_items_register.md`, machine facts
- **on new content class** — this file

## The rule

If you are about to write something and cannot find its class here, you have two
options: it belongs to an existing class (put it in that home), or it is a new
class (add a row here first, then write). Do not create a second site for an
existing class — that is the disease this map exists to prevent.
