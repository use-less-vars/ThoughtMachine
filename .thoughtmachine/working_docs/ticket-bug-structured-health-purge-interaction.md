bug/structured-health-purge-interaction
Fixture: tests/test_structured_health.py::server_module
Behaviour: converting its sys.modules purge/teardown to the SAVE-RESTORE shape leaks 1 regression member on its own; f2 + any half-B conversion leaks 3 more (4 total vs baseline).
Minimal repro: .thoughtmachine/working_docs/edge1-bisect/repro/ (self-contained synthetic; no real thoughtmachine package).
Frame: the same 2-hunk fix is inert in 5 of the 6 fixtures and harmful in 1 -- a context-dependent divergence, not a local defect.
Tag: pass-2 evidence, not hygiene.
Cross-ref: f2 is the sole harmful fixture in the class of 6.
