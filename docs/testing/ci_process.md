# CI and Branch Process

> **Branches push to origin before merge. Merge to `dev` only after CI is green on the pushed branch.**

This rule closes an enforcement gap: a branch can be merged locally without ever
being pushed, so CI never runs on it and the merge to `dev` lands unverified.
Pushing the branch first guarantees that the exact revision being merged has
already run and passed CI on origin.
