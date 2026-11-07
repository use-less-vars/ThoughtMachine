# Ticket: bug/checksystem-version-reports-loaded-revision-not-head

**Filed:** Gen 47, during wave-4 PR 4 (#162); `dev` = `d60ffc92`.
**Verified at:** `CheckSystem query='version'` returned revision `8efd798b` while the checked-out HEAD was `5e5c8f11`. `8efd798b` is `5e5c8f11`'s parent — the commit the server process loaded at start.
**Source:** my own runtime check during PR 4. Reproducible: commit, query again, and revision does not move until the process is restarted.
**Severity:** LOW — the field is correct for what it measures. The failure mode is misreading, not misinformation.
**Status:** filed, not dispatched. Documentation-only fix.

## Defect

`query='version'` returns `{revision, branch, dirty}`. `revision` is the commit the running process imported, snapshotted at start; it does not track commits made afterwards. Nothing in the payload says so, and `branch` and `dirty` invite the reading that the triple describes the **current** checkout.

Honest, not wrong: the server did load `8efd798b`, and committing `5e5c8f11` does not reload it. But a reader who takes `revision` for HEAD will conclude a merge is unbuilt, or that a commit "did not take" — which is exactly what happened during PR 4, where the revision appeared to lag a merge that had in fact landed as intended.

## Fix shape

Documentation only; no behaviour change. Add to the `query='version'` response or its help text:

> revision is the code loaded when the server process started; it does not track commits made afterwards. Restart to update.

Optional and not required: rename the field to something self-describing (`loaded_revision`) or add a load timestamp. One sentence removes the misreading; the rename is only worth it if the surface is being touched anyway.

## Bearing

LOW. Filed because the misreading cost real time during PR 4 and will recur on every merge. Cheap to close, cheap to leave; recorded so the next reader does not rediscover it.
