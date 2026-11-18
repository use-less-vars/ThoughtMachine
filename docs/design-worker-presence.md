# Worker presence: what "is this worker there?" means

Design record for the worker-presence work on branch `fix/worker-presence-contract` (HEAD `596eaefd`). Written for the next engineer who has to add a presence check, and for the reviewer who has to decide what the deferred items should become. Symbols are cited by name; a line number is only ever as good as the commit it was read at, and both files move.

## 1. The question

A worker is a `WorkerThread` object with a body that runs somewhere. In thread mode the body *is* that object's `run()` loop, in this interpreter. In process mode the body is a child interpreter and the `WorkerThread` the supervisor holds is only a handle to it. `DEFAULT_RUNTIME_MODE` is `MODE_PROCESS`, so process mode is the production case and thread mode is the degenerate one.

"Present" is therefore not "is the object alive"; it is: *Is there a body I can still reach with a query, and that still owns the state I am about to report on?*

Every caller who asked a narrower question — is the Python object alive, is the registry entry there, did the last frame arrive — got a proxy answer, and the proxies disagree with each other under process mode. That disagreement is the subject of this document.

## 2. Why `thread.is_alive()` is the wrong question

`WorkerThread.start` returns early when the worker is stamped for a non-thread runtime and is not the runtime child itself: the supervisor's thread is deliberately never started, because running it would put a second body beside the child. So for a healthy process-mode worker the supervisor-side object is a launcher that was never launched, and `is_alive()` is `False` — by design, always, including while the child is answering queries. The predicate is not measuring liveness under this substrate; it is measuring whether a thread that is not supposed to run has run.

Measured symptom, this branch: `CheckSystem running_workers` returned `alive: false` for two workers that were replying to queries, twice in one session. The workers were fine. The report was fine in form and wrong in substance, which is the worst kind of wrong for a status field: it is consumed by routing, dedupe, cap accounting and by a human reading a panel.

This was not one bug in one place. It was the same wrong question asked in six places, which is why it presented as flakiness rather than as a defect:

- four spawn-path registry reads — the dedupe gather, the double-checked re-check, the late cap re-check, and `_live_workers_in_session`;
- two payload assemblies — `check_system._query_running_workers`, and `Worker._action_check`, whose three `alive` fields (the foreign-session note, the cross-session list, the main payload) are one decision copied into three keys.

Six decisions, eight fields/reads. Counting lines would have called it eight, and the fix would have been applied twice to the same decision.

## 3. The invariant

Three parts. All three are needed; any two of them still allow a lying answer.

**(a) `worker_is_live` is the one predicate.** It lives in `tools/workspace/worker_runtime.py`, dispatches on the worker's stamped mode, and is the only function a caller may use to answer the question above. It is three-valued where it must be: the evidence it reads (`WorkerRuntimeHandle.alive`) is `Optional[bool]` — `True` alive, `False` dead, `None` unknown, meaning "no child built yet" or "the probe could not be taken" — and only identity-`False` counts as death. It **fails open**: a probe that raises is not death evidence, and an exception escaping the predicate must not become `False`. The asymmetry is deliberate. Reporting a live worker dead makes it unresolvable and unaddressable and manufactures a dead worker out of a hiccup (this is the bug). Reporting a dead worker live costs one failed dispatch, which the fence reports honestly at the point of use.

**(b) `WorkerRuntimeHandle.__init__` is the only writer of the stamp, and `mode` is required.** The handle stamps `worker._runtime_mode` in its constructor — that write is the only one in the tree — and `handle.mode` returns the same value. The constructor has no default for `mode`: a default does not make the mode *known*, it makes it *guessed*, and the guess would be "thread", which is the exact lie that started this. A caller that forgets now fails at construction with a `TypeError` instead of silently declaring its worker a thread-mode worker that every reader then believes.

**(c) Absent is UNKNOWN, not THREAD.** `declared_runtime_mode` in `worker_thread.py` is the reader that honours the distinction: it returns the mode only when the attribute is a string in `RUNTIME_MODES`, and `None` for absent, non-vocabulary, or duck-typed values (`unittest.mock.MagicMock` answers every attribute access, so a bare `getattr` hands back a `Mock` — not `None`, not `MODE_THREAD` — and callers that compare only against those two conclude "some non-thread mode"). Three readers still bypass it and read absent as thread: `polls_command`, `start`, and `worker_is_live` itself. Their behaviour is pinned (item 2 of section 5); it is a known hole, not an oversight.

## 4. The recipe: adding a presence check

1. **Classify the question.** "Is it there and reachable?" is presence. "Should this worker be picked / kept / capped?" is policy. "Do something to it" is a verb (section 5, item 1). Only the first is in scope for this invariant.
2. **Find the evidence, do not invent it.** The evidence is the stamp plus the handle for that instance. If you are about to read `_runtime_mode` yourself, stop: that is how the three bypassers came to be.
3. **Call `worker_is_live`.** Do not add a helper beside it, do not branch on the mode at your call site, and do not `getattr(handle, "alive", True)` — call the predicate.
4. **If the value has to reach output, keep the tri-state until the last line.** Collapse `None` to "live" at the point where a boolean is unavoidable, and say so in a comment at that line, so the next reader does not "fix" it into `False`.
5. **Prohibitions.** No second predicate. No fallback path that answers the question differently when the predicate says `None`. No `try: ... except Exception: return False`. Failing open is done once, inside `worker_is_live`; any fallback makes the answer depend on the caller, which is how six copies of this bug coexisted.
6. **Test with real objects, not a mock.** Build a real `WorkerThread` and attach a handle with `mode=MODE_PROCESS`, never starting the supervisor thread, and assert your check reports it live while `thread.is_alive()` is `False`. Pair it with a negative case that must read dead (a stopped thread, or a handle whose child is gone) so the test cannot pass by always answering "live".
7. **Prove the check bites.** Revert your call to `thread.is_alive()` and watch the test fail. If it still passes, it is measuring the fixture, not the system — that is exactly how the original defect stayed green for a week (section 6).

## 5. Deliberately not fixed on this branch

Four items. None of them is a predicate swap, and each needs its own review.

**1. The verbs.** `_wait_for_worker_exit`, the `worker_timeout` waits, the force-stop path around `survived_force_stop`, `_action_join`, `_action_pause`, `_action_stop`, `_action_reset`, the query-delivery waits, and the registry read behind teardown. These ask "do something to it", not "is it alive", and the two substrates answer differently: a thread is stopped by join or interrupt, a process by a signal and a reap. Substituting `worker_is_live` into them would turn a lying status into a lying *action* — the supervisor would believe it had stopped something it never signalled, and the next spawn would collide with a live body. The fix is a verb layer with a per-mode implementation (stop, is-stopped, wait-for-exit) and a decision about what "stopped" means when the child cannot be reaped.

**2. Making an absent stamp loud in `polls_command`, `start`, `worker_is_live`.** All three read `getattr(..., MODE_THREAD)` (or `None`, in `worker_is_live`'s case, compared against `MODE_PROCESS`), so an unmarked worker keeps the legacy in-process behaviour. That is pinned: `start`'s contract says an unmarked worker starts normally, `polls_command` must keep exactly one body claiming the command channel, and `test_worker_process_contract` asserts both sides. Changing any of the three requires a prior decision about what an unmarked `WorkerThread` *means* — legacy thread worker, or a bug to be refused — and that decision applies to every test that constructs a worker without a handle. It is a policy change wearing a one-line costume.

**3. Renaming the `alive` key.** `alive` now means "reachable body", not "the Python object is running"; the name is now the least informative part of the field. A rename is a breaking change to `CheckSystem` output, which is consumed by agents and by the UI, so it needs a deprecation window: emit both keys for a release, document that the old one is the collapsed tri-state, then remove.

**4. `worker_is_dispatchable` duplicates the thread branch.** It re-implements the mode decision inline rather than composing on `worker_is_live`, because it also answers a policy question ("is a query allowed to go here right now?"). The duplication is tolerable for one release and should not be copied: when it is touched, it should compose — ask `worker_is_live` for presence and add the policy clause on top — so there is one place where the mode is interpreted.

## 6. How this bug hid for a week

The tests were green. They were green because the failure modes were absorbed by the fixture, not answered by the system.

The cap test's autouse teardown joins a real `WorkerThread` for five seconds. Under process mode that thread was never started, so the join cannot return until it times out: every test in the file pays five seconds and then proceeds, and the assertions inside the body are trivial enough to pass either way. Run one test alone and it passes in five seconds. Run the file and the reaper kills the process partway through, which reads as "flaky on CI", not "this fixture cannot stop what it created".

That fixture is the same gap as the deferred verbs in section 5: nothing in this codebase can stop a process-mode worker, so the thing that was supposed to tear down the worker could only wait and give up. A green suite was evidence about the fixture, not about the system, and the five-second silence was the system saying so.

## 7. Open questions

1. Should a `WorkerThread` that never had a handle attached be treated as a legacy thread worker, or refused at the first presence query? Section 5 item 2 cannot be scheduled until a reviewer answers this, because the answer decides which existing tests are pinning behaviour and which are pinning a bug.
2. Can a process-mode worker be stopped and reaped reliably enough today for `_wait_for_worker_exit` and `survived_force_stop` to report the truth, or does the supervisor's teardown need the verb layer first?
3. Is keeping `alive` (collapsed, `None` to true) plus a new explicit presence field an acceptable deprecation, and for how long?
4. How much of the current suite is thread-mode-only by construction, and would a single mandated process-mode variant of the spawn and presence tests have caught all six sites before they shipped?
