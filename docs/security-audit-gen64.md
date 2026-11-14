## §00 — How to read this document

This document answers one question: when this product's security and trust properties are claimed to hold,
what in the test scaffolding actually enforces them — not what the design intends and not what a docstring
promises, but which rule a change would have to break a test to violate, and which rule nothing would notice.
It is written for anyone about to rely on that scaffolding: a reviewer of a security-touching change, the
author of the next test that claims to prove a boundary, or a reader who has been told "it is tested" and
wants to know what that meant.

Three routes. For **how the design was arrived at** — what ran, what documented it, what contract it settled
on — read §01. For **what the scaffolding is built of, and how much of it could silently stop running** —
read §02, then §05, which asks the same question of the collection gate itself. For **how each boundary is
enforced, and where it is not** — read §03 for the design, §04a–§04d for the rule-by-rule evidence, and §06
for the register of what no test defends.

- Citations are `path:LINE`. A line number points at the region a claim lives in, not at a stable address;
  symbols survive edits and line numbers do not, so trust the symbol when the two disagree.
- Counts carry their provenance: `[collected]` is a pytest collection count, `[static]` is a parse or grep of
  the tree, `[quoted]` is a number copied out of a workflow or a document. They are not interchangeable.
- `[unverified]` marks a claim reasoned about but not confirmed from code; §08 lists them in one place so they
  can be checked or discounted together.
- A test's existence is not evidence that it runs. §05 is the section that takes that sentence seriously.
- If you read one thing, read §06 **D1**. It is the failure the rest of the suite is arranged around.

### Map of the sections

| § | Section | Line | What it answers |
|---|---|---|---|
| §00 | How to read this document | `:13` | The route in: what the document answers, how to read the evidence, where to start. |
| §01 | The design passes: what ran, what document, what contract | `:441` | What the design work produced, and which of its documents may still be cited as the lattice. |
| §02 | The test scaffolding, mapped | `:55` | What the suite is built of — files, fixtures, gates, workflow invocations — and where it is thin. |
| §03 | The trust boundary as designed | `:742` | What the code that refuses decides, on what input, and what it does when the input is absent. |
| §04 | The trust boundary as tested | `:947` | Which of those decisions a change would have to break a test to violate, rule by rule. |
| §05 | Anti-vacuity: how the suite stops itself from passing for the wrong reason | `:1908` | How the suite could go green without testing anything, and what would notice. |
| §06 | The defect register: what the tests do not actually defend | `:2023` | The defects, ranked, in one place — start at D1. |
| §07 | Recommendations: how to make the suite mean what it says | `:2146` | What to change, in what order, and what each change would make provable. |
| §08 | Evidence index and method | `:2219` | How the evidence was gathered, what was not run, and every claim still `[unverified]`. |

*Reading order is §00 → §01 → §02 → §03 → §04 → §05 → §06 → §07 → §08; the sections were written in
dependency order rather than this one, so physical order is not reading order (§02 came first, before the
§01 it builds on), and §04 carries a second, longer read of the design, labelled 04x.*

## §04 — The trust boundary as tested

§03 read the boundary off the source. This section asks a different question of the same files: *which of
those statements would a change have to break a test to violate, and which would not?* The unit is a **rule** —
one sentence the boundary is supposed to obey — and each rule gets four columns of evidence plus one of
judgement.

**The `Class` column is a five-letter vocabulary used only in §04.** It is deliberately blunt, because the
distinction that matters here is not "does the test pass" but "could this test fail for the reason its name
claims":

| Class | Means | Why it is its own class |
|---|---|---|
| **B** | behavioural unit — calls the function under test with a real input and asserts the returned value | can fail for the stated reason |
| **H** | behavioural, through a harness — real executor / real gate / real bridge, fakes only at the process edge (`FakeState`, `spawn_harness`, `iso_server`) | can fail for the stated reason, *if* the fake edge is faithful |
| **P** | pin — a constant, truth table or byte-exact string asserted in full | fails on *any* drift, including intended drift; cannot say which drift is wrong |
| **S** | static / source pin — greps source, imports to prove absence, asserts an AST shape | cannot fail for behaviour at all; catches only the shape it spells |
| **T** | tautological or stale — the test still runs, but its body cannot fail for the reason its name or docstring gives | the failure mode §02(h) registers |

A rule may carry several classes; the interesting reading is the *mix*. A rule with only **P** is a
documentation rule. A rule with only **S** is a naming convention. A rule with **H** evidence is a rule
somebody could actually break.

Two counts frame what follows, both measured on this tree and neither of them flattering. Of the twenty-eight
test files cited below, **six** reach a real gate or executor rather than a fake; the remaining twenty-two call
a helper directly. And of the thirty-one rows in the tables, **four** have no test at all and **one** has a
test that cannot fail for its stated reason. So the honest summary is not "the boundary is tested" or "the
boundary is not tested" but: *narrow, deep coverage of the ∧ arithmetic; no coverage of the seams where two
tables meet; and a habit of pinning prose in place of mechanism.*

---

§04 and §05 described what the suite pins. This section is the inverse, and it is deliberately pessimistic: controls that exist in the source, that read as defended in review, and whose removal — or whose defeat — no test would notice. Severity is ordered by what an attacker or an ordinary careless session actually gains, not by how bad the code looks. **(S1)** reachable today, no privilege escalation required. **(S2)** the control is real and works for the inputs it was written for, but a named subclass of inputs goes around it. **(S3)** the control exists and is correct and nothing tests it. **(S4)** the suite's own guard rails permit silent regression. Each entry ends with the only question that matters for this audit: would a test fail.

#### Band S1 — reachable today

**D1 — The `credential` resource grain is never consulted where credentials are injected** (S1)
The catalog declares `"credential": ["banned","read","use"]` (`security/resource_catalog.py:132`, canonical tuple `:217`) and the gate defaults the grain to `banned` (`security/security_gate.py:97`, `read` at `:698`). But the seam that actually hands secret material to a tool — `injector = CredentialInjector(ws_id)` then `tool_args = injector.inject(tool_args)` (`agent/core/tool_executor.py:771-772`) — sits *after* the executor's only `check_required_categories` call (`:612`); between `:751` and the `CREDENTIAL_USE` audit publish there is no grain read at all. `credential:use` appears zero times across `agent/` (67 files) and `tools/` (61 files) [static].
What it enables: a session pinned to `credential: banned` still receives injected credential material for any tool that declares a credential — the `banned` state is documentation, not a barrier. The audit line records the use; nothing declines it.
`Would any test notice?` No test would fail. `tests/test_credential_resource_grain.py` (`:56`, `:74`, `:101`, `:141`, `:151`, `:180`) exercises the gate in isolation and never reaches the injector, so both the defect and its repair are invisible to it.

**D2 — Pending security requests carry no session identity, so the resolver has no owner check** (S1)
`_pending_security_requests` (`thoughtmachine/security.py:48`, populated `:296`) is keyed by request id alone. A `session_id` is threaded into the gate's request path (`security/security_gate.py:1744-1760`) and never read anywhere; the web layer's prompt plumbing (`web_ui/backend/server.py:170-210`) resolves by id as well.
What it enables: any caller that can reach the resolve path — a second session, a second browser tab holding a different session cookie, a reconnecting worker — can approve or deny a prompt that belongs to another session's turn, and the approval is indistinguishable in the audit trail from the owner's.
`Would any test notice?` No test would fail. Every prompt/resolve test in the suite drives a single session and never asks who is answering.

**D3 — The provider API key is a plain `str` on disk and is copied into every tool's `agent_config`** (S1)
`agent/config/provider_profile.py:24` declares `api_key: str = ''` — not `SecretStr`, so a repr, an f-string or a traceback frame prints it; `:71` writes the profile with a bare `open(..., 'w')` plus `json.dump`, with no `chmod` and no `0o600`, so `providers.json` inherits the process umask and is readable by any other local account on a shared host. The module docstring (`:4`) states the file is never read by the agent; `:169-170` reads it. Worse than the disk story: `agent/core/tool_executor.py:714-715` places `self.config.api_key` into `tool_args['agent_config']`, so the live key is inside the argument dict handed to every tool call (`agent/core/agent.py:957`), and the only thing standing between that key and a log file is a shape-based regex (`agent/logging/redaction.py:23-47`, `:51`, wired at `agent/logging/__init__.py:237`, `:269`, `event_logger.py:206`, `:218`, `streams.py:120`).
What it enables: a non-`sk-`-shaped key, a key from a provider whose prefix the regex does not know, or the key nested one level deeper than the matcher checks, survives redaction and lands in an event log a user is invited to export. Any tool that dumps its own arguments dumps the key.
`Would any test notice?` No test would fail. Container env is clean by construction (`infra/container_env.py:165` carries only `GIT_AUTHOR_*`/`GIT_COMMITTER_*` and the git token arrives as one env var from `thoughtmachine/git_auth_env.py:47`, `:142`), and `tests/test_api_key_hygiene.py` inspects dict *key names* only — see D12.

#### Band S2 — the control works, but a whole class of inputs goes around it

**D4 — `TRUST_TEST_HARNESS` is set for the entire suite, so every web test runs behind an open gate** (S2)
`conftest.py:153` wires a session-scoped autouse fixture that captures `auth_boundary.TRUST_TEST_HARNESS` (`:162`) and turns it on; `_TrustRestoringFinder` (`:104-137`) on `sys.meta_path` restores the flag across module reloads and `sys.modules` purges so no test can accidentally see the secure default. With the flag on, `web_ui/backend/auth_boundary.py:625` returns before steps 2-6 — Host acceptance, cookie minting, Origin verification, credential check and the websocket 1008 close — for any client whose host is in `TEST_HARNESS_CLIENT_HOSTS = frozenset({"testclient"})` (`web_ui/backend/local_gate.py:37`, against `LOCAL_CLIENT_HOSTS` at `:35`).
What it enables: not an attacker, but a *regression*. All 7,199 collected tests [collected] run against a boundary that is not the one shipped. A change that breaks Host handling, mints an unauthenticated cookie, or drops the Origin check for real peers is still green, because the only code path the suite exercises is the early return.
`Would any test notice?` Partly. The flag-off path is genuinely exercised — `tests/test_auth_boundary.py:150` (client set to `LOCAL_PEER`) with `_harness_trust(False)` at `:160` and restoration at `:167`/`:172`, a fresh-import subprocess at `:574`, and a harness peer receiving 401 with the flag off at `:611`, `:780`, `:795`, `:800`. The defect is scope, not absence: those four tests are the *only* place the shipped boundary is under test, out of 534 test files [static].

**D5 — The exec drift gate compares hardening and network, but not resource limits, and ports nowhere** (S2)
`_check_exec_drift` (`infra/container_manager.py:2711`, intent in the docstring `:2712-2737`) does refuse a container whose live user differs (`:2739`) and now genuinely re-checks the hardening recipe — `_hardening_conformance` (`:2768`) over `cap_drop`/`security_opt`/`read_only`/`user` at `:2762-2795`, exiting `_EXEC_DRIFT_EXIT_CODE = 126` (`:250`) with the `CONTAINER_EXEC_DRIFT` audit event (`:249`) and the message "Container hardening is MORE PERMISSIVE". `_config_matches` (`:4900`) compares `HostConfig.NetworkMode` (`:4918`, `:4929`) and the `/workspace` mount `RW` bool (`:4920-4930`). Nothing compares resource limits: `mem_limit`, `cpu_quota` and `pids_limit` appear in `_EFFECTIVE_INTENT_KEYS` (`:701-708`), which is consumed only by `_reobserve_effective_intent` (`:724`) to refresh a *record* — record bookkeeping, not a refusal.
What it enables: `docker update --memory` / `--cpu-quota` / `--pids-limit` against a live workspace container raises the ceiling and every subsequent `exec` passes the drift gate; a container with `--publish 0.0.0.0:5432` is likewise untouched by any assertion in the tree, so a service that was designed to be unreachable by network becomes reachable from the host and the suite calls it conformant.
`Would any test notice?` No test would fail. `tests/test_container_exec_hardening_drift.py` pins the hardening axes it was written for (`:195` read_only, `:217` cap_drop, `:228` security_opt, `:261` conformant-runs) and never varies memory, cpu or pids; `tests/test_container_exec_drift.py:350` patches `_config_matches` to raise rather than comparing against it, and six further files stub `_check_exec_drift` straight to `("allow", None)` / `("ok", None)` (`tests/test_container_user_and_git_ownership.py:388`, `:396-398`; `tests/test_container_interface_contract.py:138`; `tests/test_worker_thread_turn_cancel_binding.py:138`; `tests/test_container_exec_interrupt.py:109`; `tests/security/test_windows_uid_shape.py:245`, `:415`).

**D6 — The `ci` grain has no entry in the workspace ceiling map, so its ceiling is a documented no-op** (S2)
`_WORKSPACE_RESOURCE_MAP` (`security/security_gate.py:210-220`) names nine grains — filesystem, docker, container, host_bash, git, network, mcp, credential, visual — and `ci` is not one of them, although `WORKSPACE_CEILING_VOCAB` (`security/resource_catalog.py:210-219`) does carry it, so an operator can legally write a `ci` ceiling into workspace config. The map's own comment (`:203-209`) states the mechanism and names the defect: a grain absent from the map falls through the `continue` and its configured ceiling silently does nothing.
What it enables: a workspace administrator who sets `ci: banned` to stop an agent from touching pipelines gets no refusal and no audit line saying the ceiling was ignored — the most dangerous shape of failure, where the configuration reads as enforced in both the UI and the docs.
`Would any test notice?` No test would fail. No test applies any ceiling to `ci`, and the ceiling-metadata tests iterate the grains they already know rather than asserting the map covers `WORKSPACE_CEILING_VOCAB`.

**D7 — Container status/stop/remove fall back to a workspace-label check that deliberately admits unlabeled containers** (S2)
Creation is session-scoped: `list_containers` (`infra/container_manager.py:4165`) filters on a name regex (`:4208-4220`) built from the session id, so a session cannot *enumerate* another session's containers. The control path is looser — `tools/container_control.py:718-750` falls back to a workspace-label check when the name does not match, and `:741-746` explicitly allows unlabeled legacy containers through so pre-label containers stay manageable.
What it enables: a container started without the session-scoped name and without the workspace label — a legacy container, a hand-started container, a container whose labels were stripped — is controllable by name from *any* session in the workspace. Enumeration is defended; targeting is not, and the two paths do not share a predicate.
`Would any test notice?` No test would fail. No test constructs an unlabeled container and asserts that a foreign session is refused; the fallback branch is covered only by tests that assert the fallback *succeeds*.

#### Band S3 — the control is correct and nothing tests it

**D8 — The ceiling layer-attribution path never executes in the suite** (S3)
`_annotate_ceiling_changes` (`security/security_gate.py:703`) is what turns a denial into evidence — it stamps which layer produced a limit (labels at `:610`, `:625`, `:747`; `_FailClosedReasonDict` at `:518`, `:1459`). No test drives it: `tests/security/test_denial_layer_attribution.py:110` hand-builds a `_CeilingAnnotatedDict` and asserts against the dictionary it just constructed, so the function under test is not the function that runs in production.
What it enables: a denial attributed to the wrong layer. "The workspace ceiling refused this" versus "the session grant refused this" is the difference between an administrator tightening the wrong knob and a developer debugging a phantom config; if annotation drifts, every downstream ops decision drifts with it, and the audit trail asserts a cause it did not compute.
`Would any test notice?` No test would fail. The attribution test's fixture supplies the annotation, so deleting `_annotate_ceiling_changes` entirely leaves `tests/security/test_denial_layer_attribution.py` green.

**D9 — The `workspace_id_required` fail-closed guard is pinned by exactly one assertion** (S3)
`_FailClosedReasonDict` (`security/security_gate.py:1459`) exists so that a missing `workspace_id` produces a refusal that names its reason rather than a permissive fallback; the guard on the live `use_disk=True` path is `security/security_gate.py:1415-1422`, reached from six call sites including `infra/container_manager.py:4988`. The entire pin is `tests/test_session_grants_canonical_pins.py:536`.
What it enables: removal without a red suite. Six call sites, one assertion, and the assertion is a canonical-pin test whose subject is the *shape* of a dict rather than the refusal semantics — invert the guard to fail-open and the suite still reports green.
`Would any test notice?` `tests/test_session_grants_canonical_pins.py:536` is the only failing path in the tree; no other test touches the guard, and the six call sites include `infra/container_manager.py:4988`, which monkeypatches the gate in eight files without ever exercising this branch [static].

**D10 — The host-mode `.githooks/pre-commit` refusal has no test at all** (S3)
`tools/git_info_tool.py:1054-1063` refuses to run git in host mode when `.githooks/pre-commit` exists in the target repository, exiting before any git process is spawned — the correct defence against a repository that carries its own hook. The string appears exactly once in the tree, in that refusal; there is no test file that constructs a repository with `.githooks/pre-commit` in host mode.
What it enables: deletion or reordering of that check. The failure is silent in the worst way — an attacker-supplied repository supplies the hook, host-mode git executes it with the developer's uid and environment, and the only artefact is a commit that looks normal.
`Would any test notice?` No test would fail. `tests/security/test_git_hardening.py` covers container argv construction, not the host-mode refusal.

**D11 — The hardened git argv is pinned by name only for the flags someone already thought of, and all 14 behaviour tests are skippable** (S3)
`tools/git_info_tool.py:1070-1085` builds the hardened argv and `:1086-1089` appends `--no-verify`. §04b.4 (`:1600-1615`) already records the coverage holes: `core.attributesFile` has zero hits under `tests/`, and `diff.external` / `filter.clean` / `filter.smudge` / `credential.helper` have no by-name assertion [unverified-grep]. Independently of which names are present, the whole behaviour layer sits behind a module-level autouse `shutil.which("git") is None` skip in `tests/security/test_git_hardening.py` (`:88-96`), and there is no git binary in this environment, so those 14 tests do not run here at all.
What it enables: a repository that ships `.gitattributes` with a `filter=` driver, or a `diff.external` command, or a `credential.helper` pointing at an attacker binary — all three are *config-file* attacks that a by-name argv check is the only defence against, and all three are unasserted. The exact-argv pin that does exist (`tests/security/test_git_hardening.py:157`) covers the container path for the flags already in the list.
`Would any test notice?` No test would fail. Removing `-c core.attributesFile=/dev/null` (or its equivalent) from `:1070-1085` trips `:157` only if that file's expected argv happens to be regenerated; no test asserts the *policy* that every config-injection vector is named.

**D12 — `tests/test_api_key_hygiene.py` is narrower than its name suggests** (S3)
The file is 239 lines with nine cases across `:44`, `:52`, `:88`, `:97`, `:122`, `:152`, `:165`, `:200`, `:217`, all routed through `_check_dict_has_no_api_key` (`:31`), which walks a dict looking for the *key name* `api_key` at the top level.
What it enables: the leak it cannot see. A value nested under `agent_config.provider.api_key` two levels down, a key renamed `token`/`secret`/`bearer`, a key embedded in a URL query string, a key in a list of arguments, or a key in a rendered `repr()` of a `str` field as in D3 — all pass. The test's name is doing more work than the test.
`Would any test notice?` No test would fail. Adding a nested or renamed key anywhere in `tool_args` keeps all nine cases green.

**D13 — The "kill switch" test guarantees the absence of a control, not the presence of one, and reads one directory** (S3)
`tests/test_host_port_kill_switch.py` scans `SCAN_ROOTS = ("tools",)` (`:41-42`) for outbound-network primitives, with `SANCTIONED_MODULES` and an `ALLOWED_OFFENDERS` allowlist (`:70`, six entries, e.g. `tools/mcp_client.py:88-92` for the `Popen` at `tools/mcp_client.py:239`). It has a real-tree mode (`:271`, `:307`, `:322`, `:349`, `:368`), a positive control (`:292`) and a mutation battery (`:386-474`) — §05 correctly counts this as one of the best-built guards in the tree. The gap is scope and direction: `scripts/`, `web_ui/`, `infra/` and `agent/` are outside `SCAN_ROOTS`, and no test in the tree asserts the *absence* of `ports=` in any `docker create`/`docker run` invocation, so the published-port surface named in D5 has neither a positive nor a negative assertion. The nearest structural guard, `tests/test_tool_import_declaration_match.py`, likewise has no negative twin — nothing proves it would fail if a tool's declaration drifted.
What it enables: the kill switch erodes from the edges it does not scan. A `Popen` added under `scripts/` or a `ports=` mapping added to a compose-adjacent helper is invisible to both the guard and the audit's own mutation battery.
`Would any test notice?` No test would fail. Adding `ports={...}` to any container creation path, or a `subprocess.Popen` call under `scripts/`, is green across the suite.

#### Band S4 — the suite's own guard rails permit silent regression

**D14 — The CI anti-vacuity floor is 53.2% of the suite and gates one of four workflows** (S4)
`scripts/ci_assert_collected.py` pins `DEFAULT_EXPECTED = 3827` against 7,199 collected tests [collected] — 53.2%. `main()` (`:110`) only fails on `actual < expected`, never on `actual > expected` and never on an empty run; `build_command()` (`:98`) and `parse_collected()` (`:78-95`) make the run reproducible enough to trust, which is exactly why the number matters. The script is wired into one of the four workflows, and `.github/workflows/python-version-matrix.yml:156` runs the same selection with no floor at all.
What it enables: roughly 3,372 tests [collected] can stop collecting — a renamed directory, a broken import in a package `__init__`, a `conftest.py` collection error that pytest reports as a warning — and CI stays green. The floor proves the suite did not halve; it does not prove the suite did not lose a quarter.
`Would any test notice?` `scripts/ci_assert_collected.py:110` is the only failing path, and only below 3827 — no test asserts the suite's own size, and the guard is absent from three of the four workflows.

**D15 — pytest has no `--strict-markers`, no `xfail_strict`, and no way to notice a test that stopped running** (S4)
`pyproject.toml:51-58` sets `testpaths` (`:53`, `tests` plus `web_ui/backend/tests`) and a `markers` list (`:54`) and nothing else: no `addopts`, no `--strict-markers`, no `xfail_strict`, no `filterwarnings = error`. Five of the six xfails are non-strict (`web_ui/backend/tests/test_worker_stats_routes.py:64`, `:126`, `:154`, `:179`; `test_permission_routes.py:462`) and will silently become passes; the one strict xfail is `tests/test_worker_process_contract.py:455`. The `slow` marker has zero users, so the marker list documents an intent the suite never expresses. Two `docker`-marked tests are run by no CI job, and 25 tests sit behind `TM_RUN_DOCKER_*` environment variables that no workflow sets (`tests/security/test_git_container_sandbox.py:59`, `test_git_container_hooks.py:56-59`, `tests/docker/test_git_resource_overlay.py:62-64`, `tests/docker/test_restart_policy_live.py:303-304`).
What it enables: an xfail-driven fix regresses to broken and the run reports XPASS-less green; a typo'd marker silently filters nothing; a container test that never runs is counted in the collected total that D14's floor defends, so the floor is satisfied by tests that do not execute.
`Would any test notice?` No test would fail. None of these settings is asserted by a test, and the collected count includes every skipped and xfailed test.

**D16 — The onboarding knowledge base still documents a permission lattice the code no longer has** (S4)
`docs/KB_POLICY.md:131-137` lists `security_architecture` among the seven files a new contributor must read, and that KB still shows the dead five-level `banned < ask < read < write < full` ordering, while `security/resource_catalog.py:155` (`GRANT_LEVEL_RANKS`) ranks `read` *below* `ask`. `docs/testing/ci_process.md` states that `web_ui/backend/tests` is outside `testpaths`, which `pyproject.toml:53` contradicts; `docs/testing/test_inventory.md` claims no `ini_options` and 52 files / 752 tests against 534 files / 7,199 collected [static]/[collected]. `scripts/verify_doc_citations.py` validates citations in exactly one document, `docs/design-worker-lifecycle.md`.
What it enables: a reviewer reasoning from the documented lattice approves a grant escalation that the code treats as a downgrade, or vice versa. Because `ask` and `read` are both "not write", the ordering error is invisible in casual testing and only matters at the moment someone reasons about whether a level implies another — which is precisely the moment a security decision is made.
`Would any test notice?` No test would fail. Nothing in the suite reads the KB, and `verify_doc_citations.py` does not scan it.
## §07 — Recommendations: how to make the suite mean what it says

Every item below carries a line anchor, the change, the test it creates and what that test asserts, and the §06 deficiency it closes. Items with no anchor are not here. Figures are labelled [collected] (measured by running collection), [static] (counted in source) or [quoted] (read off an existing document).

#### Four principles

**1. Pin the seam, not the symptom.** `tests/security/test_gate_import_order.py:134` is the model because it asserts the same fact twice by two routes: once as a value derived from `security/resource_catalog.py`, once as a hard-coded literal. That redundancy is the point — the derived half fails when the table drifts, the literal half fails when the consumer stops reading the table. Every deficiency in §06 is of the second kind: the suite proves what the gate *would* answer and never proves the caller *asked*. `tests/test_credential_resource_grain.py` ranks `banned` below `use` exhaustively and would pass unchanged if `agent/core/tool_executor.py:771` were deleted. A seam test asserts the call, not the return value.

**2. Every guard gets a negative test of itself.** An unfalsifiable scanner is a comment with a cron job. `tests/test_host_port_kill_switch.py:386-474` is the working pattern: ten-odd mutants of the guard's own subject, each asserted to be rejected, so the guard's red state is exercised on every run rather than trusted. `tests/test_tool_import_declaration_match.py` has no such twin — nothing in the file proves it can fail, so a silent regex rot reads as 100% compliance. The rule is one test per structural guard that feeds a known-bad input and asserts failure; a guard without one is unverified, not verified.

**3. A floor is not a ratchet.** `scripts/ci_assert_collected.py:110` holds `DEFAULT_EXPECTED = 3827` [static] — 53.2% of the 7,199 [collected] that collection actually yields. The number is hand-maintained, so it drifts by default; it counts items *collected*, not passed, so 3,372 [static] tests could fail or be rewritten as `pass` and the assertion would not move; and it guards one of four workflows, while `python-version-matrix.yml:156` runs the suite with no floor at all. A floor answers "is it worse than the last time a human edited a constant"; a ratchet answers it on every commit and needs no human.

**4. Delete, don't annotate.** `docs/KB_POLICY.md:22` already states the rule this codebase keeps breaking: "The KB is the source. `docs/` is the publication." Nine lines of stale teaching survive in the same file at `:131-137`, describing a five-level lattice that `security/resource_catalog.py` (GRANT_LEVEL_RANKS ranks `read` at 1, below `ask` at 2) no longer implements. A document that asserts a security property is code and rots like code. Annotating it costs the same edit as deleting the claim and leaves the wrong statement in every grep and every model context.

#### Tier W — free wins

Each row is a single edit under an hour, none of them touches the design, and every one of them converts a currently-unfalsifiable claim into a failing test the first time the claim breaks.

| # | Change | Anchor | Test it creates | Closes |
|---|---|---|---|---|
| W1 | Call `check_required_categories(['credential:use'], …)` immediately before the injector is invoked, so the executor asks the grain instead of relying on a caller further up | `agent/core/tool_executor.py:751-774`, inject at `:771-772`; only grain check today at `:612` | New `tests/test_credential_grain_seam.py`: build a session whose `credential` grain is `banned`, run a tool that needs a provider key, assert the injector's call count is 0 **and** the refusal names `credential:use` | D1 |
| W2 | Give the import-declaration scanner a negative twin: a fixture declaration that violates the rule, asserted to be rejected | `tests/test_tool_import_declaration_match.py` (whole file; no failing case) | `test_rejects_known_bad_declaration` in the same file: feed a synthetic module that imports a class it does not declare, assert the scanner raises; and a positive control asserting it raises nothing on the current tree | D13 (unfalsifiable-scanner half) |
| W3 | Write `providers.json` with `os.chmod(path, 0o600)` after creation, not relying on umask | `agent/config/provider_profile.py:71`; key typed as plain `str` at `:24` | `tests/test_provider_profile_permissions.py`: create a profile, assert `stat().st_mode & 0o777 == 0o600`; second case with `umask 0o022` preset fails before the fix | D3 (half) |
| W4 | Delete the false claim in the module docstring ("never read by the agent"); the read happens at `:169-170` | `agent/config/provider_profile.py:4` vs `:169-170` | `tests/test_provider_profile_docstring.py`: assert the docstring does not contain a "never read" style claim **or** (better) delete `:4`'s claim and let the W3/W6 tests own the property | D3/D16 |
| W5 | Add `--strict-markers` and `xfail_strict = true`; triage the five non-strict xfails, delete the unused `slow` marker | `pyproject.toml:51-58`, marker list `:54`; xfails at `web_ui/backend/tests/test_worker_stats_routes.py:64,126,154,179` and `web_ui/backend/tests/test_permission_routes.py:462` | No new file: the config change turns each silently-passing xfail into a red run, which is the test. `pytest --collect-only` must then fail on any unregistered marker | D15 |
| W6 | Replace the literal with a committed baseline JSON (`ci/collected_baseline.json`), and make the CI job rewrite it on an accepted increase so the floor ratchets | `scripts/ci_assert_collected.py:110` (`DEFAULT_EXPECTED = 3827`, 53.2% of 7,199 [collected]) | `tests/test_ci_assert_collected.py`: (a) baseline-with-one-extra-deletion fails, (b) baseline raised by 1 passes and the file is written, (c) a per-directory delta of `-1` in `tests/security/` fails even when the global total rises | D14 |
| W7 | Add `-c core.attributesFile=/dev/null` to the hardened git argv and assert it alongside the existing `hooksPath` pin | `tools/git_info_tool.py:1070-1085`; existing exact-argv pin `tests/security/test_git_hardening.py:157` | Extend the exact-argv assertion to require the `attributesFile` pair; add a mutant that drops it and asserts the pin fails (pattern: `tests/test_host_port_kill_switch.py:386-474`) | D11 |
| W8 | Either assert the absence of `ports=` on every container-create path, or rename the file so it stops promising a kill switch it does not check | `tests/test_host_port_kill_switch.py:41-42` | `test_no_ports_mapping_in_create_paths`: scan `infra/container_manager.py` create calls for `ports=`/`publish_all_ports` and fail on a hit; the mutation half adds a synthetic `ports=` and asserts the scan catches it | D13 (port half) |
| W9 | Make the hidden skip loud: replace the autouse `pytest.skip` with a module-level `skipif` (or a porcelain-only fake `git` so the 14 tests run) | `tests/security/test_git_hardening.py:88-96` hides fourteen tests | The change is the test: a `skipif` shows `14 skipped` in the header; the stronger form fakes `git rev-parse --git-dir` etc. and asserts all fourteen execute, which is what makes W7 meaningful | D11 |
| W10 | Delete `docs/testing/test_inventory.md` or stamp it with a generation date and the command that produces it | `docs/testing/test_inventory.md` says 52 files/752 tests against 534 files and 7,199 [collected] | `scripts/verify_doc_citations.py` gains a numeric check: any doc claim of the form "N test files" is parsed and compared against collection; today it would fail on this file | D16 |

Use the table as a queue, not a reading list: W1 is the only row that changes a security outcome, W5 and W9 are the only rows that make existing passes visible, and the rest are the cost of being able to say the suite means what it says. Rows W2, W7 and W8 need their mutation half to count as done — a guard plus a guard-test is the unit of work, and adding the guard alone is what produced D13 in the first place.

#### Tier R — real work, part 1

**R1 — Bind approvals to a session.** `thoughtmachine/security.py:48` defines the approval registry and `:296` is where a pending request is looked up; the web sibling scopes itself per request at `web_ui/backend/server.py:170-210`. The registry keys on the capability alone, so one session's pending approval is addressable from another — the trust boundary is process-wide while the product boundary is per session. *Change:* derive `session_id` where the gate already has it (`security/security_gate.py:1744-1760`) and carry it into both the write at `:48`-built entries and the read at `:296`. *Test:* `tests/security/test_approval_session_scoping.py` — session A requests an `ask`-tier grain, then asserts (i) B's queue is empty, (ii) a grant raised in B does not satisfy A's request, (iii) a registry write without a `session_id` is refused rather than defaulted. *Closes:* D2.

**R2 — Give `ci` a ceiling, and give the map a completeness test.** `security/security_gate.py:210-220` is `_WORKSPACE_RESOURCE_MAP`; the comment above it at `:203-209` states the intent for unmapped resources, and the intent is not the behaviour. A resource with no entry is not clamped, so the grain exists in `security/resource_catalog.py:210-219` and has no ceiling. *Change:* add `ci` (and the two other catalog resources absent from the map) and flip the fall-through from permissive to a named refusal. *Test:* two assertions, and the second is the one that matters — a coverage test comparing `set(_WORKSPACE_RESOURCE_MAP)` to the catalog's resource names, so any future grain without a ceiling is red on the day it is added; and a decision test that passes an unmapped name and asserts a *refusal* whose reason names the resource, so the fail-closed direction is pinned rather than assumed. *Closes:* D6.

**R3 — Widen the drift comparison to the axes that cost money or ports.** `infra/container_manager.py:701-708` names `_EFFECTIVE_INTENT_KEYS` in six axes and `:724` (`_reobserve_effective_intent`) compares exactly those after start. The hardening recipe *is* covered — `_hardening_conformance` at `:2768` is checked at `:2762-2795` and tested at `tests/test_container_exec_hardening_drift.py:195,217,228,261` — so the gap is narrow: `mem_limit`, `cpu_quota`, `pids_limit` and published ports are recorded and never re-read, which is why a container restarted with 2 GB passes a suite that was written to catch exactly that. *Change:* add those keys to `:701-708` so `:724` compares them, and unpin the six stubs that currently replace `_check_exec_drift` wholesale — `tests/test_container_user_and_git_ownership.py:388` and `:396-398`, `tests/test_container_interface_contract.py:138`, `tests/test_worker_thread_turn_cancel_binding.py:138`, `tests/test_container_exec_interrupt.py:109`, `tests/security/test_windows_uid_shape.py:245` and `:415`. *Test:* `tests/test_container_resource_limit_drift.py` — start with `mem_limit="512m"`, re-observe an effective intent of `"2g"`, assert the drift report names `mem_limit` and the operation is refused; the stub removals are themselves the test that the existing call sites reach the real comparison. *Closes:* D5.

**R4 — Finish the session scoping of `status` / `stop` / `remove`.** `tools/container_control.py:741-746` falls back to a label match when the session lookup misses, and `infra/container_manager.py:4208-4220` is the ownership check it falls back past; the effect is that any container carrying the workspace label — including an unlabelled or legacy one matched by a different route — is addressable from a session that did not create it. The fallback was written to keep older containers usable and never got a decision attached. *Change:* make the fallback an explicit, documented choice (refuse, or admit only under an operator grant) instead of an implied one. *Test:* `tests/security/test_container_control_foreign_session.py` — create in session A, call `status`, `stop` and `remove` from session B with the real container id and assert three refusals; a fourth case covers an unlabelled container and asserts whichever side of the decision the change lands on, so the legacy path is pinned rather than incidental. *Closes:* D7.


#### Tier R — real work, part 2

**R5 — Execute the annotator instead of imitating its output.** `security/security_gate.py:703` is `_annotate_ceiling_changes`, the function that attaches which ceiling layers altered a decision. `tests/security/test_denial_layer_attribution.py:110` constructs `_CeilingAnnotatedDict` by hand, so the test asserts the *shape* of a dict the production annotator never produces; delete a branch inside `:703` and the file still passes. *Change:* build the denial through the real gate path and let `:703` fill the annotation. *Test:* the rewritten case asserts the annotated layers against a denial obtained from `check_*`, plus a mutation (make `:703` return its argument unchanged) that must turn the assertion red — without that second half the rewrite reproduces the same defect one layer up. *Closes:* D8.

**R6 — Type the provider key as a secret, end to end.** `agent/core/tool_executor.py:714-715` copies the resolved provider key into `tool_args['agent_config']`, which is the one dict most likely to be logged, echoed in an exception, or serialised into an event — and `agent/credentials/injector.py:33` already wraps secrets in a `Secret` type that exists for exactly this. Copying raw defeats the wrapper at the boundary where it matters. *Change:* type the field as `Secret` through the injector into the tool call and delete the raw copy at `:714-715`. *Test:* `tests/test_provider_key_not_in_tool_args.py` — assert `repr()`/`str()` of the full `tool_args` never contains the key value, assert `agent_config` carries a `Secret` whose formatted form is masked, and assert the client adapter receives the unwrapped value, so the wrapper is proven not to be cosmetic. *Closes:* the remainder of D3.

**R7 — Make the key-hygiene scan recursive.** `tests/test_api_key_hygiene.py:31` walks top-level key *names*, so a key named `api_key` at depth one is caught and the same value nested under `payload["response"]["key"]`, inside an event's data dict, or embedded in an exception string is not. *Change:* descend into nested dicts and lists and scan values, not just names, including `agent_config` and event payloads. *Test:* the extension plus a planted key at depth three in a synthetic event asserting red — the planted case is the guard's negative test and is what distinguishes this from the current version. *Closes:* D12.

**R8 — Record which design decision a test protects.** 8 test files [static] name a design document at all; the rest are traceable only by reading them. That is the §01 gap and it is the reason this audit had to reconstruct intent from behaviour. *Change:* a `design:` marker (or a doc-id line in the module docstring) naming the section a test pins, plus a scanner that requires it for files under `tests/security/` and `tests/` that touch `security/` or `infra/`. *Test:* the scanner needs its own negative twin (W2's pattern) — a synthetic module with no doc-id must fail, or the marker becomes the next unfalsifiable guard. Be explicit that the first version will flag hundreds of files; landing it as a non-blocking report first is the honest sequence. *Closes:* the §01 traceability gap carried into §06.

**R9 — Sweep the two smallest §06 leftovers in the same pass.** `security/security_gate.py:1415-1422` is the `workspace_id_required` second pin — `tests/test_session_grants_canonical_pins.py:536` pins it once, in one direction, so a change to the required-vs-optional polarity is caught in exactly one of the two places it can be violated (D9). And `tools/git_info_tool.py:1054-1063` is the host-mode path that bypasses the hardened argv, exercised by no test because every git test runs containerised; a shell-level test against a throwaway repo with `.githooks/pre-commit` installed is the only form that reaches it (D10).

#### Tier S — sequencing, and what not to do

- **Do not delete the `TRUST_TEST_HARNESS` pin; narrow it.** The flag is installed at `conftest.py:153` and consumed at `:162`, with the boundary side at `web_ui/backend/auth_boundary.py:625`. Deleting it trades D4 for a suite that cannot construct a session at all. *Change:* default the flag *off* in the root fixture and require each test to opt in, so the shipped boundary is the default and the harness is the exception. *Test:* the flag-off path already exists in two places (`tests/test_auth_boundary.py:611` and `:780`, with the peer case at `:150`) — generalise that pattern: a test that declares it exercises a refusal path opts out of the harness and asserts the refusal comes from `:625`, not from the fake. *Closes:* D4.

- **Do not move the floor to 7,199 [collected] in one commit.** W6's ratchet is the mechanism; a one-step jump to the real total fails on the first partially-collected environment (no Docker daemon, no `git`) and gets disabled, which is how the current 3,827 [static] got hand-maintained in the first place. Ratchet per directory (`tests/security/`, `tests/`, `web_ui/backend/tests/`), because that is where a deletion is legible.

- **Do not add a second scanner while the first has no negative test.** `tests/test_tool_import_declaration_match.py` and `scripts/verify_doc_citations.py` both currently assert in one direction only. R8 adds a third. Land W2 first, or the audit's own recommendation becomes an instance of D13.

- **Do not fix documentation by writing more documentation.** `docs/KB_POLICY.md:22` already says the KB is the source and `docs/` is the publication; `:131-137` contradicts it nine lines later. W4 and W10 are deletions. `docs/testing/ci_process.md` can absorb whatever survives, and `scripts/verify_doc_citations.py` — which checks exactly one file today — is the only durable enforcement among the three.

- **Do not let `xfail` or `skip` stand in for "done".** W5's `xfail_strict` and W9's module-level `skipif` are prerequisites for reading any of the other numbers, because a suite whose denominator is 7,199 [collected] but whose numerator includes fourteen silently skipped git tests is measuring itself.

- **Do not widen a comparison set without unpinning it.** Adding `mem_limit` to `_EFFECTIVE_INTENT_KEYS` (`infra/container_manager.py:701-708`) extends `:724`; if the six stubbed `_check_exec_drift` call sites in R3 stay stubbed, the new axes are compared in production and asserted nowhere — a new guard, no new test, D5 again.

If only one thing is done, do W1: it is the only recommendation that closes a seam where a banned session can obtain a credential, it is under an hour, and it is the pattern R2/R4/R5 then copy. Do R1 second, because an approval registry keyed without a session is the same class of defect at a higher tier. The 30-day shape is all of Tier W plus W2's negative twin; 60 days is R1, R2, R5 and R7 — the four items that make the suite's *negative* results meaningful; 90 days is R3, R4 and R6, which are the ones that require touching production behaviour rather than its scaffolding, with R8 and R9 landed non-blocking alongside them so the traceability and the leftover pins do not wait for a fourth quarter.


#### What this audit did NOT establish

- **The git-config flags that hardening never pins by name.** `diff.external`, `filter.clean`, `filter.smudge` and `credential.helper` are excluded by *mechanism* in `tools/git_info_tool.py:1070-1085`, not by an assertion that names them; `core.attributesFile=/dev/null` is asserted nowhere. A config-driven escape that re-enters through a mechanism this argv does not cover would not be caught by any test in the tree — and the claim rests on a read of that argv, not on a run.
- **The `strict=True` call-site list for `_compute_config`.** Only `infra/container_manager.py:2834` is cited. If any additional caller passes a partial intent, the count in §06 D5 is one higher than reported and the drift check is looser than described.
- **`--expected 125` is a floor, not a measurement.** `web_ui/backend/tests/` defines 263 test functions [static] and collects 305 [collected]; 125 sits at 41 % of that. Read §05's "+180" as an artefact of an under-set floor, not as 180 dead tests.
- **The six `--expected` invocations are a static grep count.** The numbers inside them are quoted from YAML — 3,827, 1, 3, 77, 125, 650 — and were not re-derived by running the workflows, which is why they are `quoted` and not `collected` in the table above.
- **`agents/` MCP manifest validation is the least-examined area in this document** (§04d §G). Manifests were read; no loader was traced end to end, so the audit makes no claim that a malformed manifest is rejected.
- **`ALLOWED_OFFENDERS` (`tests/test_host_port_kill_switch.py:70`) was sampled, not enumerated.** The offender count in §06 D10 is a lower bound on what the kill switch tolerates.
- **Nothing docker-marked or git-marked was executed.** This environment had no Docker daemon and no `git` binary, so every live container claim, every docker-marked test and all fourteen tests at `tests/security/test_git_hardening.py:88-96` were read, never run. Where §03 and §04 say a control "is tested", the anchor is the test's source, not its result.

Two corrections were made mid-audit. They are stated here once, forward-facing, so that no earlier section is read as still holding its first draft: **the root `conftest.py:153`/`:162` fixture does set `TRUST_TEST_HARNESS` process-wide** — the harness is the default state of the suite, not an opt-in — and **containers are swept on a tested timer** (`web_ui/backend/server.py:1172`, `:823-839`), with only the *worker* reaper env-gated off. Both corrections narrow a claim rather than widen one; neither changes a Tier W item in §07.

If exactly one claim in this document is wrong, the one that would most change the conclusions is **D1** — that a session holding `banned` on `credential` can still obtain one, because `read` ranks below nothing that blocks injection and the executor injects after the gate has decided. Re-check §06 first; if D1 does not hold, W1 is worth an hour less than §07 says it is.

