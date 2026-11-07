# Research: Phase 7 — Worker Configuration (r6)

## Status: ✅ Updated for Phase 7

### Summary

Worker configuration defines the runtime behaviour of sub-agents (coder, reviewer, researcher). In Phase 7, the three per-role template files were consolidated into a single `default.json`.

### Key Changes (Phase 7)

#### Before
- `resources/worker_templates/coder.json` — coding specialist, temperature 0.2
- `resources/worker_templates/reviewer.json` — code reviewer, temperature 0.1
- `resources/worker_templates/researcher.json` — codebase researcher, temperature 0.3
- Each with distinct tool access, temperature, and file permissions.

#### After
- `resources/worker_templates/default.json` — unified template for all workers
- The three old templates moved to `temp/stale_worker_templates/` for reference.
- `MANIFEST.json` description updated.
- Docstring in `tools/workspace/worker.py` updated.

### Implications

- **Worker roles are now identity-only**: The `worker_name` parameter ("coder", "reviewer", "researcher") still determines where output appears but no longer loads a role-specific template. All workers use the same default configuration.
- **Tool access is now uniform**: Previously, `reviewer` had read-only filesystem access while `coder` had write access. Now all workers share the same tool permissions from `default.json`.
- **Backward compatibility**: Existing worker spawn calls with `worker_name="coder"` etc. still work. No API signature change.
- **Future enhancement**: Role-specific templates could be re-added later if needed, but the current design favours simplicity.

### Files Changed

| File | Change |
|------|--------|
| `resources/worker_templates/coder.json` | Moved to `temp/stale_worker_templates/` |
| `resources/worker_templates/reviewer.json` | Moved to `temp/stale_worker_templates/` |
| `resources/worker_templates/researcher.json` | Moved to `temp/stale_worker_templates/` |
| `resources/worker_templates/default.json` | Unchanged (was already the base template) |
| `resources/MANIFEST.json` | Description updated |
| `tools/workspace/worker.py` | Docstring updated |
