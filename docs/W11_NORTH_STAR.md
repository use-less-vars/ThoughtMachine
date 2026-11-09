
## W11 North Star Example: The EM-Solver (Native Host Resource)
*This is the target use case for W11-W13.*
- **The Need:** A C++ OpenMP solver runs 50x slower in Docker due to cgroup CPU caps. It must run natively on the host.
- **The Vision:** The user builds a solver, registers it as a Host Resource, and the agent uses it safely.
- **W11 (Host vs Container):** The resource declares `run_location: host`.
- **W13 (Security):** The solver executes via `host_port.py`. The resource definition specifies allowed executables and argument schemas (Bins).
- **W12 (Async):** Solver runs take minutes. The agent uses `spawn -> job_id -> poll -> fetch`, not a blocking call.
- **Provenance:** The tool stamps `{value, run, cells, anchor, git_rev}` per scalar. The agent never hallucinates memory of what ran.
