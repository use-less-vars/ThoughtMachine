// Focused test for the single-predicate button contract (W9).
// `workerRespawnPlan` is the ONE decision behind the button's label, its
// clickability and whether `handleForceRespawn` kills after applying. The
// handler recomputes this same object at click time, so these four states are
// the whole contract: if a state is not here, the button cannot express it.
import { describe, it, expect } from 'vitest';
import { workerRespawnPlan } from '../WorkerManagementPanel';

// Tier map copied from the backend's own vocabulary: one requires_respawn
// field (the system prompt), everything else hot_swap.
const TIERS = {
  system_prompt: 'requires_respawn',
  temperature: 'hot_swap',
  max_turns: 'hot_swap',
};

const REQUIRE_DEF = { name: 'coder', system_prompt: 'be terse', temperature: 0.2, max_turns: 5 };
const HOT_ONLY_DEF = { name: 'coder', temperature: 0.2, max_turns: 5 };

describe('workerRespawnPlan — the button contract', () => {
  it('promises nothing while the definition map is still loading', () => {
    const plan = workerRespawnPlan(undefined, TIERS, false);
    expect(plan.label).toBe('…');
    expect(plan.disabled).toBe(true);
    expect(plan.killAfterApply).toBe(false);
  });

  it('is disabled when the worker has no stored definition', () => {
    const plan = workerRespawnPlan(undefined, TIERS, true);
    expect(plan.label).toBe('No stored config');
    expect(plan.disabled).toBe(true);
    expect(plan.killAfterApply).toBe(false);
  });

  it('advertises the kill when a requires_respawn field is in the patch', () => {
    const plan = workerRespawnPlan(REQUIRE_DEF, TIERS, true);
    expect(plan.label).toBe('Apply & Kill Worker');
    expect(plan.disabled).toBe(false);
    expect(plan.killAfterApply).toBe(true);
  });

  it('promises no restart when every field is hot_swap', () => {
    const plan = workerRespawnPlan(HOT_ONLY_DEF, TIERS, true);
    expect(plan.label).toBe('Apply (no restart)');
    expect(plan.disabled).toBe(false);
    expect(plan.killAfterApply).toBe(false);
  });

  it('killAfterApply is exactly "the label advertises a kill"', () => {
    const plans = [
      workerRespawnPlan(undefined, TIERS, false),
      workerRespawnPlan(undefined, TIERS, true),
      workerRespawnPlan(REQUIRE_DEF, TIERS, true),
      workerRespawnPlan(HOT_ONLY_DEF, TIERS, true),
    ];
    plans.forEach((plan) => {
      expect(plan.killAfterApply).toBe(plan.label === 'Apply & Kill Worker');
    });
  });
});
