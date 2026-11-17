# probe_marker_teardown.md — C76 trust-harness marker/teardown probe

Diagnostic only. `conftest.py` was temporarily instrumented, then restored (sha `1534bcb6…`, 232 lines).
Evidence: `probe_run_M1..M4.log` (deleted after the report was written; every number below is copied from them).
All line numbers below refer to the RESTORED 232-line `conftest.py`.

## 1. The instrumented code and the reference the hook holds

`conftest.py:57`:

```python
from web_ui.backend import auth_boundary
```

That is a **`from … import` of the MODULE OBJECT** into conftest's own globals. It is bound once, at conftest
import time, and it is the *held* reference for the whole session. The probe's `held` side uses exactly this
name; the `live` side uses `sys.modules.get("web_ui.backend.auth_boundary")`. `held is live` therefore answers
"has the app been re-imported under conftest's feet".

`conftest.py:206-232` (unmodified hooks; probe calls were inserted at the four marked stages and removed again):

```python
206: _PREVIOUS_TRUST = pytest.StashKey[bool]()
209: @pytest.hookimpl(tryfirst=True)
210: def pytest_runtest_setup(item):
216:     if item.get_closest_marker("trust_harness") is None:
217:         return
218:     item.stash[_PREVIOUS_TRUST] = auth_boundary.TRUST_TEST_HARNESS
219:     auth_boundary.enable_test_harness_trust(True)
222: @pytest.hookimpl(trylast=True)
223: def pytest_runtest_teardown(item):
230:     if item.get_closest_marker("trust_harness") is None:
231:         return
232:     auth_boundary.enable_test_harness_trust(item.stash[_PREVIOUS_TRUST])
```

Machinery the probe printed alongside (`≈` = line numbers not re-verified after restore): `_suite_trust`
(single module-level dict, `≈65`) is the one authoritative value; `_remembering` (`≈75-101`) wraps
`enable_test_harness_trust` so that **every** flip, from any module object, also writes `_suite_trust`;
`_apply_suite_trust` (`≈103-107`, called at `153: _apply_suite_trust(auth_boundary)`) gives a module the suite
value and the recording setter; `_TrustRestoringLoader.exec_module` (`≈112-120`) re-applies `_suite_trust` to
**every freshly imported** `web_ui.backend.auth_boundary`.

Probe stages: `setup_entry`, `setup_after_write`, `teardown_entry`, `teardown_after_write`, plus
`unmarked_setup_entry` / `unmarked_teardown_entry` — two extra stages beyond the four required, added because the
victim is UNMARKED and the marked-only guard would otherwise hide the victim's own setup state.

## 2. Order dependency (instrumentation OFF, plain `-p no:cacheprovider`)

A = `test_persistence_workspace_permission_ceiling_survives_restart` (marked)
S = `test_persistence_session_permission_grants_survive_restart_capped_by_ceiling` (marked)
V = `test_persistence_provider_settings_survive_restart` (UNMARKED victim)

| run | order | result | first failure |
|---|---|---|---|
| R1 | A alone | **1 passed** | — |
| R2 | V alone | **1 passed** | — |
| R3 | A → V | **1 failed, 1 passed** | V: `AssertionError: get providers failed: 401 {"error": "unauthorized"}` (harness.py:392) |
| R4 | V → A | **2 passed** | — |
| R5a | S alone | **1 passed** | — |
| R5b | S → V | **1 failed, 1 passed** | V: `AssertionError: get providers failed: 401 {"error": "unauthorized"}` (harness.py:392) |
| R5c | V → S | **2 passed** | — |

Both marked restart tests poison the unmarked restart test that runs **after** them; victim-first is clean.
(The same file with all three markers stripped is 8 passed, so the markers are the trigger.)

## 3. PROBE_MARKER measurements (verbatim, one run per log)

Module ids are per-process; only the equalities matter.

### M1 — V alone → 1 passed (`probe_run_M1.log`, 2 PROBE lines)
```
stage=unmarked_setup_entry    nodeid=V module_held=135933871093728 module_live=135933871093728 same=True  flag_held=False flag_live=False suite_trust_held=False suite_trust_live=False
stage=unmarked_teardown_entry nodeid=V module_held=135933871093728 module_live=135933871093728 same=True  flag_held=False flag_live=False suite_trust_held=False suite_trust_live=False
```

### M2 — A alone → 1 passed (`probe_run_M2.log`, 4 PROBE lines)
```
stage=setup_entry         nodeid=A module_held=129087693289440 module_live=129087693289440 same=True flag_held=False flag_live=False suite_trust_held=False suite_trust_live=False
stage=setup_after_write   nodeid=A module_held=129087693289440 module_live=129087693289440 same=True flag_held=True  flag_live=True  suite_trust_held=True  suite_trust_live=True
stage=teardown_entry      nodeid=A module_held=129087693289440 module_live=129087693289440 same=True flag_held=True  flag_live=True  suite_trust_held=True  suite_trust_live=True
stage=teardown_after_write nodeid=A module_held=129087693289440 module_live=129087693289440 same=True flag_held=False flag_live=False suite_trust_held=False suite_trust_live=False
```
Note `setup_entry` reads **False/False**: the hook is `tryfirst`, so it runs *before* the session-scoped autouse
fixture `_trust_the_in_process_asgi_harness` installs `True`. `item.stash[_PREVIOUS_TRUST]` therefore captures
the **shipped default False**, not the value in force during the test. `teardown_after_write` proves the restore
lands back on False — and, through the `_remembering` wrapper, drags `_suite_trust` to False with it.

### M3 — A → V → **1 failed, 1 passed** (`probe_run_M3.log`, 6 PROBE lines)
```
stage=setup_entry          nodeid=A module_held=125603203807200 module_live=125603203807200 same=True  flag_held=False flag_live=False suite_trust_held=False suite_trust_live=False
stage=setup_after_write    nodeid=A module_held=125603203807200 module_live=125603203807200 same=True  flag_held=True  flag_live=True  suite_trust_held=True  suite_trust_live=True
stage=teardown_entry       nodeid=A module_held=125603203807200 module_live=125603111389040 same=False flag_held=True  flag_live=True  suite_trust_held=True  suite_trust_live=True
stage=teardown_after_write nodeid=A module_held=125603203807200 module_live=125603111389040 same=False flag_held=False flag_live=True  suite_trust_held=False suite_trust_live=False
stage=unmarked_setup_entry nodeid=V module_held=125603203807200 module_live=125603111389040 same=False flag_held=False flag_live=True  suite_trust_held=False suite_trust_live=False
stage=unmarked_teardown_entry nodeid=V module_held=125603203807200 module_live=125603203807200 same=True flag_held=True flag_live=True suite_trust_held=True suite_trust_live=True
```
Failure: `V: AssertionError: get providers failed: 401 {"error": "unauthorized"}` (harness.py:392).

### M4 — S → V → **1 failed, 1 passed** (`probe_run_M4.log`, 6 PROBE lines)
```
stage=setup_entry          nodeid=S module_held=123949446133728 module_live=123949446133728 same=True  flag_held=False flag_live=False suite_trust_held=False suite_trust_live=False
stage=setup_after_write    nodeid=S module_held=123949446133728 module_live=123949446133728 same=True  flag_held=True  flag_live=True  suite_trust_held=True  suite_trust_live=True
stage=teardown_entry       nodeid=S module_held=123949446133728 module_live=123949342570624 same=False flag_held=True  flag_live=True  suite_trust_held=True  suite_trust_live=True
stage=teardown_after_write nodeid=S module_held=123949446133728 module_live=123949342570624 same=False flag_held=False flag_live=True  suite_trust_held=False suite_trust_live=False
stage=unmarked_setup_entry nodeid=V module_held=123949446133728 module_live=123949342570624 same=False flag_held=False flag_live=True  suite_trust_held=False suite_trust_live=False
stage=unmarked_teardown_entry nodeid=V module_held=123949446133728 module_live=123949446133728 same=True flag_held=True flag_live=True suite_trust_held=True suite_trust_live=True
```
Failure: identical 401 at harness.py:392. (Order-dependency R4/R5c, victim first, is 2 passed; M4's counts
re-confirm the poison direction: poisoner first ⇒ victim fails.)

## 4. The two questions, answered from the printed values

**(a) Is the live module a NEW object after the poisoner's restart?** **Yes.** In M3 the poisoner's
`teardown_entry` shows `module_held=125603203807200` vs `module_live=125603111389040`, `same=False`, and M4 shows
the same split (`…133728` vs `…570624`). Module churn is real. In M2 (same test, alone) `same=True` at every
stage, so the churn is produced by the poisoner's *restart*, not by the marker.

**(b) At the victim's setup entry, what is `TRUST_TEST_HARNESS` on the LIVE module?** **True.** M3:
`same=False flag_held=False flag_live=True`; M4 identical (`flag_live=True`). The live boundary module the app is
actually using is **armed** when the victim starts. The only anomalous value at that instant is
`suite_trust_held=False suite_trust_live=False` — the single authoritative value has already been poisoned by
the poisoner's teardown.
Also visible at the victim's own teardown: `same=True`, `flag_held=True` — harness restores `sys.modules` on exit,
so the object created *inside* the victim's body is not observable at teardown; the 401 is produced inside the
body, not at the boundary of the test.

## 5. VERDICT — **DIFFERENT-MECHANISM**

The predicted mechanism (teardown restores onto the stale module object and leaves the LIVE module False, so the
victim meets a closed gate) is **refuted by the flags**: at the victim's setup entry the live module is a new
object (`same=False`) but it is still **`flag_live=True`** — the stale write never disarmed the live module, and
the CONFIRMED signature (`flag_live=False` at the victim's entry) appears nowhere in M1-M4. What *is* poisoned at
that instant is `_suite_trust=False`, because `pytest_runtest_setup` is `tryfirst` and therefore reads
`auth_boundary.TRUST_TEST_HARNESS` **before** the session-scoped autouse fixture installs `True`, so
`item.stash[_PREVIOUS_TRUST]` captures the shipped default `False`; `pytest_runtest_teardown` then restores that
`False` through the `_remembering` wrapper, which writes it into the single authoritative `_suite_trust` dict.
Every subsequent import of `web_ui.backend.auth_boundary` is re-armed from `_suite_trust` by
`_TrustRestoringLoader.exec_module`, and the victim is itself a `*_survives_restart` test: its own restart purges
`web_ui.backend` and the module it gets back carries trust **False**, so its first uncredentialed request is a
401 — while an unmarked victim cannot repair this, because the hooks are strict no-ops for unmarked items and the
session fixture has already run. M1 is the control that isolates this: alone, the victim also enters with
`suite_trust=False`, but it is the session fixture's *first* setup, which turns `_suite_trust` True before the
body, so its restart re-imports True and it passes. One inference remains explicit rather than printed: the
in-body module's flag is not directly measured (the probe fires only at hook points, and harness restores
`sys.modules` before teardown); the single measurement that would close it is a probe inside
`_TrustRestoringLoader.exec_module` printing the value it re-applies, or the flag read from the boundary the
victim's TestClient actually resolves mid-body.

## 6. Full-file run (round 2) — where the poison actually comes from

The one measurement round 1 left open: the whole file at once, so the three marked tests do **not** run first.

```
python3 -m pytest -s -p no:cacheprovider tests/integration/test_config_control_matrix.py
=> 3 failed, 5 passed, 26 warnings in 8.10s
```
36 `PROBE_MARKER` lines: 8 `setup_entry`, 3 `setup_after_write`, 8 `teardown_entry`,
3 `teardown_after_write`, 8 `remember`, 6 `reimport_applied` — i.e. every hook point and
every write to `_suite_trust` in the run. (`tests/integration/test_config_control_matrix.py::`
stripped from `nodeid`, `PROBE_MARKER ` prefix stripped, worktree path stripped from
`caller`; the `caller=conftest.py:NNN` numbers refer to the *instrumented* copy that was
loaded during the run, and the `caller_fn` name plus this key identifies the site:
`:248` = `pytest_runtest_setup`'s `enable_test_harness_trust(True)`, `:263` =
`pytest_runtest_teardown`'s restore from the stash, `:180` = the session fixture's body
(`enable_test_harness_trust(True)`), `:184` = the session fixture's `finally`
(`enable_test_harness_trust(previous)`).)

### 6.1 Per-test order (marked / unmarked) and outcome

| # | test | marked | outcome |
|---|------|--------|---------|
| 1 | `test_persistence_workspace_permission_ceiling_survives_restart` | **marked #1** | passed |
| 2 | `test_persistence_session_permission_grants_survive_restart_capped_by_ceiling` | **marked #2** | passed |
| 3 | `test_persistence_session_worker_timeout_survives_restart` | unmarked | **FAILED** — `get providers failed: 401 {"error": "unauthorized"}` |
| 4 | `test_persistence_provider_settings_survive_restart` | unmarked | **FAILED** — `get providers failed: 401` |
| 5 | `test_persistence_allow_host_resources_survives_restart` | unmarked | **FAILED** — `workspace summary failed: 401` |
| 6 | `test_provider_timeout_propagates_to_llm_client` | unmarked | passed |
| 7 | `test_session_worker_timeout_propagates_to_workermanager` | unmarked | passed |
| 8 | `test_workspace_ceiling_caps_session_effective_permission` | **marked #3** | passed |

Marked tests 1 and 2 pass, and the three victims that follow them fail — the 3 failed /
5 passed shape is reproduced exactly. Tests 6 and 7 pass because they never restart the
app, so they never ask the import hook for a value.

### 6.2 Complete ordered `stage=remember` list — every write to `_suite_trust`

```
1  value=True   caller=conftest.py:248 caller_fn=pytest_runtest_setup            (T1 setup hook: marker arms)
2  value=True   caller=conftest.py:180 caller_fn=_trust_the_in_process_asgi_harness (T1 setup: session fixture body)
3  value=False  caller=conftest.py:263 caller_fn=pytest_runtest_teardown         (T1 teardown: restores stash=False)  <-- THE FLIP
4  value=True   caller=conftest.py:248 caller_fn=pytest_runtest_setup            (T2 setup hook)
5  value=False  caller=conftest.py:263 caller_fn=pytest_runtest_teardown         (T2 teardown: restores stash=False)
6  value=True   caller=conftest.py:248 caller_fn=pytest_runtest_setup            (T8 setup hook)
7  value=True   caller=conftest.py:184 caller_fn=_trust_the_in_process_asgi_harness (session fixture finally: previous=True)
8  value=False  caller=conftest.py:263 caller_fn=pytest_runtest_teardown         (T8 teardown: restores stash=False)
```

Two facts fall straight out of it. **The in-body flip does not exist**: there is no
`stage=remember value=False` anywhere except the three marked teardowns (`:263`), so no
test turns trust off from inside its own body — `_harness_trust` is never used by this
file. And the session fixture remembered `previous=True` (line 7 prints the value it
restores, and it is `True`), because it ran *after* the T1 setup hook had already armed
the module — so even the session's own restore leaves `_suite_trust` at whatever the
last marked teardown wrote.

### 6.3 `teardown_after_write` → next `setup_entry` (the poisoned hand-offs)

```
(P) teardown_after_write nodeid=test_persistence_workspace_permission_ceiling_survives_restart  same=False flag_held=False flag_live=True  suite_trust=False stash_prev=False
(N) setup_entry         nodeid=test_persistence_session_permission_grants_survive_restart_capped_by_ceiling same=False flag_held=False flag_live=True  suite_trust=False stash_prev='<none>'

(P) teardown_after_write nodeid=test_persistence_session_permission_grants_survive_restart_capped_by_ceiling same=False flag_held=False flag_live=True  suite_trust=False stash_prev=False
(N) setup_entry         nodeid=test_persistence_session_worker_timeout_survives_restart same=False flag_held=False flag_live=True  suite_trust=False stash_prev='<none>'

(P) teardown_after_write nodeid=test_workspace_ceiling_caps_session_effective_permission same=True  flag_held=False flag_live=False suite_trust=False stash_prev=False
(N) — end of run —
```

The three victims that follow a marked test (T3, T4, T5) all enter with **`suite_trust=False`**
and no stash, and — importantly — T3, the *first* victim, still enters with
**`flag_live=True`**, exactly as in round 1's M1–M4. The stale-but-armed live object is
what makes the two no-restart tests (T6, T7) pass; it does not save a
`*_survives_restart` test.

### 6.4 Every `stage=reimport_applied` line — the value the hook hands a restarted boundary

```
1  new_module=126441106464720 value=True    (T1 body — restart 1)
2  new_module=126441058045040 value=True    (T1 body — restart 2)
3  new_module=126441038622496 value=True    (T2 body)
4  new_module=126441036602864 value=False   (T3 body — the 401)
5  new_module=126441033363472 value=False   (T4 body — the 401)
6  new_module=126441030810128 value=False   (T5 body — the 401)
```

This is the probe §5 asked for and did not have. It splits cleanly on
`_suite_trust` at the time of the re-import: while the last marked test left `True`, the
restarted boundary comes back armed (`True`), and the marked tests pass; after the last
marked teardown left `False`, **every** restarted boundary comes back armed `False` and
the very next uncredentialed request is a 401. The boundary the victim's TestClient
actually resolves mid-body is therefore measured, not inferred: it is the object built
by `_TrustRestoringLoader.exec_module`, and its flag equals `value=` on that line.

Side observation (marked test #3, `test_workspace_ceiling_caps_session_effective_permission`,
`setup_after_write … flag_held=True flag_live=False`): the setup hook writes only the
module conftest holds, so if the live `sys.modules` entry is still a restarted copy the
hook's write does not reach it — that test nonetheless passes because its own env
restores `sys.modules` (no `reimport_applied` line is printed for T8, and its
`teardown_entry` reads `same=True`). The hooks' write path therefore has a second,
independent weakness (write reaches one object, tests may resolve another), but it is
not what breaks the victims here.

### 6.5 The three questions

**(i) Stash value captured for the marked tests.** Marked #1 (`test_persistence_workspace_permission_ceiling_survives_restart`): **`False`** — its `setup_entry` reads `flag_held=False flag_live=False suite_trust=False stash_prev='<none>'` and the `setup_after_write` immediately after the hook's stash write reads **`stash_prev=False`**. Marked #2 (`…capped_by_ceiling`): **`False`**. Marked #3 (`test_workspace_ceiling_caps_session_effective_permission`): **`False`**. All three stash `False`, but for two different reasons: #1 because `pytest_runtest_setup` is `tryfirst` and reads `auth_boundary.TRUST_TEST_HARNESS` *before* the session autouse fixture installs `True` (its setup entry shows the shipped `False` and there is no `remember` before it); #2 and #3 because their predecessor's teardown had already put the module back to `False`.

**(ii) The single event that set `_suite_trust=False` before the failing restarts.** A **marked teardown**, not an in-body flip: line 3 of §6.2 — `value=False caller=conftest.py:263 caller_fn=pytest_runtest_teardown`, i.e. `pytest_runtest_teardown`'s `auth_boundary.enable_test_harness_trust(item.stash[_PREVIOUS_TRUST])` running with the `False` that marked test #1's `tryfirst` setup hook had stashed. There is no other `value=False` write in the run, so lines 5 and 8 are the same event repeating. The chain is then inert: `_suite_trust=False` → no unmarked item can repair it (hooks are no-ops for unmarked items, and the session fixture has already run) → every `*_survives_restart` victim's restart re-imports via `_TrustRestoringLoader.exec_module` with `value=False` (§6.4 lines 4–6) → 401.

**(iii) §5 STANDS.** Evidence line, verbatim from this run:
`stage=reimport_applied new_module=126441036602864 value=False` (T3's restart, immediately followed by `F`) — with `stage=teardown_after_write nodeid=test_persistence_workspace_permission_ceiling_survives_restart … suite_trust=False stash_prev=False` as its cause.
§5's causal chain — `tryfirst` setup stashes the shipped `False` → marked teardown writes that `False` into `_suite_trust` → a later restart re-imports armed `False` → unmarked victim 401s — is now fully printed, including the last link §5 listed as "one inference remains explicit rather than printed". Its label `DIFFERENT-MECHANISM` also stands: at the first victim's entry `flag_live=True` (the stale object is still armed), so the failure is not an entry-time closed gate on a stale write; it is the restart re-arm. One refinement worth recording rather than an amendment: for victims #2 and #3 (#6.3, T4/T5) the "CONFIRMED signature" `flag_live=False` *does* appear at setup entry, but it is a *consequence* — the previous victim's restart installed the disarmed copy — not the cause of the first failure.
