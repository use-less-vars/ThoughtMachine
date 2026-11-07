# Brief 1: The Frontend CI Gate

**Goal:** Make CI actually run the frontend test suite (658 tests as of `fcd569ab`).
**File:** `.github/workflows/platform-matrix.yml`
**Change:** Add a job `web-ui-frontend-suite`. It should `cd web_ui/frontend`, run `npm install`, and run `npm test`. (Model it after `web-ui-backend-suite`.)
**Why:** `src/components/__tests__/permissionVocab.test.jsx` was red on `dev` for 5 days because CI ignored it.
