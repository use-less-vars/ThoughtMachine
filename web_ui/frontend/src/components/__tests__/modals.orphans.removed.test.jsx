/**
 * Deletion contract — retired workspace modals.
 *
 * Retired: 2026-09-22
 * Reason: ZERO IMPORTERS. Note the criterion is *zero importers*, not *marked orphaned*:
 *   only ResourceCatalogModal.jsx ever carried an orphan marker; CredentialPickerModal.jsx
 *   and WorkerEditorModal.jsx were unmarked and were still dead. Re-confirmed this session:
 *   importer count 0, test-import count 0 (grep by symbol), zero `React.lazy(`/`require(`/
 *   `export * from` indirection anywhere under src, and no `*index*` barrels under src.
 *
 * These three files are INTENTIONALLY ABSENT. A failure here is not a broken test — it is the
 * contract asserting the deletion held. If you bring any of these back, delete that assertion
 * and justify the reintroduction in the commit message.
 */
import { existsSync } from 'node:fs';
// NOTE: this repo does not enable vitest `globals` (vite.config.js has no `test`
// block), so every existing test file imports its helpers from 'vitest' explicitly.
// The bare `test`/`expect` shape from the task brief produced a collection error
// (`ReferenceError: test is not defined`); importing them is the fix and keeps the
// file to exactly three tests.
import { test, expect } from 'vitest';

const modalsDir = new URL('../workspace/modals/', import.meta.url);

test('CredentialPickerModal.jsx is retired (file must not exist)', () => {
  expect(existsSync(new URL('CredentialPickerModal.jsx', modalsDir))).toBe(false);
});

test('ResourceCatalogModal.jsx is retired (file must not exist)', () => {
  expect(existsSync(new URL('ResourceCatalogModal.jsx', modalsDir))).toBe(false);
});

test('WorkerEditorModal.jsx is retired (file must not exist)', () => {
  expect(existsSync(new URL('WorkerEditorModal.jsx', modalsDir))).toBe(false);
});
