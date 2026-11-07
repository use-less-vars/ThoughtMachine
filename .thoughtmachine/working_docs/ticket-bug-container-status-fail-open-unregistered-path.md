# Ticket: bug/container-status-fail-open-unregistered-path

**Filed:** 2026-10-25, from the Gen 50 client-claim red team pass at `24012a89` (base commit; handlers read in full).
**Severity:** MEDIUM — a status read for an unregistered workspace path answers with fully-permissive capabilities and the raw build log instead of an error.
**Status:** open; verified by read at `24012a89`, not exercised at runtime — `UNRUN: no docker` in the auditing environment. Not to be fixed in this wave.

## Finding

`GET /api/container/status` (`web_ui/backend/server.py:4081`, `def container_status(workspace: str = "")` at `:4082`) passes the `workspace` query value straight into `get_container_status` (`infra/containers/executor.py:1512`, called at `:4091`) with no lookup that could reject an unknown path; an omitted parameter short-circuits earlier at `:4084-4085` with `unavailable`.

`get_container_status` resolves the path to a workspace id (`_resolve_workspace_id`, `:1541`) and fills `capabilities` from `_compute_effective_capabilities(normalised)` (`:1557`, returned at `:1612`), a one-line delegation to `_load_capabilities` (`:1690-1699`). That helper has three fail-open legs, all landing on the same permissive answer:

```python
1672:        if workspace_id is None:
1673:            return _default_capabilities()
1676:        if caps is None:
1677:            return _default_capabilities()
1686:    except Exception:
1687:        return _default_capabilities()
```

`_default_capabilities` (`:1717`) returns `allow_network: True`, `allow_docker: True`, `allowed_file_extensions: ["*"]`, `max_file_size_bytes: 0`; its docstring (`:1718`) calls these "fully-permissive capability defaults", and `get_container_status`'s own docstring names the same fallback (`:1522-1523`).

The response also carries build output: the return at `:1610-1615` includes `"build_log": build_log`, read from `_build_log_cache` (`:1598-1599`) or from a finished background rebuild (`:1604-1606`), and handed back unchanged at `:4092`; the error path returns the exception text the same way (`:4093-4095`).

So a path the server cannot map to any workspace is answered not with a refusal but with the most permissive description it can express, plus build output. The failure mode inverts: the less the server knows about the workspace, the more permission it reports.

## Bearing

The surface inverts its own question. A read is where a caller goes to learn policy, and here a reader asking "what may this workspace do" is told that network and docker are allowed and every extension is permitted precisely when the server holds no record of the workspace — the case whose honest answer is "unknown workspace". The three legs also collapse distinct situations (unmappable path `:1672`, no capabilities file `:1676`, any exception including an import failure `:1686`) into one answer, so the report cannot distinguish "unregistered" from "lookup raised".

## Blast radius

Any client that can route to the port can ask about any path and receive a permissive capability description plus that path's container build log, with no credential — build logs being build output, not policy, and returned verbatim on both the success and the error path. Nothing is written and no container is started.

## What this is not

- Not a write. No state changes; the route reads the build-log cache and returns.
- Not a capability grant by itself. Whether anything downstream consumes a status response as authority — a client that widens its own behaviour because the status said `allow_network: true` — was **not traced**; the consumer question is unestablished and no exploit chain is claimed. `UNRUN: no consumer of the status response traced`.
- Not exercised. `UNRUN: no requests were issued against a running backend`; every claim comes from reading the code.
- Not a full map of the `None` branch. The condition was established for the unregistered-path case: a non-empty `workspace` value that `_resolve_workspace_id` cannot map. Other routes into `workspace_id is None`, and other callers of `_load_capabilities`, were **not** enumerated. `UNRUN: other entries into the None branch not enumerated`.

## Fix shape

Not specified here. The answer should distinguish "no record of this workspace" from "capabilities unreadable" and refuse rather than describe, so that a client inferring policy from this surface cannot infer the widest one from the absence of a record; the build log belongs behind whatever gate the rest of the container surface ends up with.
