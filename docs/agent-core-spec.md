# ThoughtMachine agent core specification

This document specifies the agent core of ThoughtMachine as implemented in this tree, and follows a single prompt from process start to the point at which an Agent exists and a turn can begin.

It exists so that an engineer can locate a mechanism before changing it, without holding the whole call graph in memory.

## Contents

1. Purpose, scope and provenance
2. Boot: from process start to the first prompt
3. Agent construction and the object graph
4. Configuration resolution and precedence
5. Messages and conversation history
6. The turn loop: from query to committed turn
7. Streaming, events and observers
8. Tool dispatch and the gate chain
9. The tool catalog
10. Execution substrates
11. Workers, sub-agents and delegation
12. Context economics: token accounting and compaction
13. Session state, persistence and trust boundaries
14. Failure modes, invariants and the extension guide

Citation conventions used throughout: line numbers are 1-based against the checkout named in Section 1 and ranges are inclusive; a citation names where a mechanism is implemented rather than where it is called from unless the text says otherwise; paragraphs are one line each and are never hard-wrapped, so one paragraph is one line in a diff; and no behaviour is inferred from a function name, because every claim below was read at the cited location.

## 1. Purpose, scope and provenance

In scope: the process entry point, the server boot sequence up to the construction of an Agent, agent/core/, agent/controller/__init__.py, agent/config/, security/, and the two thoughtmachine helpers that the server calls during startup.

Out of scope, and referenced only where a core mechanism leaves the core: the browser frontend under web_ui/backend/static, the tool inventory under tools/, the provider adapters under llm_providers/, and the packaging and install scripts.

This document was read against commit 643fc100 on branch dev. In the agent sandbox .git is a 0-byte file rather than a directory, so that commit is asserted by the maintainer checkout and is not independently verified from inside the sandbox; the line numbers below come from that checkout.

Provenance of the sections: the section headings below are authoritative for what this document covers, and each section states its own status in its own first line, so a placeholder is visible where it occurs rather than being asserted once here. Every claim below was read from the working tree rather than recalled, and the cited commit identifier is asserted from the repository record rather than verified, because .git is a zero byte file in this checkout so no history can be read from it.

A reading warning, because it is the one place where a directory listing misleads: agent/controller/__init__.py is not a package marker. The entire AgentController implementation lives inside that single __init__.py, and Agent objects are constructed there at agent/controller/__init__.py:594 and at agent/controller/__init__.py:637. An engineer navigating by filename will see one file called __init__.py and skip it, which skips the controller.

The package is flat and its contents are these: agent/core/__init__.py, agent/core/agent.py, agent/core/conversation_manager.py, agent/core/debug_context.py, agent/core/llm_client.py, agent/core/message.py, agent/core/message_utils.py, agent/core/state.py, agent/core/token_counter.py, agent/core/tool_executor.py, agent/core/turn_transaction.py and agent/core/worker_context.py, with no subpackages, so the core reads as a single directory of modules rather than a layered tree.

Sizes are the reason this document is written as cited statements rather than narrative: agent/core/agent.py is 1645 lines, agent/core/tool_executor.py is 815, web_ui/backend/bridge.py is 3190 and web_ui/backend/server.py is 6230 in this checkout, so a summary that did not point at lines would be unusable at the point of change.

Two files that a reader would reasonably expect to find on the boot path are not on it, and Section 2.9 states both with citations rather than leaving the omission implicit: agent/cli/main.py and agent/startup_health_check.py.

## 2. Boot: from process start to the first prompt

Boot is not a single function, and the Agent does not exist when the server starts. The server process initialises logging, the vault and a FastAPI application, and an Agent is constructed only when a client connects and starts a session; the sequence below is the order in which the code executes, not a logical grouping.

### 2.1 Process entry

thoughtmachine_entry.py is the process entry point, 60 lines long, and its last two statements are the import of the server main at thoughtmachine_entry.py:55 and the call to it at thoughtmachine_entry.py:56.

Everything before those two statements prepares the process rather than the agent: a stdio guard at thoughtmachine_entry.py:19-22, sys.path adjustment at thoughtmachine_entry.py:32-47, and insertion of the --serve-frontend argument at thoughtmachine_entry.py:50-52.

The consequence for the rest of this section is that boot leaves the agent package immediately. thoughtmachine_entry.py contains no reference to agent/cli/ and constructs no Agent, so the only continuation of the boot path is web_ui.

### 2.2 Uvicorn and the FastAPI application

The server main is defined at web_ui/backend/server.py:6168 and starts the process with uvicorn.run at web_ui/backend/server.py:6221, and the module is also runnable directly through the guard at web_ui/backend/server.py:6229.

The application object is created at web_ui/backend/server.py:1158 with FastAPI(, and its lifespan handler is async def lifespan(app: FastAPI) at web_ui/backend/server.py:731.

### 2.3 Lifespan: what happens before the first connection

Startup is logged first, at web_ui/backend/server.py:733-734, including a server revision string.

Console logging is configured by agent.logging.console.configure_console_logging at web_ui/backend/server.py:737-741, and a failure there is downgraded to a WARNING rather than aborting.

An EventLogger is constructed and started at web_ui/backend/server.py:743-751, stored on app.state.event_logger at web_ui/backend/server.py:747, and set to None when startup of the logger fails at web_ui/backend/server.py:751.

An agent logger is created with AgentConfig(enable_file_logging=False) at web_ui/backend/server.py:759-763 and then registered with two facades so that the server process has the same registered-logger path the agent process has: agent.logging.unified.set_logger at web_ui/backend/server.py:767-771 and thoughtmachine.security.set_logger at web_ui/backend/server.py:774-777.

The user vault is initialised next: thoughtmachine.bootstrap.ensure_user_defaults is imported at web_ui/backend/server.py:783, called at web_ui/backend/server.py:784, the number of files it touched is logged at web_ui/backend/server.py:786, and get_version is logged at web_ui/backend/server.py:787.

A read-only vault drift check follows at web_ui/backend/server.py:794-800, which reports schema drift without mutating the vault or failing startup.

The failure policy of the whole lifespan is uniform: each stage sits in its own try/except and degrades to a WARNING, so a broken logger or an uninitialisable vault does not stop the server from accepting connections, as at web_ui/backend/server.py:788-789.

### 2.4 Vault defaults, and what the bootstrap actually guarantees

ensure_user_defaults is defined at thoughtmachine/bootstrap.py:140-230 and runs a fixed sequence: ensure_vault_structure at thoughtmachine/bootstrap.py:171, ensure_vault_defaults at thoughtmachine/bootstrap.py:174, ensure_resource_build_files at thoughtmachine/bootstrap.py:182-188, manifest files at thoughtmachine/bootstrap.py:191-204, manifest directories at thoughtmachine/bootstrap.py:207-224, and ensure_global_kb at thoughtmachine/bootstrap.py:226-228.

Resource build files are copied with overwrite_existing=False at thoughtmachine/bootstrap.py:182-188, which is what makes the call safe to repeat on every restart: existing user files are left alone.

The vault location is resolved by _user_dir at thoughtmachine/bootstrap.py:19-32 and exposed as USER_DIR through a module-level __getattr__ at thoughtmachine/bootstrap.py:35-55; bundled resources are located by _resources_dir at thoughtmachine/bootstrap.py:104-118 and enumerated from the manifest by _load_manifest at thoughtmachine/bootstrap.py:60-71 and get_manifest at thoughtmachine/bootstrap.py:74-80.

get_version is at thoughtmachine/bootstrap.py:135-137, and the module is separately runnable as python -m thoughtmachine.bootstrap through main at thoughtmachine/bootstrap.py:251-286, which reports failure on stderr and returns non-zero at thoughtmachine/bootstrap.py:270-275.

### 2.5 From a websocket connection to a bridge

No bridge exists until a client connects. The websocket handler is websocket_endpoint at web_ui/backend/server.py:1250, and bridges are cached per session in a dictionary at web_ui/backend/server.py:1088 with a lookup helper at web_ui/backend/server.py:1093, so a reconnecting client reuses its existing bridge.

The bridge is constructed as WebAgentBridge(session_store=session_store) at web_ui/backend/server.py:1383, whose class is at web_ui/backend/bridge.py:470 and whose __init__ begins at web_ui/backend/bridge.py:476.

### 2.6 bridge.start(): from configuration to a constructed Agent

start is defined at web_ui/backend/bridge.py:1539 and is the function that turns configuration into a running Agent; the steps below are its actual order.

The session configuration is defaulted and then guarded: if no session config was passed and none was loaded, start raises RuntimeError at web_ui/backend/bridge.py:1551-1554.

The previous session reference is cleared at web_ui/backend/bridge.py:1556 before anything is rebuilt.

Credentials are resolved through the full config chain when the session config has no api_key: self._config_manager.resolve_full_config is called at web_ui/backend/bridge.py:1564-1570 with workspace_id, session_id and provider_id, the api_key is taken at web_ui/backend/bridge.py:1571-1572, base_url at web_ui/backend/bridge.py:1573-1574, and provider-specific timeout and retry settings are merged at web_ui/backend/bridge.py:1575-1580; the whole block is wrapped in a bare except that swallows failures at web_ui/backend/bridge.py:1581-1582, so a resolution error leaves the session config as it was rather than failing the start.

The AgentConfig is derived from the session config at web_ui/backend/bridge.py:1584 through session_config.to_agent_config, which is the boundary between session-level settings and the config object the Agent receives.

Workspace identity is filled in from the registry if the session config lacks it: workspace_id at web_ui/backend/bridge.py:1586-1588 and workspace_path from WorkspaceRegistry at web_ui/backend/bridge.py:1589-1599.

If a controller already exists, start delegates to it at web_ui/backend/bridge.py:1613-1634 and returns at web_ui/backend/bridge.py:1634, which means the Agent is built by the controller rather than by the bridge on that path.

Otherwise a second start on a running bridge is refused with RuntimeError at web_ui/backend/bridge.py:1636-1637, and the run primitives are reset at web_ui/backend/bridge.py:1639-1643.

The session is either the loaded one or a fresh Session() at web_ui/backend/bridge.py:1645-1651, tagged metadata source web_ui at web_ui/backend/bridge.py:1651; mode is copied from the session config at web_ui/backend/bridge.py:1653-1655 and workspace_id is applied at web_ui/backend/bridge.py:1657-1659.

The Agent is constructed at web_ui/backend/bridge.py:1662 as Agent(agent_config, session=session), and the bridge adopts the session id at web_ui/backend/bridge.py:1663.

There is a second, independent path into AgentConfig: _build_global_agent_config at web_ui/backend/bridge.py:1516-1537 builds it from the global config file through create_agent_config_service, service.get_all() and AgentConfig(**raw_config) at web_ui/backend/bridge.py:1529-1531, and deliberately raises RuntimeError instead of falling back to defaults at web_ui/backend/bridge.py:1532-1537 so that config corruption reaches load_session instead of producing an unusable session.

Agent construction, and every object that exists once the constructor returns, is Section 3; this section stops at the invocation above (web_ui/backend/bridge.py:1662).

### 2.8 Where the first prompt enters

The prompt is not passed to the Agent directly. It is enqueued at web_ui/backend/bridge.py:1666, and a daemon thread named web-bridge-agent is started at web_ui/backend/bridge.py:1669-1674 to consume that queue; the thread target is the bridge run loop, and the turn itself begins at agent/core/agent.py:885 in process_query.

That is the end of boot as this section defines it: from the process entry at thoughtmachine_entry.py:56 to the constructor at web_ui/backend/bridge.py:1662 is a single ordered chain with no step outside the files cited above, and the queue handover at web_ui/backend/bridge.py:1666-1674 is the last point at which boot and execution can be separated.

### 2.9 What is not on the boot path

agent/cli/main.py is a standalone argparse CLI that is 39 lines long: it declares its parser under the program name thoughtmachine, registers a single subcommand through add_index_codebase_subparser at agent/cli/main.py:27, and dispatches through args.func(args) at agent/cli/main.py:36. It never constructs an Agent, and agent/cli/__init__.py is empty, so agent/cli/ is not a stage between the process entry and the bootstrap described in Section 2.4.

agent/startup_health_check.py defines a pre-flight suite at agent/startup_health_check.py:275 whose checks are registered at agent/startup_health_check.py:292-299 and cover config paths, config validity, API key presence, directories, python version and imports; results are logged at agent/startup_health_check.py:316-325, and exit_on_fail turns a failure into sys.exit(1) at agent/startup_health_check.py:327-329, with a module CLI at agent/startup_health_check.py:334-339.

That suite is never reached from the server: run_health_check has no importer outside the module itself and the tests, and the lifespan at web_ui/backend/server.py:731-800 does not call it, so the health check runs only when something imports it or when python -m agent.startup_health_check is run deliberately.

Both files are therefore a gap between intent and wiring rather than a step in boot, and this section records the wiring that exists: the CLI is reachable only by invoking it, and the health check is available but unwired.

## 3. Agent construction and the object graph

Section 2 stopped at the invocation `Agent(agent_config, session=session)` (web_ui/backend/bridge.py:1662). This section opens that constructor and describes the object graph which exists once it returns, because every later section assumes that graph.

### 3.1 Three call sites, and they do not pass the same arguments

The class is declared at agent/core/agent.py:96 and the constructor body spans agent/core/agent.py:101-198.

The controller builds an agent from a run config, passing the configuration positionally and the session by keyword, behind a guard that permits None (agent/controller/__init__.py:594).

The same shape is used when a pending configuration is applied, after a fresh stop_check has been attached to it (agent/controller/__init__.py:635-637).

The worker thread is the only site that supplies a third argument: configuration and session by keyword plus event_bus (tools/workspace/worker_thread.py:2534-2538).

Because event_bus is passed by keyword at tools/workspace/worker_thread.py:2537, the constructor accepts it; neither controller site passes one, so a controller-built agent and a worker-built agent are not interchangeable objects even before a query runs (Section 11).

Downstream code depends on a side effect of construction rather than on an argument: the controller copies agent.session_id outward so that emitted events carry a session identity (agent/controller/__init__.py:597-598), which is why the constructor binds a session identity as early as agent/core/agent.py:137.

### 3.2 The order inside the constructor

Order is load bearing here, because several fields are captured by value rather than looked up later, so the body is stated as a sequence rather than as a list of attributes.

Configuration is stored first (agent/core/agent.py:110), and only then is a logger built - and only when logging is enabled in the configuration (agent/core/agent.py:114-121).

The logger is then registered with the unified logging registry (agent/core/agent.py:122-127) and installed as the security logger (agent/core/agent.py:128-131), with self.security_available set True on the success path (agent/core/agent.py:130) and False in the failure branch (agent/core/agent.py:135).

The session is bound next (agent/core/agent.py:136-142): the session identity at agent/core/agent.py:137, and _conversation assigned the session's own history list at agent/core/agent.py:138, which means the agent does not own a message store but aliases one (Section 5).

The number of prior turns is derived from that history for display purposes (agent/core/agent.py:143-149).

The collaborators follow: TokenCounter(config) at agent/core/agent.py:151, credential placeholder resolution at agent/core/agent.py:152, LLMClient(config, session, self.logger) at agent/core/agent.py:153, ConversationManager(session, None, self.logger) at agent/core/agent.py:154, DebugContext(self.logger) at agent/core/agent.py:155, and the pending warning buffers at agent/core/agent.py:156-157.

The tool surface is built from configuration rather than from a global registry: the class list is config.get_filtered_tool_classes() (agent/core/agent.py:158), MCP tools are registered on a background thread (agent/core/agent.py:161-165), model-facing definitions are derived from those classes at agent/core/agent.py:166, and the executor is constructed with the class list, the config, a None third argument, the logger, the security flag and a back-reference to the agent (agent/core/agent.py:167).

The provider is then lifted off the client (agent/core/agent.py:168), the runtime parameters are built (agent/core/agent.py:169-170), and token counts are taken from the session when it carries them and from zeros otherwise (agent/core/agent.py:172-175).

A final block wires the prompt and the context machinery: ensure_system_prompt (agent/core/agent.py:176), create_context_builder (agent/core/agent.py:177), the builder handed to the conversation manager (agent/core/agent.py:179-180), the stop_check callback (agent/core/agent.py:181), the next-query queue with its pause flags and rate-limit fields (agent/core/agent.py:182-191), the agent state (agent/core/agent.py:192) which is installed on the tool executor at agent/core/agent.py:193, session-state initialisation (agent/core/agent.py:194), the conversation token estimate (agent/core/agent.py:195) and the pending configuration fields (agent/core/agent.py:196-198).

### 3.3 The graph that exists when the constructor returns

config is the object the caller passed; nothing in the constructor reloads it (agent/core/agent.py:110), so a configuration change after construction is invisible to that agent - which is why the controller rebuilds an agent rather than mutating one when a configuration change is applied (agent/controller/__init__.py:632-637).

The logger may be None when logging is disabled (agent/core/agent.py:114-121), so consumers must tolerate a None logger rather than assume one.

security_available is a boolean snapshot taken at construction (agent/core/agent.py:130, agent/core/agent.py:135) and handed to the executor by value at agent/core/agent.py:167, so a later change in security availability does not reach an already built executor.

_conversation aliases session.user_history (agent/core/agent.py:138), while history writes normally go through the conversation manager (agent/core/conversation_manager.py:29-59); the two views agree only because both manager branches ultimately touch the same list (agent/core/conversation_manager.py:49, agent/core/conversation_manager.py:56).

tool_executor holds a reference back to the agent (agent/core/agent.py:167), so the graph contains a cycle, and tool_definitions (agent/core/agent.py:166) is not the same collection as tool_classes (agent/core/agent.py:158): one is what the model is shown, the other is what the executor instantiates (Section 9).

Wiring is completed in two phases rather than as one tree: the conversation manager is handed None instead of a context builder at agent/core/agent.py:154 and receives the real builder at agent/core/agent.py:179-180, so a history write occurring between those two lines would take the direct append path (agent/core/conversation_manager.py:54-57) rather than the builder path (agent/core/conversation_manager.py:51-53).

### 3.4 What the constructor deliberately does not do

It does not call the model, does not send a prompt and does not process a query; the first prompt enters through a different entry point than the one that builds the agent (Section 2.8 for the boot path, Section 6 for the turn loop).

It does not create the session: the session is an argument (agent/core/agent.py:136-142), so an agent always belongs to a caller-provided durable object and never manufactures one for itself (Section 13).

It does not necessarily finish registering tools: MCP registration is backgrounded (agent/core/agent.py:161-165) and a failure there is logged rather than raised, so the tool set immediately after construction is not final (Section 9).

It does not validate the configuration: validation is a separate entry point (agent/config/loader.py:454-472) that the constructor does not call, so an agent built from an invalid configuration fails later rather than at construction (Section 4).

## 4. Configuration resolution and precedence

### 4.1 The layers, weakest to strongest

Model defaults are the bottom layer: load_default_config constructs AgentConfig() and dumps it (agent/config/loader.py:278-286).

The factory file is next: load_factory_config reads the path held in the module constants (agent/config/loader.py:20-22), caches the result in a module-global so the file is parsed once per process (agent/config/loader.py:96-110), and falls back to the model defaults when the file is missing (agent/config/loader.py:100-104) or unreadable (agent/config/loader.py:111-115).

The saved configuration file is overlaid on the factory dictionary by a deep merge that preserves nested dictionaries (agent/config/loader.py:326-327, helper at agent/config/loader.py:118).

Nulls in the overlay are then backfilled from the model defaults (agent/config/loader.py:329, helper at agent/config/loader.py:366), so a null in the file means "take the default" rather than "take the null".

Keys which are not fields of AgentConfig are logged and dropped so that strict construction cannot raise (agent/config/loader.py:330-335, helper at agent/config/loader.py:351), with agent_soft_budget_seconds explicitly carried across the drop because it is consumed by a before-validator instead of being a model field (agent/config/loader.py:332-337).

Above the file sits runtime mutation: the controller overwrites a missing api_key from OPENAI_API_KEY, DEEPSEEK_API_KEY and the other environment names it tries, in that order (agent/controller/__init__.py:588-592), so an environment variable can outrank whatever the file said.

Above that sits the in-process service state described in 4.4, which is what a live agent is actually built from (4.5).

### 4.2 Failure policy: degrade, never raise

load_config does not propagate file errors: invalid JSON returns the factory dictionary with a warning (agent/config/loader.py:339-342), and any other exception returns the factory dictionary as well (agent/config/loader.py:343-345).

The consequence is that a corrupt or unreadable user file produces a WARNING line and a silently different configuration, so a reader debugging behaviour must check which branch was taken rather than assume the file was honoured.

validate_config is the explicit validation entry point and returns either an AgentConfig or None, logging at ERROR on failure (agent/config/loader.py:454-472); it also strips a stop_check value which was persisted as a string, because a live callback must never be resurrected from JSON (agent/config/loader.py:464-468).

Because it returns None instead of raising, a caller which ignores the return value gets no validation at all - the failure is entirely in the return value.

### 4.3 Where the files are

get_config_paths returns a project-level agent_config.json computed from the package location (agent/config/loader.py:483) and a user-level file under the vault directory (agent/config/loader.py:484), and the vault directory constant sits at agent/config/loader.py:15.

### 4.4 The service layer

ConfigService is the mutable, file-backed view (agent/config/service.py:12): its constructor merges defaults and then calls load() (agent/config/service.py:23-38).

load() starts from a copy of the defaults and updates it with the file contents (agent/config/service.py:40-66, update at agent/config/service.py:52-53), so the same degradation rule applies - a missing file leaves the defaults in place.

Writes go through an atomic temporary-file-then-rename path (agent/config/service.py:68-86, replacement at agent/config/service.py:101-106), so a crash mid-write cannot truncate the live file.

Access is get, set, update, get_all and add_listener (agent/config/service.py:123, agent/config/service.py:137, agent/config/service.py:157, agent/config/service.py:183-191, agent/config/service.py:193).

validate() returns True unconditionally (agent/config/service.py:225-236): it performs no validation whatsoever, so any caller that treats its True as a green light receives one regardless of the file's contents.

### 4.5 Which path actually serves the boot

The live agent's configuration does not come from load_config; it comes from _build_global_agent_config in the bridge (web_ui/backend/bridge.py:1516-1537), which calls create_agent_config_service() (web_ui/backend/bridge.py:1529), reads get_all() (web_ui/backend/bridge.py:1530) and constructs AgentConfig(**raw_config) from that dictionary (web_ui/backend/bridge.py:1531), raising a deliberate RuntimeError when the pieces are missing (web_ui/backend/bridge.py:1532-1537).

That service is created with a default file under the vault root (agent/config/service.py:279-280) and with defaults built from AgentConfig().model_dump(exclude={'api_key'}, exclude_none=True) (agent/config/service.py:282-284).

Excluding api_key from the service's own defaults means the service baseline carries no credential at all, so a credential can only arrive from the file, from a set() call, or from the controller's environment fallback (agent/controller/__init__.py:588-592).

The loader API and the service API compute their paths independently (agent/config/loader.py:483-484 versus agent/config/service.py:279-280), so they are two access paths to a configuration rather than two views of one resolved object; the boot path uses the service, and the loader functions are used by callers which resolve paths themselves.

### 4.6 The migration surface

The loader also carries the migration and serialisation machinery: migrate_config (agent/config/loader.py:436), a legacy system-prompt migration (agent/config/loader.py:169), legacy field mapping (agent/config/loader.py:255), a diff helper (agent/config/loader.py:141), a sanitiser for serialisation (agent/config/loader.py:230), save_config (agent/config/loader.py:382) and update_config (agent/config/loader.py:492).

These are named here for orientation only; their internal behaviour belongs to whichever section later depends on it.

## 5. Messages and conversation history

### 5.1 What a message is

A message is a dict subclass rather than a dataclass (agent/core/message.py:19), which is what allows it to be passed anywhere a plain message dictionary is expected and to be JSON-serialised without conversion (agent/core/message.py:8-9).

The system-notification flag is a regular dict key which must be set explicitly at the injection site: there is no content-based derivation, so a user who types the notification prefix is not treated as a notification (agent/core/message.py:4-6).

The prefix constant exists for those injection sites (agent/core/message.py:16) and is not a parser.

The class adds only a readable repr, a copy which preserves the type, a from_dict constructor and a to_dict that deliberately returns a plain dict (agent/core/message.py:25-27, agent/core/message.py:29-30, agent/core/message.py:33-35, agent/core/message.py:37-39).

### 5.2 Where history lives

History is the session's list, and the agent holds a reference to it rather than a copy (agent/core/agent.py:138).

That single decision explains several later behaviours: history persists because the session persists (Section 13), the agent's view cannot drift from the session's view, and any code that reassigns the list instead of appending to it would silently detach the agent.

### 5.3 How a message is appended

ConversationManager.add_message takes a message and the current conversation and returns the updated list (agent/core/conversation_manager.py:29-59).

It stamps created_at on the message if the message does not already carry one (agent/core/conversation_manager.py:45-46), which is where the timestamp on a stored message comes from rather than from the caller.

With no session it appends to the conversation it was given and returns it (agent/core/conversation_manager.py:47-50).

With a session it delegates to the context builder when that builder exposes add_message (agent/core/conversation_manager.py:51-53) and otherwise appends to session.user_history directly, also touching the session's updated_at (agent/core/conversation_manager.py:54-57).

The delegation is duck-typed with hasattr rather than checked through an interface (agent/core/conversation_manager.py:51), so a builder that renames its method silently falls back to the direct path.

### 5.4 Grouping messages into turns

Turns are the unit that pruning, context assembly and compaction all operate on, so the grouping rule is part of the contract rather than an incidental helper.

group_messages_into_turns is documented as the single source of truth for that rule, shared by the agent's summary pruning and by context assembly (agent/core/message_utils.py:4-5).

Its rules: system messages are skipped by the grouper and must be filtered before or after (agent/core/message_utils.py:38-39); a user message always starts a turn (agent/core/message_utils.py:42-52); a user message flagged as a system notification is appended to the current turn instead of starting one, and an orphan notification is dropped (agent/core/message_utils.py:43-49); an assistant message carrying tool_calls may also start a turn, which is what preserves tool sequences after a prune cut away their user message (agent/core/message_utils.py:53-56); a tool result is attached when any message in the current turn is an assistant with tool_calls, so multi-tool-call turns keep all their results (agent/core/message_utils.py:59-69); and messages with no current turn are discarded (agent/core/message_utils.py:72-74).

The final filter keeps only turns whose first message is a user message or an assistant message with tool_calls (agent/core/message_utils.py:80-91).

A second entry point returns the same grouping together with the original index of each turn's first message, because the summary insertion point has to be mapped back into a history which still contains system messages (agent/core/message_utils.py:94-102).

### 5.5 A second copy of the grouping rule

ConversationManager carries its own group_messages_into_turns (agent/core/conversation_manager.py:61-117) in addition to the shared helper, and the two are not identical.

The manager's version has no branch for is_system_notification, so a notification message starts a new turn there (agent/core/conversation_manager.py:82-85), whereas the shared helper appends it to the current turn (agent/core/message_utils.py:43-49).

The manager's version also differs on an assistant message with tool_calls: it appends that message to a current turn which started with a user message, and only starts a new turn otherwise (agent/core/conversation_manager.py:86-94), where the shared helper always starts a new turn (agent/core/message_utils.py:53-56).

Two implementations of the same rule with different answers for two message shapes is an invariant hazard: any code which groups history through the manager will disagree with code which groups it through the helper for notification-carrying and assistant-with-tool-calls turns, and only one of the two can match what the model actually saw.

### 5.6 Where trimming is not

The conversation manager exposes only three methods - construction, add_message and group_messages_into_turns (agent/core/conversation_manager.py:16, agent/core/conversation_manager.py:29, agent/core/conversation_manager.py:61) - so trimming, pruning and summarisation are not its responsibilities and live in the callers that use the shared grouping rule (Section 12).

Because a message carries its own created_at (agent/core/conversation_manager.py:45-46), trimming decisions can be taken from the message itself rather than from a parallel index, and because to_dict returns a plain dict (agent/core/message.py:37-39) a stored message can be rehydrated through from_dict (agent/core/message.py:33-35) without the dict subclass leaking into serialised sessions.

## 6. The turn loop: from query to committed turn

`process_query` is the whole engine of a single user query. It is one plain generator function, declared as `def process_query(self, query)` at `agent/core/agent.py:885` and documented at `agent/core/agent.py:886-887` as "Process a user query, appending it to conversation and running the agent. Yields events as dicts." Its body runs to `agent/core/agent.py:1467`; the next definition in the file is `_apply_summary_pruning` at `agent/core/agent.py:1468`.

Nothing in this section depends on a cooperative async runtime: the function contains only `yield`, never `await`, and the model call it makes is synchronous (`agent/core/agent.py:1121`). Everything the caller sees leaves the generator as a dictionary.

### 6.1 The entry contract

Preconditions the caller must already have satisfied: an `AgentState` exists on the instance (constructed at `agent/core/agent.py:192` and installed on the tool executor at `agent/core/agent.py:193`), a conversation list exists, and an LLM client plus tool definitions are configured. `process_query` takes exactly one argument, the raw query string, and returns a generator; no work happens until the caller starts iterating it.

Side effects that occur before the first yield, all of them already durable when the caller receives that first event:

1. `self._emergency_retries = 0` (`agent/core/agent.py:888`): the per-query emergency allowance is reset, so retry budget never leaks across queries.
2. The user message `{'role': 'user', 'content': query}` is appended first, through `self._add_to_conversation` (`agent/core/agent.py:892-894`). The ordering is deliberate: the query is in history before any config application, validation or model call can fail.
3. The estimated cost of that message is added to `state.current_conversation_tokens` (`agent/core/agent.py:895-896`).
4. The display turn counter is incremented (`agent/core/agent.py:899`).
5. Pending config is applied via `self._apply_pending_config()` (`agent/core/agent.py:905`). If that fails, a user-visible `[SYSTEM NOTIFICATION]` message marked `is_system_notification=True` is appended (`agent/core/agent.py:918`), an error event with `error_type: 'invalid_config'` and `stop_reason: 'error'` is yielded (`agent/core/agent.py:921-929`) and the generator returns (`agent/core/agent.py:930`). Note the asymmetry: the user message stays in history, the turn still ends.
6. The system prompt is ensured (`llm_client.ensure_system_prompt` at `agent/core/agent.py:951`) and the pause flag is cleared (`agent/core/agent.py:953`).
7. Execution state is moved to RUNNING on two mutually exclusive branches that both yield: the READY case (`agent/core/agent.py:959-963`) and every other case (`agent/core/agent.py:964-968`).
8. Per-query timing and counters are reset: `state.time_start` is stamped at `agent/core/agent.py:977`, and previous conversation length plus last input/output token counts are captured at `agent/core/agent.py:973-975`.

The first user-facing event is then `{'type': 'user_query', 'content': query, 'turn': ...}` at `agent/core/agent.py:934-938`, whose timestamp defaults at `agent/core/agent.py:936-937`. The loop entry logging follows at `agent/core/agent.py:969-972`.

The generator yields 36 times in total over its whole body. The enumeration, in file order:

- `agent/core/agent.py:898` - `token_update`, built by `_create_token_update_event` (`agent/core/agent.py:868`).
- `agent/core/agent.py:929` - `error`, `error_type: 'invalid_config'`, `stop_reason: 'error'` (terminal).
- `agent/core/agent.py:938` - `user_query`, the query echo with its turn number.
- `agent/core/agent.py:963` - execution-state transition event, READY branch.
- `agent/core/agent.py:968` - the same transition, other branch.
- `agent/core/agent.py:996` - state events for the PAUSING transition reported by the stop check (`agent/core/agent.py:991-1005`).
- `agent/core/agent.py:1004` - `stopped` with `stop_reason: 'stopped'` (terminal).
- `agent/core/agent.py:1021` - `token_update`, after a time-warning message was injected.
- `agent/core/agent.py:1029` - time-state event such as `time_warning`.
- `agent/core/agent.py:1039` - inside the `for event in self.state.update_turn_state(turn)` loop (agent/core/agent.py:1031-1032), the `turn_warning` branch first turns the warning into a `[SYSTEM NOTIFICATION]` user-role message and appends it to the conversation (agent/core/agent.py:1034-1035), charges that message's estimated tokens to `state.current_conversation_tokens` (agent/core/agent.py:1036-1037), and yields a token-update dict from `_create_token_update_event` (agent/core/agent.py:868) before the same iteration yields the warning event itself at agent/core/agent.py:1042 - so one warning costs two yields: the budget update first, the warning after.
- `agent/core/agent.py:1042` - turn-state event such as `turn_warning`.
- `agent/core/agent.py:1046` - `token_update`, after the turn-state event.
- `agent/core/agent.py:1055` - `token_update`, after a token-warning message was injected.
- `agent/core/agent.py:1058` - token-state event.
- `agent/core/agent.py:1164` - `error` with `stop_reason: 'context_full'` (terminal).
- `agent/core/agent.py:1187` - `error` for an `LLMError` that is not the token-limit case (terminal).
- `agent/core/agent.py:1199` - `rate_limit_warning`.
- `agent/core/agent.py:1205` - `stop_reason` with `stop_reason: 'rate_limit'` (terminal).
- `agent/core/agent.py:1220` - `error` for a provider failure, `PROVIDER_ERROR` or the provider's own upper-cased type (terminal).
- `agent/core/agent.py:1233` - `error` with `UNEXPECTED_ERROR`, the catch-all (terminal).
- `agent/core/agent.py:1270` - state events for PAUSING on the pause path.
- `agent/core/agent.py:1275` - `paused` (terminal).
- `agent/core/agent.py:1297` - `token_update`, after the assistant message was committed.
- `agent/core/agent.py:1304` - turn event for the completed turn.
- `agent/core/agent.py:1322` - `tool_call_start`, once per requested call, from the loop at `agent/core/agent.py:1312-1322`.
- `agent/core/agent.py:1341` - `tool_call`.
- `agent/core/agent.py:1345` - `tool_result`.
- `agent/core/agent.py:1353` - a buffered `token_warning`, one per entry of `_pending_warning_events`.
- `agent/core/agent.py:1376` - `agent_responded` on the Respond-tool path, carrying the response type, status, confidence and meta the tool supplied (terminal).
- `agent/core/agent.py:1386` - `token_update`, after summary pruning.
- `agent/core/agent.py:1394` - `context_summarized`.
- `agent/core/agent.py:1411` - state events for PAUSING at the post-turn check.
- `agent/core/agent.py:1416` - `paused` (terminal).
- `agent/core/agent.py:1432` - a buffered `token_warning` flushed on the no-tool-call path.
- `agent/core/agent.py:1445` - `agent_responded` with `response_type: 'answer'`, the normal completion (terminal).
- `agent/core/agent.py:1463` - the max-turns event (terminal).

Exactly eight of the thirty-six yields are immediately followed by `return` (agent/core/agent.py:929, agent/core/agent.py:1004, agent/core/agent.py:1187, agent/core/agent.py:1205, agent/core/agent.py:1220, agent/core/agent.py:1233, agent/core/agent.py:1445, agent/core/agent.py:1463), so those eight ends are the only ones a consumer can rely on as terminal. The remaining twenty-eight yields are provisional: the next statement is not `return`, the generator keeps going, and a consumer that treats one of them as final cuts the turn short.

Adjacency is the definition; event type is the practical test, and the two agree everywhere except at four sites. `agent/core/agent.py:1164`, `:1275`, `:1376` and `:1416` are followed by `return` a few statements later (`agent/core/agent.py:1169`, `:1280`, `:1381`, `:1421`), so a consumer that pattern-matches on the event TYPE rather than on ADJACENCY treats those four as terminal and is right in practice: the ambiguity is in the definition, not in the behaviour.

The caller's obligations follow directly: iterate to exhaustion, because stopping early leaves the query half-run with the user message already in history (`agent/core/agent.py:892-894`); treat `agent_responded`, `stopped`, `paused`, `error`, `stop_reason` and the max-turns event as terminal markers, since each is followed by a return; forward state, token and turn events without reinterpreting them; and never infer that history grew from a `token_update` event, because assistant content lands in history only through the commit at `agent/core/agent.py:1291-1297` or `agent/core/agent.py:1424-1425`.

### 6.2 Navigation map

`process_query` owns sequencing only; every mechanism it drives lives in a named collaborator, so the loop body reads as a call graph:

- `_add_to_conversation` (`agent/core/agent.py:748`) appends messages; `_estimate_tokens` (`agent/core/agent.py:776`), `_update_conversation_token_estimate` (`agent/core/agent.py:719`) and `_update_tokens_after_tool` (`agent/core/agent.py:780`) maintain the running size.
- `_create_token_update_event` (`agent/core/agent.py:868`), fed by `_get_conversation_data_for_event` (`agent/core/agent.py:837`) and `_add_conversation_data_to_event` (`agent/core/agent.py:857`), builds every `token_update` event and attaches the conversation snapshot other events carry.
- `_apply_pending_config` (`agent/core/agent.py:214`) is the only config entry point during a query (`agent/core/agent.py:905`).
- `_handle_state_event` (`agent/core/agent.py:674`) converts `AgentState` transitions into events; the state object itself is created once at `agent/core/agent.py:192` and initialized through `_initialize_session_state` (`agent/core/agent.py:662`).
- The three warning engines called from the top of each turn are `state.update_time_state` (`agent/core/state.py:173`), `state.update_turn_state` (`agent/core/state.py:260`) and `state.update_token_state` (`agent/core/state.py:84`).
- `self.context_builder.build` (`agent/core/agent.py:1067-1070`) produces the wire-format message list, `ContextBuilder._cleanup_orphaned_tool_messages` (`agent/core/agent.py:1078`) repairs orphaned tool messages before the call, and `token_counter.estimate_tokens` (`agent/core/agent.py:1087`) is used for logging only.
- `self.llm_client.format_tools` (`agent/core/agent.py:1097`) and `self.llm_client.chat_completion` (`agent/core/agent.py:1121`) are the model boundary.
- The tool-execution call at `agent/core/agent.py:1323` returns a five-tuple unpacked into the executed tools, the final/respond flag, the respond payload, an optional summary text and a keep-recent count; it runs tools, it does not commit them.
- `TurnTransaction` (`agent/core/turn_transaction.py:18`) is the commit unit for assistant and tool messages.
- `_apply_summary_pruning` (`agent/core/agent.py:1468-1547`), its fallback (`agent/core/agent.py:1548`) and its helpers `_find_summary_insertion_index` (`agent/core/agent.py:1578`) and `_group_messages_into_turns` (`agent/core/agent.py:1598`) rewrite history when compaction is requested.
- `request_pause` (`agent/core/agent.py:880`) and `reset_rate_limiting` (`agent/core/agent.py:874`) are the in-flight control knobs the loop polls; `reset` (`agent/core/agent.py:1608`), `submit_next_query` (`agent/core/agent.py:1614`), `request_reset` (`agent/core/agent.py:1618`) and `_wait_for_next_query` (`agent/core/agent.py:1622`) live outside the query path.

### 6.3 One turn, stage by stage

The loop header is `for turn in range(self.config.max_turns):` (`agent/core/agent.py:982`), with `turn_start_time` captured at `agent/core/agent.py:983` and a turn log line plus system-resource sample every five turns at `agent/core/agent.py:986-989`. The limit is read once, at loop entry, on purpose: the comment at `agent/core/agent.py:978-981` states that a config update picked up from the mailbox applies to the next query and never extends the range of an already running loop.

Stage 1, stop check (checkpoint 1), `agent/core/agent.py:991-1005`. A stop request is honoured before any state accounting: execution state goes to PAUSING (`agent/core/agent.py:993`), the resulting events are yielded (`agent/core/agent.py:996`), `{'type': 'stopped', 'stop_reason': 'stopped'}` is yielded (`agent/core/agent.py:1004`) and the generator returns (`agent/core/agent.py:1005`).

Stage 2, time budget, `agent/core/agent.py:1006-1029`. Elapsed time is measured from `state.time_start` and handed to `update_time_state` (`agent/core/state.py:173`); resulting events are yielded at `agent/core/agent.py:1029`, and any injected warning message publishes its token cost at `agent/core/agent.py:1021`. A CRITICAL time state does not stop the turn: the comment at `agent/core/agent.py:1022-1024` records the soft-restriction decision, so time can only annotate a turn, never end it.

Stage 3, turn and token warnings, `agent/core/agent.py:1031-1058`. `update_turn_state` (`agent/core/state.py:260`) reports at `agent/core/agent.py:1042`, a fresh count publishes at `agent/core/agent.py:1046`, then `update_token_state` (`agent/core/state.py:84`) reports at `agent/core/agent.py:1058`, with the warning-message token delta published at `agent/core/agent.py:1055`.

Stage 4, request assembly, `agent/core/agent.py:1059-1097`. `reasoning_content` is normalised to empty on assistant messages that carry tool calls (`agent/core/agent.py:1059-1062`) so the provider accepts the replay; the message list comes from `self.context_builder.build(self.conversation)`, or the raw conversation when no builder is configured (`agent/core/agent.py:1067-1070`); orphaned tool messages are cleaned at `agent/core/agent.py:1078`; the size estimate is logged at `agent/core/agent.py:1087`; the request is logged at `agent/core/agent.py:1090`; an optional rate-limit spacer sleeps for `min(rate_limit_delay, rate_limit_max_wait)` (`agent/core/agent.py:1091-1096`); tool definitions are formatted at `agent/core/agent.py:1097`.

Stage 5, the model call and its accounting, `agent/core/agent.py:1111-1155`. Sampling parameters are assembled at `agent/core/agent.py:1112-1114` and `self.llm_client.chat_completion(...)` is called at `agent/core/agent.py:1121`. The call is synchronous and returns the whole response, which is why nothing in this loop streams. Usage is read at `agent/core/agent.py:1126-1127`, the provider's prompt-token count overwrites the local estimate at `agent/core/agent.py:1129-1130` (the provider is the authority on conversation size), a drift above five percent is logged at `agent/core/agent.py:1133-1147`, and cumulative totals are updated at `agent/core/agent.py:1152-1155`.

Stage 6, failure branches before any commit, `agent/core/agent.py:1156-1234`. `LLMError` at `agent/core/agent.py:1156` splits on the token-limit flag: the limit case enters emergency mode, incrementing `_emergency_retries` at `agent/core/agent.py:1171` against a cap of 2 at `agent/core/agent.py:1158`, and either yields `context_full` at `agent/core/agent.py:1164` and returns at `agent/core/agent.py:1169`, or continues to retry the whole turn at `agent/core/agent.py:1175`; a non-limit `LLMError` yields an error at `agent/core/agent.py:1187` and returns. `RateLimitExceeded` at `agent/core/agent.py:1189-1206` counts the event (`agent/core/agent.py:1190`), computes a backoff (`agent/core/agent.py:1193`), warns at `agent/core/agent.py:1197-1199`, sleeps at `agent/core/agent.py:1200`, appends a system notification at `agent/core/agent.py:1202`, yields `stop_reason: 'rate_limit'` at `agent/core/agent.py:1205` and returns; a rate limit is not retried inside the loop. `ProviderError` and remaining `LLMError` cases at `agent/core/agent.py:1207-1221` and the catch-all at `agent/core/agent.py:1222-1234` yield errors at `agent/core/agent.py:1220` and `agent/core/agent.py:1233` and return. No assistant message is ever committed for a failed call, because all of these sit before Stage 8.

Stage 7, pause before commit, `agent/core/agent.py:1247-1280`. If a pause was requested and the response contains tool calls, the pause is deferred (`agent/core/agent.py:1248-1252`); otherwise the assistant message is committed as a grace commit (`agent/core/agent.py:1257-1263`), PAUSING events are yielded (`agent/core/agent.py:1268-1270`), `{'type': 'paused'}` is yielded (`agent/core/agent.py:1275`) and the generator returns (`agent/core/agent.py:1280`).

Stage 8, commit the assistant message, `agent/core/agent.py:1291-1297`. The comment at `agent/core/agent.py:1291-1293` states the rule: the assistant message goes into `user_history` before anything about it is yielded, so a consumer that dies mid-turn cannot lose a message the model already produced. The guard is `if turn_transaction and turn_transaction.has_assistant_message():` (`agent/core/agent.py:1294`). The token count is republished at `agent/core/agent.py:1297`, then the turn event at `agent/core/agent.py:1304`.

Stage 9, tools, `agent/core/agent.py:1305-1355`. `tool_call_start` is yielded per requested call before execution begins (`agent/core/agent.py:1312-1322`). The execution call itself is `agent/core/agent.py:1323`. Each result is classified at `agent/core/agent.py:1324-1333`; a result counts as unsuccessful when the returned string starts with a cross mark, contains `TOOL CALL REJECTED`, or contains `Error`. `turn_transaction.commit()` at `agent/core/agent.py:1336-1337` then publishes the whole exchange atomically, and only afterwards are `tool_call` (`agent/core/agent.py:1341`) and `tool_result` (`agent/core/agent.py:1345`) yielded. Buffered warnings are appended to the conversation and flushed as events at `agent/core/agent.py:1348-1355`, and both buffers are cleared there.

Stage 10, three exits from the tool phase, `agent/core/agent.py:1357-1421`. First, a final answer from the Respond tool: `agent_responded` at `agent/core/agent.py:1376` carries the response type, content, status, confidence, meta, turn, context length and usage, is logged, and is followed by return at `agent/core/agent.py:1381`. Second, a compaction request: `_apply_summary_pruning` at `agent/core/agent.py:1384`, a token refresh at `agent/core/agent.py:1386`, a single `context_summarized` event at `agent/core/agent.py:1394`, and `continue` at `agent/core/agent.py:1402`; the comment at `agent/core/agent.py:1387-1392` records that the older duplicate `token_recovery` and `context_cleared` emits were deliberately dropped so the caller sees exactly one compaction event. Third, pause checkpoint 3: PAUSING events at `agent/core/agent.py:1411`, `paused` at `agent/core/agent.py:1416`, return at `agent/core/agent.py:1421`.

Stage 11, the no-tool-call turn, `agent/core/agent.py:1422-1446`. With nothing to execute the turn is committed at `agent/core/agent.py:1424-1425`, buffered warnings are flushed at `agent/core/agent.py:1427-1434`, the run is logged as completed at `agent/core/agent.py:1435-1438`, and the final event `{'type': 'agent_responded', 'response_type': 'answer'}` is built at `agent/core/agent.py:1439`, yielded at `agent/core/agent.py:1445`, with return at `agent/core/agent.py:1446`.

Stage 12, budget exhausted, `agent/core/agent.py:1449-1463`. When `range(self.config.max_turns)` (`agent/core/agent.py:982`) is exhausted, the max-turns event is built at `agent/core/agent.py:1449-1462`, yielded at `agent/core/agent.py:1463`, and the generator returns at `agent/core/agent.py:1464`.

The whole body sits inside the `try:` opened at `agent/core/agent.py:889`, whose `finally:` is at `agent/core/agent.py:1465`; the content of that `finally` is outside the region read for this section, so nothing is claimed about it here.

As a sequence, with the caller on the left:

```
  caller                            process_query  (agent/core/agent.py:885)
    |                                    |
    |--- next() -----------------------> | user message appended        (:892-894)
    |<------------------ token_update -- |                              (:898)
    |--- next() -----------------------> | pending config applied       (:905)
    |<------------------ user_query ---- |                              (:938)
    |<------------------ state --------- | execution_state -> RUNNING   (:959-968)
    |--- next() -----------------------> | for turn in range(max_turns) (:982)
    |<------------------ state --------- | stop check -> PAUSING+stopped (:993-1004) [exit]
    |<------------------ state --------- | time / turn / token states   (:1029-1058)
    |--- next() -----------------------> | chat_completion (blocking)   (:1121)
    |<------------------ error --------- | LLM or rate-limit failure    (:1164-1233) [exit]
    |<------------------ state --------- | pause before commit          (:1270-1275) [exit]
    |<------------------ token_update -- | assistant message committed  (:1291-1297)
    |<------------------ turn ---------- |                              (:1304)
    |<------------------ tool events --- | tool_call_start, call, result (:1322-1345)
    |<------------------ summary ------- | context_summarized           (:1394)      [continue]
    |<------------------ state --------- | pause after turn             (:1411-1416) [exit]
    |<------------------ responded ----- | Respond tool or plain answer (:1376, :1445) [exit]
    |<------------------ max_turns ----- | loop exhausted               (:1463)      [exit]
```

`[exit]` marks a yield immediately followed by a return, and `[continue]` marks the single non-terminal wrap-around.

### 6.4 Turn state

All per-turn bookkeeping lives on one dataclass, `AgentState` (`agent/core/state.py:41`), created once for the agent at `agent/core/agent.py:192` and handed to the tool executor at `agent/core/agent.py:193`. Its shape is fixed at construction: `config` (`agent/core/state.py:43`), `logger` (`agent/core/state.py:44`), `security_config` (`agent/core/state.py:45`), the sub-states `token_state` (`agent/core/state.py:46`), `turn_state` (`agent/core/state.py:47`) and `time_state` (`agent/core/state.py:48`), `execution_state` (`agent/core/state.py:49`, default READY), `session_state` (`agent/core/state.py:50`), the scalars `current_conversation_tokens` (`agent/core/state.py:51`), `current_turn` (`agent/core/state.py:52`), `restrictions_pending` (`agent/core/state.py:53`) and `restrictions_active` (`agent/core/state.py:54`), the warning bookkeeping fields at `agent/core/state.py:55-64`, `timeout_seconds` (`agent/core/state.py:65`, defaulting to `SOFT_BUDGET_FALLBACK_SECONDS`), `time_warning_threshold` (`agent/core/state.py:66`, default 240), `time_start` (`agent/core/state.py:67`), `restriction_reason` (`agent/core/state.py:68`) and the pending-event queue `_pending_events` (`agent/core/state.py:69`). The sub-state enums are `TokenState` (`agent/core/state.py:10`), `TurnState` (`agent/core/state.py:16`), `ExecutionState` (`agent/core/state.py:22`) and `TimeState` (`agent/core/state.py:28`).

What one query mutates, in order: `current_conversation_tokens` three times over, by the estimate of the new user message (`agent/core/agent.py:895-896`), by the provider's own count (`agent/core/agent.py:1129-1130`) and by `update_token_state` (`agent/core/state.py:89`); `execution_state` to RUNNING (`agent/core/agent.py:959-968`) and later to PAUSING on each of the three pause paths (`agent/core/agent.py:993`, `agent/core/agent.py:1268-1270`, `agent/core/agent.py:1411`); `time_start` (`agent/core/agent.py:977`); and whatever the warning engines decide inside `update_time_state` (`agent/core/state.py:173`), `update_turn_state` (`agent/core/state.py:260`, called once per iteration at `agent/core/agent.py:1031`) and `update_token_state` (`agent/core/state.py:84`).

What resets it: thresholds are re-derived from config on every `update_token_state` call (`agent/core/state.py:92-97`), so a config change takes effect on the next turn; only upward transitions fire events, which is what stops the warning stream from repeating itself (`agent/core/state.py:103-123`); and the explicit setters are `AgentState.reset` (`agent/core/state.py:363-391`), `set_execution_state` (`agent/core/state.py:339-350`) and `set_session_state` (`agent/core/state.py:351-362`). Inside the turn loop the only resets are the pause flag (`agent/core/agent.py:953`) and the two warning buffers cleared after flushing (`agent/core/agent.py:1348-1355`, `agent/core/agent.py:1427-1434`). Nothing else in `AgentState` is zeroed between turns by the loop.

Two fields that look like budgets are not: `timeout_seconds` defaults to `SOFT_BUDGET_FALLBACK_SECONDS` (`agent/core/state.py:65`, defined as 300 at `thoughtmachine/timeout_constants.py:28`) and `time_warning_threshold` defaults to 240 (`agent/core/state.py:66`). Neither can end a turn, as 6.6 sets out. `SHIPPED_SOFT_BUDGET_SECONDS` (300) at `thoughtmachine/timeout_constants.py:30` and `IDLE_TIMEOUT_SECONDS` (600) at `thoughtmachine/timeout_constants.py:26` are consumed outside this loop.

### 6.5 Commit and abort: the transaction

A turn's durable output is the assistant message plus the tool-call messages plus the tool results. `TurnTransaction` (`agent/core/turn_transaction.py:18`) exists to publish exactly that set atomically; the intent is stated in the module docstring at `agent/core/turn_transaction.py:1-11`.

The object is a buffer, not an owner. `add_assistant_message` (`agent/core/turn_transaction.py:38-48`) rejects any role other than assistant with `ValueError` and stores a copy; `add_tool_call` (`agent/core/turn_transaction.py:50-59`) and `add_tool_result` (`agent/core/turn_transaction.py:61-70`) both require role `tool` and append copies. Nothing reaches history until a commit. The constructor wires it to a session, a context builder and a fallback conversation (`agent/core/turn_transaction.py:21-36`), and the bookkeeping flags are `_committed` and `_assistant_committed`.

Four behaviours matter here. `commit_assistant_only` (`agent/core/turn_transaction.py:72-100`) raises `RuntimeError` if the transaction is already committed (`agent/core/turn_transaction.py:84-85`) or has no assistant message (`agent/core/turn_transaction.py:86-87`), then extends `session.user_history` (`agent/core/turn_transaction.py:91`) and stamps `session.updated_at` (`agent/core/turn_transaction.py:92`), or extends the fallback conversation when there is no session (`agent/core/turn_transaction.py:95`), clears the context-builder cache when the builder supports `clear_cache` (`agent/core/turn_transaction.py:96-97`), and records that only the assistant message is in (`agent/core/turn_transaction.py:98`). `commit` (`agent/core/turn_transaction.py:102-144`) imports `ObservableList` locally (`agent/core/turn_transaction.py:113`), raises if already committed (`agent/core/turn_transaction.py:114-115`), appends only the tool buffer when the assistant message was committed earlier (`agent/core/turn_transaction.py:116-118`), otherwise writes the assistant message first (`agent/core/turn_transaction.py:122-123`) and then the tool buffer (`agent/core/turn_transaction.py:129`), extends `user_history` and `updated_at` (`agent/core/turn_transaction.py:135-136`) or the fallback (`agent/core/turn_transaction.py:137-139`), clears the cache (`agent/core/turn_transaction.py:140-141`) and marks itself committed (`agent/core/turn_transaction.py:142`). `rollback` (`agent/core/turn_transaction.py:146-152`) refuses if already committed (`agent/core/turn_transaction.py:148-149`) and then drops the buffered assistant message and clears the buffer (`agent/core/turn_transaction.py:150-151`); it never touches `session.user_history`, the token counters or `AgentState`, because there is nothing to undo there - rollback only discards work that was never published. The context-manager protocol `__enter__` and `__exit__` (`agent/core/turn_transaction.py:186-197`) commits when no exception escapes, rolls back when one does, and returns `False` so the exception propagates; `process_query` does not use it, since there is no `with` statement anywhere between `agent/core/agent.py:885` and `agent/core/agent.py:1467`.

How the loop actually uses it: `has_assistant_message()` (`agent/core/turn_transaction.py:171-173`) is tested at `agent/core/agent.py:1294` and the assistant message is published before anything about it is emitted (`agent/core/agent.py:1291-1297`); `commit()` runs before any `tool_call` or `tool_result` event at `agent/core/agent.py:1336-1337`, so a tool result can never be visible to the caller while the assistant message that ordered it is still buffered; and the no-tool-call turn is committed at `agent/core/agent.py:1424-1425`. Buffer inspection helpers `get_buffer` (`agent/core/turn_transaction.py:154-165`), `is_empty` (`agent/core/turn_transaction.py:167-169`) and `count_messages` (`agent/core/turn_transaction.py:175-184`) complete the surface.

Permission refusals and tool errors abort nothing. A refusal or a failure returns as a string result, is classified at `agent/core/agent.py:1324-1333` (unsuccessful when it starts with a cross mark, contains `TOOL CALL REJECTED`, or contains `Error`), is committed like any other result at `agent/core/agent.py:1336-1337`, and is reported at `agent/core/agent.py:1345`. The turn continues and the model sees the failure on the next iteration. That is the direct consequence of putting the gate chain's verdicts in-band, which is Section 8's subject: a refused tool call is a normal recorded exchange rather than an error path.

So the rollback domain is narrow, and worth stating both ways. Rolled back: the buffered assistant message and the buffered tool-call/result messages, and nothing else. Not rolled back: the user message, which was appended before the generator's first yield (`agent/core/agent.py:892-894`); the injected warning and notification entries (`agent/core/agent.py:918`, and the flush at `agent/core/agent.py:1348-1355`); the token counters and cumulative totals, already updated at `agent/core/agent.py:1152-1155` before a later branch can fail; and pruned history, since `_apply_summary_pruning` (`agent/core/agent.py:1384`) rewrites `session.user_history` directly and no rollback covers it. A failed turn therefore leaves the query, the warnings and the counters behind, and leaves only assistant and tool messages unpublished.

Two facts are stated as gaps rather than filled. The construction site of `turn_transaction` is not inside the region of the generator read for this section - the object is used at `agent/core/agent.py:1294`, `agent/core/agent.py:1336` and `agent/core/agent.py:1424`, and its construction belongs to the tool-execution side described in Section 8. And the body of the `finally` at `agent/core/agent.py:1465` was not read.

### 6.6 Termination

There is exactly one iteration limit and no wall-clock abort. Every way a query can end:

1. Plain answer. Check: the response carried no tool calls, so the branch at `agent/core/agent.py:1422` runs. Limit: none. The caller observes `agent_responded` with `response_type: 'answer'` (`agent/core/agent.py:1439`), yielded at `agent/core/agent.py:1445`, then `StopIteration` at `agent/core/agent.py:1446`.
2. Final answer from the Respond tool. Check: `final_detected`, returned by the call at `agent/core/agent.py:1323` and tested at `agent/core/agent.py:1357`. The caller observes `agent_responded` at `agent/core/agent.py:1376`, carrying the response type, status, confidence and meta the tool supplied, then `StopIteration` at `agent/core/agent.py:1381`.
3. Stop request. Check: `stop_check` at checkpoint 1 (`agent/core/agent.py:991`). The caller observes PAUSING events (`agent/core/agent.py:996`) and then `stopped` with `stop_reason: 'stopped'` (`agent/core/agent.py:1004`).
4. Pause requested before the tools run. Check: `agent/core/agent.py:1247`, with the deferral when tool calls are present (`agent/core/agent.py:1248-1252`). The caller observes `paused` (`agent/core/agent.py:1275`), after a grace commit when the branch allowed one (`agent/core/agent.py:1257-1263`).
5. Pause requested after the turn. Check: checkpoint 3 at `agent/core/agent.py:1403-1421`. The caller observes `paused` at `agent/core/agent.py:1416`, with the tool exchange already committed.
6. Turn budget exhausted. Check: `for turn in range(self.config.max_turns)` (`agent/core/agent.py:982`). The value is whatever `config.max_turns` holds; no numeric turn or step constant is defined in `thoughtmachine/timeout_constants.py:26-30`, and `agent/core/agent.py:978-981` fixes the semantics as a per-query limit captured at loop entry. The caller observes the max-turns event (`agent/core/agent.py:1449-1463`) and `StopIteration` at `agent/core/agent.py:1464`.
7. Context too large. Check: `LLMError` carrying the token-limit condition at `agent/core/agent.py:1156`, against an emergency allowance capped at 2 (`agent/core/agent.py:1158`), incremented at `agent/core/agent.py:1171` and reset per query at `agent/core/agent.py:888`. The caller observes `error` with `stop_reason: 'context_full'` at `agent/core/agent.py:1164`; while the allowance lasts the loop retries the turn instead (`agent/core/agent.py:1175`).
8. Rate limit, provider failure, or anything unanticipated. Checks: `RateLimitExceeded` at `agent/core/agent.py:1189`, `ProviderError` and remaining `LLMError` at `agent/core/agent.py:1207`, bare `Exception` at `agent/core/agent.py:1222`. The caller observes `rate_limit_warning` then `stop_reason: 'rate_limit'` (`agent/core/agent.py:1199`, `agent/core/agent.py:1205`), or an `error` event at `agent/core/agent.py:1220` or `agent/core/agent.py:1233`.

Five things deliberately do not terminate a turn, and each is worth naming because their absence is load-bearing. There is no wall-clock abort: the time state warns, and at CRITICAL only logs the soft-restriction decision (`agent/core/agent.py:1022-1024`). There is no per-iteration cap other than `config.max_turns` (`agent/core/agent.py:982`). There is no cancellation handler in the generator: no `except GeneratorExit`, no asyncio cancellation branch, so abandoning the generator mid-turn runs only the `finally` at `agent/core/agent.py:1465` and publishes nothing else. There is no rollback on abandonment, for the same reason that there is no cancellation branch. And tool failures and permission refusals do not end anything - they are classified at `agent/core/agent.py:1324-1333` and committed at `agent/core/agent.py:1336-1337`.

### 6.7 Forward pointers

Streaming and the shape of the provider call are Section 7's ground. Token accounting, drift detection and the compaction machinery behind `agent/core/agent.py:1384` belong to Section 12. The tool gate chain, and why a refusal arrives as an in-band string result rather than an exception, is Section 8.
## 7. Streaming, events and observers

A front end learns about a running turn through two channels that are not interchangeable: the plain-dict generator of Section 6, and a typed publish/subscribe bus defined in `agent/events.py`. They share a vocabulary, and they even share converters, but they carry different traffic - the bus never carries the model's answer, and the generator never carries an approval request - so a reader who wires one where the other belongs gets silence rather than an error.

### 7.1 The event vocabulary

`agent/events.py` is three layers deep: an `EventType` enum of string-valued kinds (`agent/events.py:23`), a pydantic `BaseEvent` envelope of `type` + `metadata` + `data` (`agent/events.py:114-118`), and one subclass per kind whose only addition is a `validate_data` guard (for example `ToolCallEvent` at `agent/events.py:175-198`).

The envelope itself is `EventMetadata`: `event_id`, `timestamp`, `source` (default `'unknown'`), `session_id`, `turn` (`agent/events.py:103-109`). `BaseEvent.to_dict()` flattens it to `{type, timestamp, event_id, source, **data}`, adding `session_id` and `turn` only when they are set (`agent/events.py:120-127`); that flattening is why a bus event and a yielded dict look alike on the wire and are still different objects in code.

The first block of the enum is turn-scoped: `AGENT_START`, `AGENT_END`, `LLM_REQUEST`, `LLM_RESPONSE`, `RAW_RESPONSE`, `TOOL_CALL`, `TOOL_CALL_START`, `TOOL_RESULT`, `CONVERSATION_UPDATE`, `CONVERSATION_PRUNE`, `EXECUTION_STATE_CHANGE`, `SESSION_STATE_CHANGE`, `TOKEN_WARNING`, `TURN_WARNING`, `TIME_WARNING`, `TOKEN_UPDATE`, `TURN_UPDATE`, `AGENT_RESPONDED`, `USER_INTERACTION_REQUESTED`, `USER_QUERY`, `FINAL_DETECTED`, `FINAL`, `STOP_SIGNAL`, `MAX_TURNS_REACHED`, `MAX_TURNS`, `PAUSED`, `STOPPED`, `THREAD_FINISHED`, `SESSION_STOP`, `ERROR` (`agent/events.py:25-54`).

A second block is security- and sandbox-scoped: `FILE_ACCESS`, `SECURITY_VIOLATION`, `DOCKER_SANDBOX`, `CAPABILITY_CHECK`, `SECURITY_PROMPT`, `SECURITY_RESPONSE` (`agent/events.py:55-60`).

A third block exists only for the worker bus adapter, as its own comment says (`agent/events.py:76`): spawned, status, completed, message, assistant message, error, running, stopping, heartbeat, partial result, timeout - one event class each at `agent/events.py:293`, `:303`, `:315`, `:325`, `:336`, `:347`, `:360`, `:371`, `:382`, `:395`, `:408`.

Delivery is `EventBus.publish`: a synchronous in-process loop over the per-type subscriber list and then the wildcard list, each callback wrapped in `try/except` that logs and continues (`agent/events.py:475-489`). The two directions of the dictionary bridge are `convert_to_legacy_format` (`agent/events.py:534`) and `convert_from_legacy_format` (`agent/events.py:551`); payload builders are `create_event` (`agent/events.py:420`), `create_tool_call_event` (`agent/events.py:436`), `create_tool_result_event` (`agent/events.py:440`) and `create_token_warning_event` (`agent/events.py:447`).

Emitters are few. The core publishes tool-level events from the tool executor (`agent/core/tool_executor.py:638-641`, with the bus injected at `agent/core/tool_executor.py:166-185`); the controller publishes a typed conversion of the dicts it forwards (`agent/controller/__init__.py:531-534`); the worker thread publishes worker status (`tools/workspace/worker_thread.py:2545-2553`). Front ends are otherwise subscribers: the web bridge subscribes to `SECURITY_PROMPT` (`web_ui/backend/bridge.py:621-670`) and to worker lifecycle events (`web_ui/backend/bridge.py:672-681`); the exception among the sites checked is the HTTP layer publishing an event of its own at `web_ui/backend/global_routes.py:361`.

Now the separation that matters. `EventBus` is not the turn stream: `global_event_bus = EventBus()` is one process-wide singleton (`agent/events.py:649`), while a turn is one `process_query` generator per agent (`agent/core/agent.py:885`), and the two meet only through the two converters above. Subscribing a turn consumer to `global_event_bus` therefore yields nothing, and a consumer that only iterates `process_query` never sees a typed event.

### 7.2 How a yield becomes an event

Follow one yield through. `Agent.process_query` yields a dict (`agent/core/agent.py:885-1467`); the consumer is the controller's run loop, `for event in local_agent.process_query(query)` (`agent/controller/__init__.py:705`), which reads `event.get('type')` for logging only (`agent/controller/__init__.py:706-707`). The web bridge does not iterate the generator itself - its own module comment states the indirection, that it runs the agent through the controller and receives dicts by callback (`web_ui/backend/bridge.py:11-12`, `:21`).

The dict is then fanned out to every registered callback (`agent/controller/__init__.py:523`), registration being `set_event_callback`, which appends to a list and refuses duplicates (`agent/controller/__init__.py:49-57`).

So the event a front end receives is the generator's own dict, unmodified, handed to N callbacks in the producing thread. The controller adds no renaming, no batching, no buffering at that point. The parallel delivery at `agent/controller/__init__.py:531-534` - the typed publish of the same occurrence - sits a few lines away and is a second channel, not a replacement for the dict fan-out.

### 7.3 The two consumers

The terminal presenter consumes the controller callbacks. `_handle_controller_event` (`agent/presenter/agent_presenter.py:60-62`) forwards each dict to `EventProcessor.process_event` (`agent/presenter/event_processor.py:32`), which re-types it with `ev.convert_from_legacy_format(event)` (`agent/presenter/event_processor.py:39`) and dispatches on `typed_event.type.value` (`agent/presenter/event_processor.py:40`). The presenter never touches `process_query`; it is five modules and 1805 lines (`agent/presenter/agent_presenter.py` 416, `agent/presenter/session_lifecycle.py` 563, `agent/presenter/state_bridge.py` 372, `agent/presenter/event_processor.py` 345, `agent/presenter/gui_integration.py` 94).

The web bridge consumes the same callbacks by the same mechanism. `WebAgentBridge` (`web_ui/backend/bridge.py:470`) either attaches an existing controller - `controller.set_event_callback(self._on_controller_event)` (`web_ui/backend/bridge.py:1505-1512`) - or builds one and registers the same callback on it (`web_ui/backend/bridge.py:1709-1716`), so the generator still has one consumer per session.

The divergence is vocabulary, not source. The bridge routes each dict through an `EventForwarder` (`web_ui/backend/bridge.py:495`) that maps agent events to frontend events (`web_ui/backend/bridge.py:24-35`), and it adds kinds the agent never produced - `status_message` is bridge and server vocabulary (`web_ui/backend/bridge.py:35`, sent at `web_ui/backend/server.py:1407`). The socket is written after per-connection registration (`bridge.set_event_callback(event_callback, key=id(ws))` at `web_ui/backend/server.py:1384`, again at `:1668`) with a plain `await ws.send_json(event)` (`web_ui/backend/server.py:1307`).

So the two consumers share an upstream and differ downstream. The terminal sees the controller's dicts under their original `type` values (`agent/presenter/event_processor.py:39-40`); the browser sees a renamed subset of those plus synthetic control messages. A front-end bug can therefore live entirely in the mapping layer while every core event is correct, which is what a two-presenter design buys and what it costs.

The third constructor argument decides where the bus traffic goes. `tools/workspace/worker_thread.py:2534-2538` builds `Agent(config=..., session=..., event_bus=self._event_bus)`; `Agent.__init__` accepts it (`agent/core/agent.py:101`) and does exactly one thing with it, handing it to the tool executor as `event_bus=event_bus or global_event_bus` (`agent/core/agent.py:167`), preserving it across a config swap (`agent/core/agent.py:529`, `:563`) and re-reading it for downstream construction (`agent/core/agent.py:415-420`). The worker's bus is per-worker, created lazily and exposed as a property (`tools/workspace/worker_thread.py:1109-1110`, `:1138-1141`). A controller-built agent passes no `event_bus` - `web_ui/backend/bridge.py:1662` is plain `Agent(agent_config, session=session)` - so its tool events land on the process-wide `global_event_bus` (`agent/events.py:649`), where any other session in the process can subscribe to them. The worker can isolate its bus traffic per worker; a controller-built agent cannot isolate it per session.

### 7.4 Slow consumers, queueing and loss

The event path from generator to socket has no queue, no coalescing and no drop policy. `EventBus.publish` calls subscribers inline in the caller's thread (`agent/events.py:475-489`), the controller calls its callbacks inline from its own loop (`agent/controller/__init__.py:523`, driven from `:705`), and the socket write is an `await` in the connection handler (`web_ui/backend/server.py:1307`). Nothing between them measures consumer latency, and nothing is bounded: `asyncio.run_coroutine_threadsafe` (`web_ui/backend/server.py:1335`) hands each event straight to the loop with no queue in between, so the only thing that can grow is the loop's own pile of unobserved scheduled sends.

The queues that do exist are off that path: `_query_queue` carries incoming queries into the agent thread (`web_ui/backend/bridge.py:489`), `_input_queue` and `_output_queue` carry worker IPC (`tools/workspace/worker_thread.py:1118-1119`), and `_worker_event_buffer` is a plain list capped at 100 (`web_ui/backend/bridge.py:500-502`) holding worker events published while no socket callback is attached, flushed on the next `set_event_callback` (`web_ui/backend/bridge.py:497-501`). That cap is the only bound on anything resembling the event path; the trim rule behind it was not read.

Consequences follow from the synchronous design rather than from policy. A consumer that stops reading stalls whoever is delivering to it, because the producer is the one calling it: a wedged renderer delays the turn loop instead of losing events. The socket case splits in two. The agent thread does not block - `event_callback` (`web_ui/backend/server.py:1319`) schedules `send_event(event)` with `asyncio.run_coroutine_threadsafe` (`web_ui/backend/server.py:1335`) and discards the returned future - while the send itself is an inline `await ws.send_json(event)` (`web_ui/backend/server.py:1307`) with no queue in front of it (`web_ui/backend/server.py:1302-1307`). What a slow client blocks is therefore the event loop: every other task on that loop waits behind the pending write, the backlog is a list of scheduled coroutines rather than a bounded buffer, and nothing reports its depth. A subscriber that raises has its event logged and discarded while the producer continues (`agent/events.py:483-489`), so that failure is visible only in the log. And a consumer that attaches late has missed everything already delivered, because neither channel replays history to a new subscriber - the worker list above is the one bounded exception. The socket writer carries a hazard of its own, documented in the module as a race in the legacy websockets internals around `send_json` (`web_ui/backend/server.py:368`).

Stated as absences, with the lines checked: no bounded queue between generator and socket (`agent/events.py:475-489`, `agent/controller/__init__.py:523`, `web_ui/backend/bridge.py:489-502`, `web_ui/backend/server.py:1302-1307`, `:1319-1335`, `:1384`); no coalescing of repeated token updates; no drop counter; no per-consumer buffer; no slow-consumer eviction; no replay for late subscribers.

### 7.5 What is not streamed

Model output. The model call is a single synchronous `self.llm_client.chat_completion(...)` (`agent/core/agent.py:1121`) returning a complete response with `.usage`, and the next yield after it is `agent/core/agent.py:1164`: nothing is emitted while tokens are being produced, so no consumer can render a growing answer, and the smallest unit of visible progress is a finished model turn.

Tool execution. A tool that runs for minutes produces no stream traffic; the surrounding code yields at turn boundaries (`agent/core/agent.py:1164-1233`), and `TOOL_CALL_START` exists in the enum (`agent/events.py:31`) without being emitted from the turn loop.

Approvals. `SECURITY_PROMPT` and `SECURITY_RESPONSE` (`agent/events.py:59-60`) never travel on the yield stream; the web front end learns of an approval only by subscribing to the global bus (`web_ui/backend/bridge.py:621-670`). A consumer that iterates `process_query` alone sits in a blocked turn with no indication that a human decision is pending.

Workers. Worker status, heartbeat and completion kinds (`agent/events.py:76`, classes `:303`, `:382`, `:315`) reach a front end through the worker bus and the bridge's subscriptions (`web_ui/backend/bridge.py:672-681`), never through the parent's yield stream, so sub-agent progress is invisible to anything that only reads the generator.

Per-request model records. `LLM_REQUEST`, `LLM_RESPONSE` and `RAW_RESPONSE` are declared (`agent/events.py:27-29`), but the turn loop emits nothing of the kind at the call site (`agent/core/agent.py:1121`); a reader looking for a request-level record on either channel will not find one there.

Token accounting, by contrast, is streamed: `_create_token_update_event` (`agent/core/agent.py:868`) is yielded repeatedly while a turn runs (`agent/core/agent.py:1039`, `:1046`), which is why a live progress bar is easy and a live answer is not.

## 8. Tool dispatch and the gate chain

### 8.1 The dispatch entry point

Every model tool call in a turn passes through one method: `ToolExecutor.execute_tool_calls` (`agent/core/tool_executor.py:193`), signature `(tool_calls, add_to_conversation_func, update_token_func=None, agent_id=0, session_id='', workspace_id='', turn_transaction=None)`, return type `Tuple[List[Dict[str, Any]], bool, Optional[Dict[str, Any]], Optional[str], Optional[int]]` (`agent/core/tool_executor.py:193`).

The five elements, in order: `executed_tools`, one `{'name', 'arguments', 'result'}` dict per call (`agent/core/tool_executor.py:342`); `final_detected`, true when a Respond tool ran (`agent/core/tool_executor.py:318-319`); `respond_result`, that tool's `response_type`, `content`, `status`, `confidence` and `meta` (`agent/core/tool_executor.py:320-326`); `summary_text` (`agent/core/tool_executor.py:329`); and `summary_keep_recent_turns` (`agent/core/tool_executor.py:330`), which is `None` unless a summariser ran (`agent/core/tool_executor.py:343`).

`agent/core/agent.py:1323` unpacks all five in one assignment and then uses each: `executed_tools` becomes `processed_tools` with a `success` flag and an `error` field per entry (`agent/core/agent.py:1324-1333`) and is emitted as `tool_call` then `tool_result` events (`agent/core/agent.py:1338-1345`); `final_detected` and `respond_result` decide whether `agent_responded` is yielded and what it carries (`agent/core/agent.py:1357-1367`); `summary_text` and `summary_keep_recent_turns` are passed straight into `_apply_summary_pruning` (`agent/core/agent.py:1382-1384`). The cooperative stop path returns the same five slots with `None` in the last four (`agent/core/tool_executor.py:269`), so every element has a defined absent value.

### 8.2 Executor construction and the state it accumulates

`Agent.__init__` builds one executor per agent, `ToolExecutor(self.tool_classes, config, None, self.logger, self.security_available, agent=self, event_bus=event_bus or global_event_bus, is_worker_context=config.worker_mode)` (`agent/core/agent.py:167`); the constructor stores the classes, config, state, logger, the agent back-reference, the bus and the worker flag (`agent/core/tool_executor.py:166-191`) and falls back to the process-wide `global_event_bus` when no bus is given (`agent/core/tool_executor.py:25-28`).

That call site is worth reading literally: it passes `None` for the state argument and `is_worker_context=config.worker_mode` (`agent/core/agent.py:167`), so the executor's worker flag is a property of the configuration rather than of who constructed the agent - a configuration with worker mode enabled gives a main agent worker semantics (deny instead of prompt, `security/security_gate.py:1612-1616`) with nothing at the construction site marking that as unusual.

Almost nothing accumulates. Every dispatch decision re-reads `config` and `state` on the call itself (`agent/core/tool_executor.py:396`, `:417-419`). The single exception is `self._last_effective_permissions`, initialised to `{}` at construction (`agent/core/tool_executor.py:191`), written only at `agent/core/tool_executor.py:757` inside `_record_permission_widening` (`agent/core/tool_executor.py:701`) and read only at `agent/core/tool_executor.py:729`. That method runs only for worker executors (`agent/core/tool_executor.py:522-523`) and only publishes an audit event when the resolved profile grew more permissive, so no permission decision reads the stored value back.

### 8.3 The chain, in order

- Allowlist. Before any parsing, each call is tested with `self.state.is_tool_allowed(tool_name)` (`agent/core/tool_executor.py:271`); a blocked name gets `_create_tool_rejection_message` (`agent/core/tool_executor.py:801`) as its result and the loop moves on (`agent/core/tool_executor.py:272-277`).
- Argument parsing. `json.loads` (`agent/core/tool_executor.py:281`), then `repair_loads` on failure (`agent/core/tool_executor.py:284`); JSON that cannot be repaired becomes a tool result, never an exception (`agent/core/tool_executor.py:289-295`).
- Resolution. The tool class is found by stable `tool_name()`, then by class name (`agent/core/tool_executor.py:301-308`); an unknown name yields `Unknown tool: ...` with `tool_type: normal` (`agent/core/tool_executor.py:309-313`).
- Per-call execution. `_execute_single_tool` (`agent/core/tool_executor.py:345`) holds every gate below; it copies the arguments first (`agent/core/tool_executor.py:362`).
- Schema validation. Keys the model emitted that are not model fields are stripped (`agent/core/tool_executor.py:374-377`) and the remainder is validated with `tool_class.model_validate` (`agent/core/tool_executor.py:379`); failure returns `Invalid arguments: ...` (`agent/core/tool_executor.py:386`). Validation deliberately runs before injection (`agent/core/tool_executor.py:363-389`).
- Gate availability. If the module-level gate import failed, `_ensure_gate_imported` retries it (`agent/core/tool_executor.py:406-407`); a gate still unavailable denies with `Permission denied: security gate unavailable ...` (`agent/core/tool_executor.py:408`).
- Permission gate. Workspace resolution plus `tool_class.get_required_categories(arguments)` (`agent/core/tool_executor.py:417-419`), the effective profile (`agent/core/tool_executor.py:449-466`), then `check_required_categories` (`security/security_gate.py:1483`) called with the effective dict, the tool arguments, the bus and the worker flag (`agent/core/tool_executor.py:468-503`).
- Ask path. Inside the gate, a requirement resolving to `ASK` is collected (`security/security_gate.py:1604-1606`); with no interactive user - `event_bus is None`, or `is_worker_context`, or a `NullEventBus` - the call is denied without blocking (`security/security_gate.py:1612-1616`); otherwise the gate publishes `SecurityPromptEvent` (`security/security_gate.py:1626-1638`) and waits on a per-request queue with `PROMPT_TIMEOUT` (`security/security_gate.py:1642`).
- Resource binding. `check_requires_resource` (`security/security_gate.py:1413`) runs after the category check (`agent/core/tool_executor.py:533`).
- Denial exit. A failed gate returns its message as the tool result with `tool_type: normal` (`agent/core/tool_executor.py:525-526`, `:540`).
- Injection. Session and effective permissions, workspace id, agent config, session id, worker name and the worker flag are written onto the arguments (`agent/core/tool_executor.py:541-604`); credential placeholders are resolved after that (`agent/core/tool_executor.py:606-655`).
- Backend dispatch. `tool_class(**tool_args)` (`agent/core/tool_executor.py:657`), one strip-and-retry when an injected field is not a model field (`agent/core/tool_executor.py:658-666`), then `tool_instance.execute()` (`agent/core/tool_executor.py:677`).
- Post-processing. Framework truncation (`agent/core/tool_executor.py:680-681`), the result shaped by tool type (`agent/core/tool_executor.py:682-695`), exceptions flattened to `Error executing tool: ...` (`agent/core/tool_executor.py:696-699`); the loop then appends the result to the conversation (`agent/core/tool_executor.py:334-337`), charges its tokens (`agent/core/tool_executor.py:338-339`) and returns the five-tuple (`agent/core/tool_executor.py:343`).

### 8.4 The permission model

The executor decides nothing itself: it resolves an effective profile and hands every decision to `security/security_gate.py`. The in-memory source is `config.session_permissions` (`agent/core/tool_executor.py:396-398`); `get_effective_permissions` (`security/security_gate.py:880`) merges it against the workspace ceiling and returns the flat six-key dict `filesystem`/`network`/`container`/`git`/`mcp`/`host_bash` (`security/security_gate.py:891-894`), the merge itself being `apply_workspace_ceiling` (`security/security_gate.py:224`, called at `security/security_gate.py:435`).

Which source wins is decided inside that function by the two ids. With both `session_id` and `workspace_id` it takes disk mode (`security/security_gate.py:965`), reading grants and ceiling from the vault permission store (`security/security_gate.py:967-969`) and substituting a deny-all session plus deny-all ceiling on any read error (`security/security_gate.py:970-988`, `:830-835`) rather than fabricating defaults; store imports are lazy so the in-memory path never touches the vault (`security/security_gate.py:842-846`, `:960-963`). With either id missing the merge runs in memory, and the executor passes `_DISK_FAIL_CLOSED_SESSION` as the session rather than its own mirror, so an id-less executor can only ever restrict (`agent/core/tool_executor.py:456-466`).

In practice every main-agent call takes disk mode, because the caller supplies both ids from its own session (`agent/core/agent.py:1323`). That is the design's point: a grant or revocation written between two tool calls is re-read on the second, and a store failure is loud in the log (`security/security_gate.py:975-981`) and closed at the gate instead of quietly permissive.

An `ask` grain is carried through the merge unchanged and only becomes a decision at the category check: a session-level `ask` survives the ceiling (`security/security_gate.py:268-272`), the check turns it into `result == 'ASK'`, and the gate then either denies for want of a user or blocks on the prompt queue (`security/security_gate.py:1604-1656`). One asymmetry is worth naming: a ceiling-fabricated cap on `host_bash` falls to `banned` rather than `ask`, because prompts may only come from genuine session-level `ask` grants (`security/security_gate.py:340-344`).

### 8.5 Injected parameters, and what a model cannot forge

All injection happens in `_execute_single_tool` on a copy of the model's arguments (`agent/core/tool_executor.py:362`), after validation (`agent/core/tool_executor.py:374-389`). `session_permissions` is written at `agent/core/tool_executor.py:552` (worker context, from the live effective dict), `:554` (from `config.session_permissions`) or `:556` (a default constant); `effective_permissions` and `workspace_id` at `agent/core/tool_executor.py:560-561`, or `{}` at `:563`; `agent_config` at `agent/core/tool_executor.py:567-582`, or `None` at `:584`; `session_id` at `agent/core/tool_executor.py:588` and `worker_name` at `:597`, both only when the class declares the field; `is_worker_context` unconditionally at `agent/core/tool_executor.py:604`.

These are assignments, not merges, so every one of them overwrites a same-named key the model supplied. The two conditional ones are skipped entirely when the class does not declare the field, and any injected field the class does not declare is stripped by the instantiation retry (`agent/core/tool_executor.py:658-666`).

The important case is the worker flag. `is_worker_context` is a real field on the base tool model (`tools/base.py:74`), so a model that emits `is_worker_context: true` passes validation at `agent/core/tool_executor.py:379` rather than being rejected as an unknown key - but the value is then overwritten from the executor's own construction-time flag (`agent/core/tool_executor.py:604`) before the instance exists (`agent/core/tool_executor.py:657`). That flag comes from `config.worker_mode` (`agent/core/agent.py:167`), which the worker construction sets true (`tools/workspace/worker_thread.py:4311`). So a model cannot obtain worker privileges by emitting the flag, in either permission mode, for any tool that inherits the base model. It is also not an input to the permission decision at all - the worker cap comes from `self.config.permission_footprint` (`agent/core/tool_executor.py:498-503`), not from tool arguments - so even a forged flag would not change category gating. What it does change is prompt-versus-deny (`security/security_gate.py:1612`) and which session dict the tool receives (`agent/core/tool_executor.py:544-556`). The same argument covers `session_permissions` and `effective_permissions`: both are overwritten from executor-held values (`agent/core/tool_executor.py:552-563`), so a permissive dict supplied by the model is replaced before the tool sees it, and the tool's in-tool checks run against the gate's resolution.

### 8.6 A refusal is a result, not an exception

A denial reaches the model as an ordinary tool result. Every gate exit returns `{'result': <message>, 'tool_type': 'normal'}` (`agent/core/tool_executor.py:408`, `:428`, `:526`, `:540`), the same shape a successful tool returns at `agent/core/tool_executor.py:695`, and the loop stores it as the `content` of a `tool` message (`agent/core/tool_executor.py:334-337`).

Only prose distinguishes it. The gate prefixes `Permission denied: ` (`security/security_gate.py:1585`, `:1614`, `:1649`, `:1654`) and the executor's fail-closed exits do the same (`agent/core/tool_executor.py:408`, `:428`), while the allowlist rejection has its own wording (`agent/core/tool_executor.py:813-815`). There is no distinguishable field, no error type and no structured detail: a refusal and a success are the same two keys with the same types.

Classification is therefore unreliable, and worth stating plainly. `agent/core/agent.py:1324-1333` marks a call failed only when the result begins with a cross-mark character or contains `TOOL CALL REJECTED` or `Error executing tool` (`agent/core/agent.py:1330-1332`). A `Permission denied: ...` result matches none of the three, so a refused call is emitted with `success: true` and `error: None` in both the `tool_call` and `tool_result` events (`agent/core/agent.py:1333`, `:1339`, `:1343`). A consumer that needs to know a call was refused must read the result text, not the flag.

### 8.7 The trailer convention

The Git tools append two lines to their result, `execution_mode: <containerized|host|unavailable>` and `failure_reason: <reason>`. The producer is the tool itself: `tools/git_info_tool.py:1666` formats the first line, drawing on the mode recorded during the call at `tools/git_info_tool.py:279`, and the contract is restated in the docstrings (`tools/git_info_tool.py:253`, `tools/git_write_tool.py:98`).

Nothing enforces it. The executor treats `tool_result` as an opaque string (`agent/core/tool_executor.py:677-681`), no code under `agent/` or the web backend parses either line - a tree-wide search for `execution_mode: ` finds only the two Git tools and tests - and the only readers are tests (`tests/test_git_write_tool.py:721`, `tests/security/test_git_info_tool_operations.py:21`).

Because it is conventional, omission degrades silently: the tool still returns a string, `success` is computed from the three prose markers above and is unaffected, and a model or front end expecting the trailer sees an ordinary result with no indication that the execution mode went unreported. What is lost is the one fact the dispatch path deliberately does not model - whether a tool ran containerized, on the host, or not at all.

### 8.8 What the dispatch path does not do

Stated as absences, each with the lines checked.

- No rate limiting. Nothing counts or spaces tool calls; `rate_limit` across `agent/`, `security/` and `tools/base.py` matches only the model client's backoff counters (`agent/core/agent.py:186-190`), and calls run as fast as the model emits them (`agent/core/tool_executor.py:245`).
- No per-tool timeout. `timeout` appears in the executor only as a config value copied into `agent_config` (`agent/core/tool_executor.py:580`) and in a docstring (`agent/core/tool_executor.py:805`); `execute()` is called bare (`agent/core/tool_executor.py:677`). The only bounded blocking wait in the chain is the approval queue (`security/security_gate.py:1642`, `PROMPT_TIMEOUT` imported at `security/security_gate.py:34`).
- Output truncation does exist, and it is the framework's: `agent/core/tool_executor.py:680-681` calls `tools/base.py:312` with a limit injected at `agent/core/tool_executor.py:393-394`, measured as a chars/4 estimate (`tools/base.py:309-310`), with tools able to opt out through `skip_output_truncation` (`tools/base.py:314`).
- No retries. `agent/core/tool_executor.py:661` says `retry once`, but it re-instantiates after stripping unknown fields; the tool body never runs twice.
- No idempotency or deduplication. Calls execute in list order (`agent/core/tool_executor.py:245`) with no memoisation by `tool_call_id`, so a repeated id runs twice.
- The one per-call control that exists is cooperative cancellation between calls: `stop_check` turns every remaining call into a `status: stopped` result and returns early (`agent/core/tool_executor.py:252-269`). It is a checkpoint, not a timeout - a tool already executing cannot be interrupted from here.

## 9. The tool catalog

This section is the inventory behind Section 8: what a tool is as a type, where the list of them comes from, how a name in a model response becomes a class, and which of the resulting tools declare a permission category of their own.

### 9.1 The contract a tool satisfies

A tool is a Pydantic model that subclasses `ToolBase` (`tools/base.py:36`) and adds an `execute()` that returns a string; the base states the rule in the string literal at `tools/base.py:38-41` and the unimplemented default raises `NotImplementedError` (`tools/base.py:298-299`).

That documentation block sits after the `model_config` assignment (`tools/base.py:37-41`), so it is a bare string expression rather than a class docstring and `ToolBase.__doc__` does not carry it; the contract is in the source, not in introspection.

`model_config = ConfigDict(extra="forbid")` (`tools/base.py:37`) is the reason argument handling is strict: a field the class does not declare is a validation failure, not an ignored key, which is what forces the executor's strip-and-retry pass at `agent/core/tool_executor.py:658-666`.

Identity is `tool_name()`, which returns the `name` ClassVar or the class name (`tools/base.py:93-98`); `name` exists so a class can be renamed without breaking presets and the `/api/tools` surface (`tools/base.py:90-93`).

### 9.2 What the base class supplies without being asked

Nine injected fields arrive from the executor rather than from the model: `workspace_path` marked `[DEPRECATED]` (`tools/base.py:43`), `token_limit` (`:44`), `is_docker` (`:45`), `container_workspace_path` (`:46`), `session_permissions` (`:50`), `effective_permissions` (`:57`), `agent_config` with `exclude=True` (`:65`), `session_id` (`:69`) and `is_worker_context` (`:74`).

Five ClassVars let a subclass declare without code: `requires_capabilities` (`tools/base.py:77`), `required_categories` (`:81`), `requires_resource`, denied unless the session holds `<resource>:read` (`:88`), `name` (`:93`) and `skip_output_truncation` (`:102`).

`get_required_categories(params)` is the hook that makes a requirement depend on the argument rather than the class: the default returns the static `required_categories` (`tools/base.py:105-120`) and the docstring names the case in use, `FileEditor` requiring `filesystem:read` for reads and `filesystem:write` for writes and deletes (`tools/base.py:110-112`).

Behaviour, not just fields, is inherited: `_resolve_registry_workspace()` resolves through `SessionRegistry` then `WorkspaceRegistry` and only falls back to the deprecated `workspace_path` with a warning (`tools/base.py:263-296`); `_validate_path` is the shared containment check (`:345-395`); `_estimate_tokens` is the chars/4 approximation (`:305-310`); and `_truncate_output` (`:312-343`) honours `skip_output_truncation`, cuts to `limit * 4` characters, prefers the last newline when it lies past eighty percent of the target, then chops a hundred characters at a time and appends `... (output truncated to {limit} tokens, original was {estimated_tokens} tokens)`.

### 9.3 The registry, and how a name becomes a class

`tools/__init__.py` is the registry: `TOOL_CLASSES` (`:25`), `SIMPLIFIED_TOOL_CLASSES` derived from it minus the eight legacy file names in `FILE_TOOL_BLACKLIST` (`:30-39`, applied by `_update_simplified_toolset` at `:41-60`), and one `try`/`append` block per tool that records failures in `IMPORT_FAILURES` (`:11-22`) instead of importing quietly (`:108-304`).

Outside toolmodules the lists are read through accessors - `iter_tool_classes` (`tools/__init__.py:88-90`), `simplified_tool_classes` (`:93-95`), `get_tool_class` (`:98-103`), `register_tool_class` (`:75-85`) - with the registry described as module state having one writer and no lock (`tools/__init__.py:70-74`).

Resolution in the executor is a linear scan, not a dictionary: `next(cls for cls in self.tool_classes if cls.tool_name() == tool_name or cls.__name__ == tool_name)` (`agent/core/tool_executor.py:299-308`), and an unmatched name becomes the result `Unknown tool: {tool_name}` (`:309`).

Two properties follow and both matter at the point of change: the tool surface is whatever `self.tool_classes` held when the executor was constructed, and two classes sharing a `tool_name()` are not detected - the first in list order wins silently, because nothing validates uniqueness.

### 9.4 The filter between registry and agent

A registered tool is not necessarily an available tool. `get_filtered_tool_classes` starts from `SIMPLIFIED_TOOL_CLASSES` (`agent/config/models.py:314-315`), drops `SearchCodebaseTool` when `rag_enabled` is false (`:316-317`), drops `KnowledgeBaseTool` when `kb_enabled` is false (`:318-319`), then applies `enabled_tools` by comparing against `tool_name()` or the class name (`:320-336`) after rewriting two legacy identifiers, `GitInfoTool` to `git_read` and `GitWriteTool` to `git_write` (`:325-329`).

The agent takes that list once, at construction: `self.tool_classes = config.get_filtered_tool_classes()` (`agent/core/agent.py:158`), builds the schemas it shows the model from the same list (`:166`) and hands it to the executor (`:167`). Nothing re-reads it later in the turn loop, so the model's view of what it can call is fixed for the lifetime of the agent.

### 9.5 The catalog, as counted

Thirty-seven classes are appended to `TOOL_CLASSES`, in this order: FileEditor, ReadFile, FilePreviewTool, DirectoryTreeTool, GlobTool, FileSearchTool, ApplyEdits, CodeModifier, RefactorTool, SearchCodebaseTool, DateTimeTool, DirectoryCreator, DockerCodeRunner, ContainerStartTool, ContainerExecTool, ContainerStopTool, ContainerRestartTool, ContainerRemoveTool, ContainerStatusTool, ContainerListTool, ContainerBuildTool, ContainerLogsTool, FieldViewer, FileMover, FileSummaryTool, CiReadTool, GitReadTool, GitWriteTool, HostBashTool, KnowledgeBaseTool, ProgressReport, Respond, SummarizeTool, Thought, CheckSystem, Worker, WorkingDocument (`tools/__init__.py:110-302`).

The file census explains the small number relative to the directory: `tools/` contains 35 `.py` files and `tools/workspace/` another 13; 27 top-level modules and 3 modules under `tools/workspace/` define tools, 18 are non-tool support (`__init__`, `base`, `utils`, `code_modifier_utils`, `host_resource_policy`, `mcp_client`, `mcp_manager`, `test_audit`, and the ten `workspace/` modules for job, container, thread, timeout, lifecycle, manager, query, registry and the package marker).

Thirty-eight concrete subclasses exist, not thirty-seven: `MCPServerConnect` (`tools/mcp_server_connect.py:54`) is a real `ToolBase` subclass with `required_categories = ["mcp:connect"]` (`:57`) that no `TOOL_CLASSES` line names, so the executor at `agent/core/tool_executor.py:299-308` can never resolve it and a model that calls it gets `Unknown tool`.

Shared bases are not themselves tools: `ToolBase` (`tools/base.py:36`) and `_ContainerControlBase` in `tools/container_control.py`, which the nine container tools are imported from in one block (`tools/__init__.py:187-197`).

### 9.6 What each tool declares to the gate

Most tools declare nothing and inherit `required_categories = []` (`tools/base.py:81`), which means they pass the category check and are bounded only by the filesystem grain where their own code calls `_validate_path`.

Where a tool does declare, the category is the gate grain from Section 8.4: `DockerCodeRunner` declares `["filesystem:write", "container:true"]` (`tools/docker_code_runner.py:61`), `MCPServerConnect` declares `["mcp:connect"]` (`tools/mcp_server_connect.py:57`), and a generated MCP tool declares `["mcp:connect"]` for a stdio server or `["network:outbound"]` for an HTTP or SSE server (`tools/mcp_manager.py:226-234`).

Two tools enforce their grain themselves rather than through `required_categories`: `HostBashTool` reads `effective_permissions` and fails closed on anything but `ask` or `allow` (`tools/host_bash_tool.py:267-300`), which Section 10.5 treats in full.

### 9.7 The two tools the executor knows by type

Respond and SummarizeTool are special not because of their category but because the executor inspects the instance: it imports both by name (`agent/core/tool_executor.py:16-17`), switches on the tool type to set `final_detected` with `response_type`, `content`, `status`, `confidence` and `meta` (`:314-326`) or `summary_requested`, `summary_text` and `summary_keep_recent_turns` (`:329-331`), and tests `isinstance` when shaping the result (`:682`, `:692`).

They are also the two tools named in the truncation opt-out comment (`tools/base.py:100-102`), so their output is never shortened - which is the property that makes a long final answer survive the framework cap that Section 8.8 describes.

Thought and ProgressReport carry no executor special case: a search of `agent/core/tool_executor.py` for either name finds no branch, so their results are ordinary strings and their side effects are whatever the tool body does.

### 9.8 Tools generated at runtime

MCP tools do not exist as source. `create_tool_class` (`tools/mcp_manager.py:163`) builds a `ToolBase` subclass per remote tool, named with an `MCP` prefix (`:193`), with a `tool` Literal field (`:201`) and a category chosen from the transport (`:226-234`); `MCPServerManager.start_all` (`:261`) and `list_tools` (`:289`) feed it, appending generated classes to the registry (`:292-294`).

The timing is the part to know: `register_mcp_tools(timeout=5.0)` (`tools/mcp_manager.py:341`) runs on a daemon thread, and the agent starts it at `agent/core/agent.py:159-165` - after `self.tool_classes` was taken at `:158` and before the schemas are built at `:166`. The generated classes therefore reach `TOOL_CLASSES` at an unspecified later moment and are not re-read into the running agent's list, so in this path an MCP tool is resolvable only by a later agent, not by the one that spawned the thread.

The connection tool is the exception that proves the pattern: `MCPServerConnect.execute` checks its own grain by passing `required_category="mcp:connect"` into `SandboxedExecution.run` (`tools/mcp_server_connect.py:93-105`), so that one refuses correctly even though the executor cannot dispatch it.

### 9.9 What the catalog does not guarantee

No uniqueness check. `tool_name()` collisions resolve by list order (`agent/core/tool_executor.py:299-308`) and nothing reports the loser.

No completeness check on import. A failed import is recorded in `IMPORT_FAILURES` (`tools/__init__.py:11-22`) and logged at ERROR, but the agent starts with the smaller list and the model is simply never offered the tool.

No ownership of the surface by the agent. Because `tool_classes` is captured once (`agent/core/agent.py:158`) and the registry is mutable module state written by an MCP thread (`tools/mcp_manager.py:292-294`), the catalog a process advertises is not the catalog its agents use.

## 10. Execution substrates

A dispatched tool (Section 8) runs in one of three places: the agent process itself, a Docker container, or the host. Which substrate applied is not modelled anywhere in the dispatch path; it is decided inside each tool body, and reported, when at all, in prose.

### 10.1 The vocabulary, and the one producer of it

The words are `containerized`, `host` and `unavailable`, and the only code that emits them is the Git tools: the trailer is documented at `tools/git_info_tool.py:253` and `tools/git_write_tool.py:98`, formatted at `tools/git_info_tool.py:1666` from the mode recorded during the call (`:279`, `:606`, `:716`).

Resolution is a lookup, not a probe: `resolve_git_execution_mode` (`tools/git_info_tool.py:140-175`) reads the `execution_mode` field of the resource catalog's `git` entry (`:165`) and returns `unavailable` when there is no entry (`:169`); a legacy `git_execution_mode` config key is popped and ignored by `_migrate_legacy_git_execution_mode` (`:55-71`).

`unavailable` and a degraded `host` are refusals, not fallbacks: the call fails loudly at `tools/git_info_tool.py:887-891`, and the two comments restating the rule are at `:778` and `:848`. A degraded `host` means the container path was configured and could not be used, so the tool declines rather than silently running somewhere else.

### 10.2 In-process: the substrate nobody names

Every tool that is not `DockerCodeRunner`, not one of the nine container tools and not `HostBashTool` runs in the agent's own interpreter, with the agent's privileges and the process's filesystem view; `execute()` is called directly at `agent/core/tool_executor.py:677` and there is no sandbox between that call and the tool body.

For those tools the only boundary is cooperative: the declared five grains are checked before dispatch through `required_categories` (`tools/base.py:81`) and `get_required_categories` (`:105-120`), and file containment is enforced afterwards, inside the body, by `_validate_path` (`tools/base.py:345-395`).

That is why the file tools can be described as gated rather than isolated: a bug in `_validate_path` is executable code with the agent's own reach, and the six-grain gate is the only thing in front of it.

### 10.3 The container substrate: one container per call

`DockerCodeRunner` (`tools/docker_code_runner.py:60`) declares `["filesystem:write", "container:true"]` (`:61`) and its `execute` (`:295`) owns a full container lifecycle: it constructs `ContainerManager` (`:394-402`), optionally runs the build-drift gate through `manager.build_image` (`:403-409`), starts with `lifecycle_class=LIFECYCLE_EPHEMERAL, heal_missing=True` (`:411-416`), executes (`:450-456`) and stops in a `finally` (`:457-458`).

The drift path is explicit rather than automatic: `manager.start` refuses a reused container whose live isolation is more permissive than the resolved policy and does not mutate it (`tools/docker_code_runner.py:421-427`), so this tool removes the drifted container and creates a fresh one (`:428-446`), and a deny payload without a `container_id` raises instead of skipping the remove (`:434-439`).

The isolation the container provides is asserted in the class docstring rather than enforced here (`tools/docker_code_runner.py:100-120`: read-only root filesystem, dropped capabilities, no network by default, non-root user, workspace mounted at `/workspace`), and the docstring's description of an idle-pooled container reused across calls (`:113-117`) is contradicted by the start/exec/stop sequence above, which never reuses an id between calls - a code/docstring tension, not a behaviour.

Result shape is fixed: `success`, `exit_code`, `stdout`, `stderr`, `command`, `duration`, `timed_out` and `error` (`tools/docker_code_runner.py:87-97`), every return path wrapped in `self._truncate_output(self._build_json_response(...))` (`:301`, `:325`, `:337`, `:348`, `:368`, `:472`, `:484`, `:492`, `:501`), and a timeout is reported as `exit_code -2` with `timed_out` set (`:466-471`, handled `:466-495`).

Script mode does not escape that shape: the script is written to a temporary file and turned into a command by `_prepare_script_command` (`:233-262`) before the same single `exec` runs it.

### 10.4 The persistent container substrate

The nine container tools registered from `tools/container_control.py` (`tools/__init__.py:187-197`) are the substrate that survives across calls: ContainerStart, ContainerExec, ContainerStop, ContainerRestart, ContainerRemove, ContainerStatus, ContainerList, ContainerBuild and ContainerLogs share `_ContainerControlBase` and differ in the manager verb they wrap.

Because the handle is a container id the model carries between calls, persistence there is a conversation-level property rather than a framework one: nothing links a `ContainerStartTool` call to a later `ContainerExecTool` call except the id the model remembered, and no executor state tracks it.

### 10.5 The host substrate

`HostBashTool` (`tools/host_bash_tool.py:65`) runs on the host, outside Docker, and the docstring states the cost plainly (`:2-40`): it exists for work the container cannot do, and it is governed by an operator policy rather than by isolation.

The policy is checked in a fixed order, and the order is the security property. The grain is read only from the executor-injected `effective_permissions` (`tools/host_bash_tool.py:267-271`) - absent means `banned`, and a stale session mirror is never consulted - then the workspace `allow_host_resources` flag is checked first (`:272-285`), then the grain must be `ask` or `allow` (`:286-300`).

`ask` has no interactive user in a worker, so it denies with a standardised message (`tools/host_bash_tool.py:301-307`); when it does prompt, an approval timeout is also a denial (`:307-309`); approval itself is requested through `_request_approval` with a `SecurityPromptEvent` and `HOST_BASH_APPROVAL_TIMEOUT` (`:173-229`).

The `allow_host_resources` check is deliberately paranoid: `load_workspace_config` returns an empty dict on any error (`tools/host_resource_policy.py:22-58`) and `workspace_allows_host_resources` enables the surface only for a JSON boolean true, so the strings `true` and `yes`, the integers 1 and 0 and null all read as false (`:61-74`).

Execution is `subprocess.run(..., shell=True, timeout=HOST_BASH_TIMEOUT)` (`tools/host_bash_tool.py:311-333`) and every invocation is appended to `host_port_audit.jsonl` by `_audit_log` (`:142`); the module docstring records that this workspace flag is also intended as the ceiling for other host-side surfaces such as the Git host fallback (`tools/host_resource_policy.py:1-15`).

### 10.6 The persistence boundary

For the container substrate, what survives a `DockerCodeRunner` call is exactly what is on the mounted workspace volume; the container's own filesystem, its processes and its environment do not, because the container is stopped in the same `execute` that started it (`tools/docker_code_runner.py:411-458`).

For the host substrate nothing is bounded at all: `HostBashTool` has the operator's machine, and the only limits are the subprocess timeout and the audit line.

For the in-process substrate the state that survives is process memory - which is why the only inter-call state in the dispatch path is the single `_last_effective_permissions` field described in Section 8.2 (`agent/core/tool_executor.py:191`).

### 10.7 The records behind the container substrate

Containers are not anonymous. `thoughtmachine/container_record/` holds the bookkeeping: a `Record` dataclass with `id`, `lifecycle_class`, `owner`, `docker_id`, `purpose`, `intent_snapshot`, `notes`, `event_log`, `schema_version`, `inferred`, `state`, timestamps, `restart_policy`, `name`, `retention_days`, `user` and `permissions`, the six-category grant captured at create time (`thoughtmachine/container_record/models.py:182-218`).

The lifecycle vocabulary the runner uses comes from there - EPHEMERAL, PERSISTENT, RESOURCE, SERVICE with owners and creating/running states (`models.py`, exported through `thoughtmachine/container_record/__init__.py:16-30` alongside `create_record`, `begin_record`, `attach_container`, `load_record`, `list_records`, `find_by_docker_label`, `update_record`, `append_event`, `delete_record` and `read_event_log`).

`infra/container_manager.py` is the reader: it imports records, snapshot extraction, drift and the creation hook (`:131-172`) for the name index (`:1288-1318`), notes (`:1162-1252`), heal decisions (`:1443-1450`, `:1580-1590`) and OOM event appending (`:1072-1077`); creation is wrapped in the `record_creation` hook (`infra/container_create.py:48-49`) and teardown appends an event (`infra/container_registry.py:1103-1113`).

Records live in the default vault, which the manager names as the single source of truth (`infra/container_manager.py:1165-1167`), and state is read only through `resolve_container_state` (`thoughtmachine/container_record/models.py:199-204`).

### 10.8 What the substrate layer does not model

There is no substrate field on a tool result. The only machine-readable mode belongs to the Git tools and is prose in a string (Section 8.7), so `DockerCodeRunner` and `HostBashTool` results carry no mode marker at all - their shape is JSON, but nothing in it says where it ran in comparison with the other tools.

There is no selection policy. Which substrate a capability uses is decided by the model choosing a tool name, not by the framework; a model that prefers `ContainerExecTool` over `DockerCodeRunner` changes the isolation properties of the run with no gate noticing.

There is no host fallback for containers. `DockerCodeRunner` fails with a JSON error when Docker or `ContainerManager` is unavailable (`tools/docker_code_runner.py:299-306`, `:346-353`) rather than degrading to the host, and the container start path refuses to mount the current directory when the workspace cannot be resolved (`:308-315`).

## 11. Workers, sub-agents and delegation

### 11.1 A worker is an Agent with a different context object

A worker is not a separate process, a separate class hierarchy or a remote service; it is an ordinary Agent whose context object is a WorkerContext instead of a Session, agent/core/worker_context.py:8-13.

The class states its own contract: it supplies just the attributes that Agent.process_query() reads, and deliberately nothing else, agent/core/worker_context.py:4-6.

The surface is small and enumerable: session_id, workspace_id, user_history, total_input_tokens, total_output_tokens, conversation_version, conversation_hash, summary and updated_at, plus _get_next_seq() and _on_conversation_changed(), agent/core/worker_context.py:15-28.

What is missing is the persistence machinery; the module says that this context is not intended to outlive the worker, agent/core/worker_context.py:56-61.

Identity is defaulted rather than required: session_id and worker_name both fall back to a worker- name followed by 12 hex characters when the caller supplies nothing, agent/core/worker_context.py:73-75.

The worker name is also published through a contextvar, WORKER_NAME_CONTEXTVAR at agent/core/worker_context.py:46-48, read through current_worker_name() at agent/core/worker_context.py:51-53, which is what lets a Docker container started by a worker be labelled thoughtmachine.worker and attributed back to that worker.

### 11.2 worker_mode is the whole policy surface

The agent is constructed with is_worker_context=config.worker_mode, agent/core/agent.py:167, and that one boolean is read in four places which together constitute worker policy.

In the security gate an ask outcome becomes a deny, security/security_gate.py:1612-1616, which is the correct behaviour rather than a degradation, because there is no interactive user on the other end to answer the question.

The permission footprint is capped, agent/core/tool_executor.py:498-503, so a worker cannot escalate beyond what the spawning context already held.

HostBashTool refuses on ask, tools/host_bash_tool.py:301-307, which closes the one tool that would otherwise reach the host shell through a prompt nobody can answer.

The remaining use is labelling, agent/core/worker_context.py:40-53, which is observability rather than restriction.

### 11.3 The reply envelope is a fixed dict

Everything a worker says comes back through a single envelope assembled at tools/workspace/worker_thread.py:2811-2818.

content carries the reply text, defaulting to "Worker finished with no output." when Respond produced nothing, tools/workspace/worker_thread.py:2813.

status is taken from _respond_metadata, tools/workspace/worker_thread.py:2807, and is forced to timeout when _timeout_triggered is set, tools/workspace/worker_thread.py:2808-2809; the comment immediately preceding names progress among the possible statuses, tools/workspace/worker_thread.py:2802-2806.

confidence and meta are passed straight through from the Respond metadata, tools/workspace/worker_thread.py:2815-2816.

telemetry is a dict of elapsed_seconds, tool_call_count, timeout_triggered and token_usage, tools/workspace/worker_thread.py:2795-2800, built from counters initialised when the worker starts, tools/workspace/worker_thread.py:1061-1063.

The envelope is stamped afterwards with the query_id, tools/workspace/worker_thread.py:2825-2826, and with the WLM supervisor query id, tools/workspace/worker_thread.py:2828-2830; those two fields are what make a worker's own downstream work traceable from the parent's log line.

The caller never sees the raw envelope: _normalize_reply flattens it to status, content, confidence, meta, telemetry, response, worker_name, instance_label and elapsed_seconds, tools/workspace/worker_query.py:266-304, as its contract docstring states at tools/workspace/worker_query.py:275-278.

The normaliser defaults status to ok both when the key is absent and when the reply is not a dict, tools/workspace/worker_query.py:287 and tools/workspace/worker_query.py:293, so an unparseable worker reply is reported as success.

That default is the sharpest edge in the contract, and the hard-kill terminal path which would compensate for it, _resolve_terminal at tools/workspace/worker_query.py:307, was not read to the end for this document; it is stated here as a gap rather than described.

### 11.4 Soft and hard timeouts

A soft timeout fires inside the worker's own loop and only sets _timeout_triggered, which then forces the envelope status, tools/workspace/worker_thread.py:2802-2809, so a soft timeout still returns whatever work was finished.

A one-shot soft warning rides on soft_timeout_warning_emitted on the lifecycle manager, tools/workspace/worker_thread.py:1933-1937.

A hard timeout is the caller's patience: it waits timeout_seconds plus QUERY_WAIT_GRACE_SECONDS, which is 60, agent/config/defaults.py:34-37, for the explicit reason that the worker's own envelope should win the race.

The timeout is resolved through a chain: an explicit constructor argument, then timeout_seconds on the tool definition, then agent_config.worker_timeout_seconds, then session config, then WORKER_TIMEOUT_SECONDS which is 600, tools/workspace/worker_thread.py:977-994 and agent/config/defaults.py:61.

A hard timeout is not a kill: the force-stop path checks survived_force_stop, calls thread.stop() and tests is_alive(), and keeps the entry in the registry when the thread outlives the stop, tools/workspace/worker_thread.py:3907-3936.

The surrounding constants are SPAWN_QUEUE_TIMEOUT of 600, agent/config/defaults.py:32-33, MAX_WORKERS_PER_SESSION of 3, agent/config/defaults.py:38-39, HEARTBEAT_INTERVAL_S of 30, agent/config/defaults.py:41, and WORKER_MAX_RETRIES of 3, agent/config/defaults.py:64.

### 11.5 Lifecycle, persistence and the event buffer

Context is saved once per query by _save_context(), tools/workspace/worker_thread.py:2790, under the FileLock pattern the module documents at tools/workspace/worker_thread.py:35-39.

Summarisation is asymmetric by design: the agent inserts a summary into user_history without deleting the messages it summarised, tools/workspace/worker_thread.py:2780-2781, so the thread then calls WorkerContext.compact_after_summary() to bring the in-memory view into line, tools/workspace/worker_thread.py:2781-2782.

History is append-only and compaction is a view operation over it, which is why a worker's full transcript survives while the model only ever sees the compacted form.

Worker events reach the UI through a bounded buffer: _worker_event_buffer is a plain list with a lock and _worker_event_buffer_max of 100, web_ui/backend/bridge.py:497-502.

Trimming is append followed by pop(0) while over the cap, web_ui/backend/bridge.py:1476-1479.

Replay happens when a callback is installed: set_event_callback routes to _flush_worker_event_buffer, web_ui/backend/bridge.py:1461-1463, which takes a shallow copy and then empties the list, web_ui/backend/bridge.py:1481-1491.

Overflow drops the oldest event silently and inserts no marker, so a client connecting late cannot tell from the stream that it has missed anything.

### 11.6 What the worker subsystem is not

There is no isolation: the worker is a thread inside the parent process, tools/workspace/worker_thread.py:3909-3921, so a worker that exhausts memory or corrupts shared state takes the parent with it.

There are no per-worker resource limits; agent/config/defaults.py:33-64 bounds time, count and retries, and bounds no memory and no CPU.

There is no fairness mechanism: workers are dispatched from a queue, and MAX_WORKERS_PER_SESSION is a count cap at agent/config/defaults.py:38-39 rather than a share of any resource.

There is no persistence across sessions by design, agent/core/worker_context.py:56-61.

There is no closed status vocabulary: status strings are produced by convention at tools/workspace/worker_thread.py:2802-2818 and defaulted at tools/workspace/worker_query.py:287, so a new status value would be accepted by every consumer without complaint.

No session-scoped rule governing when a worker may be force-restarted was found, which is stated here as a gap in this reading rather than as an absence in the code.

## 12. Context economics: token accounting and compaction

### 12.1 The estimator

Token accounting starts from one class, TokenCounter, agent/core/token_counter.py:11.

It uses tiktoken with the cl100k_base encoding, agent/core/token_counter.py:24-31 and agent/core/token_counter.py:49-51, so counts are model-plausible rather than character heuristics.

When tiktoken is unavailable, or the input is not a string, it falls back to length divided by 4, agent/core/token_counter.py:52-53.

Dict messages are serialised with json.dumps before counting, agent/core/token_counter.py:45-46, which keeps the estimate honest about role and key overhead at the cost of also counting JSON syntax.

A whole request is estimated by estimate_request_tokens, agent/core/token_counter.py:55-70, which is the function callers use rather than counting messages one at a time.

### 12.2 What is estimated, and when

The live figure is state.current_conversation_tokens, rebuilt by _update_conversation_token_estimate, agent/core/agent.py:719-746.

It takes the exact context the model will see from context_builder.get_context_for_llm(), agent/core/agent.py:733-736, rather than measuring the raw history.

It first runs ContextBuilder._cleanup_orphaned_tool_messages, agent/core/agent.py:738, so orphaned tool results are not charged against the budget.

It then sums estimate_tokens over the messages, agent/core/agent.py:739-741, and assigns the total to state.current_conversation_tokens, agent/core/agent.py:742.

That estimate is refreshed at construction, agent/core/agent.py:195, and again after every compaction, agent/core/agent.py:1533.

During the turn the authoritative number is the provider's own prompt_tokens, with estimates filling in only the parts the provider did not report, agent/core/agent.py:1043-1045, so the budget drifts toward truth rather than away from it.

One inconsistency is worth naming: tool output truncation measures length in characters and divides by 4, tools/base.py:305-310, while TokenCounter uses tiktoken, so the two disagree on exactly the inputs that are largest.

### 12.3 Thresholds and restriction

The state machine has three levels, LOW, WARNING and CRITICAL, agent/core/state.py:10-12, with boundaries supplied by config.token_monitor_warning_threshold and config.token_monitor_critical_threshold, agent/core/state.py:170.

Warnings fire once per crossing rather than once per turn, agent/core/state.py:107-112, so the notification is not repeated until the level changes.

The warning text names the consequence directly, including the sentence that only Respond and SummarizeTool remain available, agent/core/state.py:129.

The emitted event carries its data through the keys set at agent/core/state.py:138-139.

CRITICAL applies immediately: restrictions_active is set, restriction_reason becomes token, and there is no grace turn, agent/core/state.py:156-160.

Recovery requires dropping to LOW after a warning has fired, agent/core/state.py:142-147, and clears the restriction, agent/core/state.py:161-165.

The turn loop appends the warning as a [SYSTEM NOTIFICATION] message and charges its tokens to the same budget, agent/core/agent.py:1049-1055, which is the honest accounting choice and also means the warning costs context.

The time-based equivalent is deliberately weaker: a time CRITICAL logs and ends the turn by timeout without applying the token restriction, agent/core/agent.py:1022-1026.

### 12.4 Compaction is pruning, not rewriting

Compaction is _apply_summary_pruning, agent/core/agent.py:1468-1545, and it operates on an append-only user_history: the summary message records where pruning happened instead of removing anything, agent/core/agent.py:1469-1475.

The insertion point comes from _find_summary_insertion_index, agent/core/agent.py:1496.

System messages are excluded from the grouping, and the remaining messages are grouped by _group_messages_into_turns, agent/core/agent.py:1499-1500.

The number of turns kept is min(keep_recent_turns, len(turns)), agent/core/agent.py:1501, so a short conversation is never over-pruned.

The summary message is a system role message whose content is prefixed with the phrase Summary of previous conversation, carrying summary True and the metadata pruning_keep_recent_turns, pruning_discarded_msg_count and pruning_insertion_idx, agent/core/agent.py:1515.

It is inserted at that index or appended, agent/core/agent.py:1516-1521.

Summary text is capped at MAX_SUMMARY_LENGTH of 20000 with a truncation suffix, agent/core/agent.py:1510-1513, so compaction cannot itself become the overflow.

A [SYSTEM NOTIFICATION] recording that context was cleared is appended, agent/core/agent.py:1522-1524, so the model is told what happened to its own history.

The session then points at the new summary, agent/core/agent.py:1526, the context builder cache is cleared, agent/core/agent.py:1528-1530, the estimate is recomputed, agent/core/agent.py:1533, the token state is re-evaluated, agent/core/agent.py:1534-1535, emergency_mode is cleared, agent/core/agent.py:1543, and any token_recovery events are returned, agent/core/agent.py:1545.

A no-session fallback exists for callers without a session object, agent/core/agent.py:1482-1492 and _apply_summary_pruning_fallback at agent/core/agent.py:1548.

One hazard: pruning groups turns with agent.py's own _group_messages_into_turns, agent/core/agent.py:1500, while two further grouping implementations exist at agent/core/conversation_manager.py:61-117 and agent/core/message_utils.py:43-49; whether the three agree is stated here as unverified rather than as divergence.

### 12.5 Truncation and caps

Tool output is capped before it reaches the conversation: the limit is injected at agent/core/tool_executor.py:393-394.

The truncation itself, including the marker appended to the surviving tail, is at tools/base.py:312-343.

Respond and SummarizeTool are exempt from the cap, tools/base.py:100-102, which is deliberate, because the worker's answer and the compaction result must not be cut.

## 13. Session state, persistence and trust boundaries

### 13.1 Session state and its duplications

The session is the object that carries everything which must survive a turn: Session is a dataclass at session/models.py:141, with identity and time (session_id at :143, created_at and updated_at at :144-145), the conversation (user_history at :148), accounting (total_input_tokens and total_output_tokens at :149-150, context_length at :151), durable configuration (runtime_params at :147, preset_name at :154, version at :155, mode at :160, security_config at :163, workspace_id at :159), bookkeeping (next_seq at :156, last_active at :161, metadata at :162), plus containers at :153, agent_context at :152, summary at :157 and an agent_instance at :158.

user_history is not a plain list: it is an ObservableList, session/models.py:26-104, installed by __setattr__, session/models.py:168-194, through _wrap_user_history, session/models.py:209-221, so that every mutation bumps conversation_version, session/models.py:296-298.

That counter is the cache key the rest of the system trusts: get_conversation_snapshot, session/models.py:300-306, and the context builder cache invalidation described in Section 12 both hang off it, which is why the wrapping is done in a setter rather than offered as a helper.

The round trip is four methods: to_persistable_dict, session/models.py:321-327, from_persistable_dict, session/models.py:335-380, update_from_persistable_dict, session/models.py:382-442, and rebind_workspace, session/models.py:444-466.

On the agent side the conversation is not copied. Agent binds self._conversation = session.user_history at agent/core/agent.py:138, so the two names denote one object, and substitutes a fresh empty list when there is no session, agent/core/agent.py:142, in which case the turn's history exists nowhere but that list.

The display turn number is derived once, by counting user messages, agent/core/agent.py:143-149, and thereafter only incremented, agent/core/agent.py:899, so a resumed conversation's numbering comes from a scan of history and the rest of the run comes from arithmetic.

AgentState is constructed at agent/core/agent.py:192 and handed to the executor at agent/core/agent.py:193, and the token counters are seeded from the session's totals at agent/core/agent.py:173.

Four duplications follow, and three of them can disagree while the system is running. The first is the session-less case: with a session, Session.user_history and Agent._conversation are one object and cannot diverge, but with no session the agent's list has no owner and nothing persists it.

The second is Session.summary, session/models.py:157, against the summary message stored inside user_history and mirrored onto the session at agent/core/agent.py:1526; the field is a convenience view, the message is the record, and the assignment site is the only thing keeping them aligned.

The third is two counters both called turn: _display_turn counts user queries, agent/core/agent.py:143-149 and :899, while an inner turn variable feeds state.update_turn_state, agent/core/agent.py:1031, and both are emitted in one payload, agent/core/agent.py:1040, so a single event carries the word in two different senses.

The fourth is token accounting: the totals live on the session, session/models.py:149-150, and the agent holds its own counters seeded from them at agent/core/agent.py:173, so after construction they are separate holders that agree only if every increment reaches both.

The bridge is where the object is made: an existing session is reused, web_ui/backend/bridge.py:1646-1648, or a new one constructed, web_ui/backend/bridge.py:1650-1651, the workspace_id is stamped, web_ui/backend/bridge.py:1657-1659, and the Agent is built around the result, web_ui/backend/bridge.py:1662.

### 13.2 Persistence is one JSON file per session

The store is FileSystemSessionStore, session/store.py:88, and its unit is one file per session, described in the save_session docstring, session/store.py:305-311, as ~/.thoughtmachine/workspaces/<workspace>/sessions/<name>_<short_id>.json.

save_session, session/store.py:304-435, also redirects a save whose workspace_id does not match the directory it is about to write into, session/store.py:333-339, so workspace_id is immutable once stamped rather than merely conventional.

A metadata sidecar is written by _save_session_metadata, session/store.py:268-302, and sidecars carry the _meta_ prefix precisely so that scanning code can skip them, a convention visible in thoughtmachine/permission_store.py:174-178.

Reads take a lock, session/store.py:461, so a load cannot observe a half-written file.

load_session, session/store.py:437-482, rebuilds through Session.from_persistable_dict, session/store.py:467, backfills a legacy workspace_id from metadata.workspace_path, session/store.py:468-477, and returns None on any exception, session/store.py:479-482.

That last behaviour is the important one: a corrupt session and a nonexistent session are the same observation, so a resume cannot distinguish lost history from a session that never existed and can only start over silently.

Listing is list_sessions, session/store.py:653-731, deletion is delete_session, session/store.py:851-920, and the open-session and current-session bookkeeping is session/store.py:938-1053.

Resume does not restore a process. The loaded Session is installed into a fresh Agent, web_ui/backend/bridge.py:1646-1662, so the agent, executor, AgentState, context builder and token counters are rebuilt from scratch; agent_instance is excluded from persistence for exactly this reason, session/models.py:158.

### 13.3 The trust boundary runs between the vault and the workspace

vault_root, thoughtmachine/vault.py:29-39, is the single definition of where durable state lives: THOUGHTMACHINE_VAULT_ROOT when set, otherwise ~/.thoughtmachine.

The workspace registry lives inside it, thoughtmachine/workspace_registry.py:83-95 for the user directory and registry path, with WorkspaceRegistry at thoughtmachine/workspace_registry.py:186-456 and a _load that tolerates corruption, thoughtmachine/workspace_registry.py:215-263.

Per-workspace capability records live there too: the paths at thoughtmachine/workspace_capabilities.py:198-205, load at :208-227, save at :230-240, resolve_workspace_id at :243-285, ensure_workspace_dirs at :301-412, and _safeguard_workspace_dir at :520-551, which is the code that refuses a workspace directory outside the permitted roots.

The boundary is a directory boundary, not an abstraction: the vault sits under the user's home and outside the workspace tree, so nothing inside a workspace is persisted state and nothing in the vault is a workspace file.

CheckSystem is the one tool that reads across it, and its reach is an explicit allowlist rather than a general read: get_checksystem_allowlist and is_path_allowed, thoughtmachine/vault.py:523-590.

Container substrate sits on the workspace side. It sees the workspace mounted at /workspace and does not see the vault.

Line 32 of this document is the worked example of that split: inside the sandbox the .git directory is a 0-byte stub, because the host git tooling reads a real repository while an in-sandbox read of the same path sees a placeholder.

### 13.4 Permissions at rest

Session grants are a file in the vault: session_grants_path, thoughtmachine/permission_store.py:145-159, is <vault>/workspaces/<ws>/sessions/<sid>/permissions.json.

_session_record_path, thoughtmachine/permission_store.py:168-193, locates it by content scan rather than by assumed position, and skips _meta_* files while doing so.

Reads are strict, _read_json_strict, thoughtmachine/permission_store.py:196-214, and normalisation, _normalize_session_permissions, thoughtmachine/permission_store.py:222-260, is where legacy shapes die: git_read and git_write collapse onto git, git_allow_worktree_commits is dropped, only canonical catalog keys survive, and host_bash is preserved because it is not part of that legacy pair.

The public read is read_session_permissions, thoughtmachine/permission_store.py:268-290, bounded by workspace_ceiling, thoughtmachine/permission_store.py:293-315.

The sentinel is deny_all_grants, thoughtmachine/permission_store.py:318-326, and read_grants_or_deny_all, thoughtmachine/permission_store.py:329-361, chooses between it and the designed default on a stated rule: absent identifiers means the default, both identifiers plus any failure means the sentinel, with the reasoning in the docstring at thoughtmachine/permission_store.py:334-346.

Writes are atomic: write_session_permissions, thoughtmachine/permission_store.py:380-445, writes a temp file, replaces it with os.replace, optionally fsyncs file and directory, and raises PermissionStoreError, thoughtmachine/permission_store.py:122-124, on OSError, thoughtmachine/permission_store.py:435-438.

Seeding and migration are separate operations, seed_session_permissions_if_absent, thoughtmachine/permission_store.py:448-482, and migrate_session_permissions, thoughtmachine/permission_store.py:490-537, so a legacy record is upgraded when it is next written rather than when it is read.

What the stored grants mean at call time is Section 8.4; this section stops at the file and at the parse that guards it.

### 13.5 Audit

The audit logger's contract is its module docstring, thoughtmachine/audit_logger.py:1-12: two entry points, audit_event and get_logger, over a RotatingFileHandler of 10MB across three files.

Path resolution is _default_log_path, thoughtmachine/audit_logger.py:49-66, and _resolve_log_path, thoughtmachine/audit_logger.py:69-74.

Failure is once-only and non-fatal: _warn_once, thoughtmachine/audit_logger.py:77-93, and _get_handler, thoughtmachine/audit_logger.py:96-122, warn a single time and then drop events, thoughtmachine/audit_logger.py:120-121.

audit_event, thoughtmachine/audit_logger.py:125-159, writes one line of the shape timestamp, event name, payload, as the module example shows, thoughtmachine/audit_logger.py:174-175; get_logger, thoughtmachine/audit_logger.py:162-181, is the same sink for free-form lines.

There are five call sites and all of them are lifecycle rather than per-call: security setup, thoughtmachine/security.py:22 for the import and :714 for SECURITY_SETUP_DOCKER; container removal in vault garbage collection, thoughtmachine/vault_gc.py:621; container teardown, thoughtmachine/container_record/hook.py:214-216; and worker permission widening, agent/core/tool_executor.py:741, imported with a no-op fallback at agent/core/tool_executor.py:36-40 so that a missing logger cannot break the executor.

The finding is what is absent: audit_event has no call site anywhere under security/, so an individual permission decision is never written to the audit log, and only setup, teardown, garbage collection and widening are.

Combined with the once-only warning, an unavailable audit sink produces a system that looks audited and records nothing, with one line in the log to say so.

### 13.6 The knowledge base enforces by return value

KnowledgeBaseTool, tools/knowledge_base.py:125, is the writer for the knowledge base, and its refusal logic is a string producer: _global_write_refusal, tools/knowledge_base.py:377-403, returns the refusal text rather than raising.

The two rules it encodes are that the system domains are read-only, tools/knowledge_base.py:393-398, and that the global user area is read-only for agents, the refusal naming scope=workspace as the alternative, tools/knowledge_base.py:399-403.

The check reads GLOBAL_KB_DIR from agent/knowledge/global_kb.py at tools/knowledge_base.py:390 before anything is created, so a refused write leaves nothing on disk.

The rest of the path machinery is _get_global_kb_root, tools/knowledge_base.py:43-45, _get_ensure_global_kb, tools/knowledge_base.py:38-40, which initialises on call, _resolve_domain_path, tools/knowledge_base.py:405-514, which for workspace scope checks the built-in DOMAINS and then project/ and personal/, _assert_inside_kb, tools/knowledge_base.py:434-440, and _initialize_kb, tools/knowledge_base.py:516-546.

Writes serialise on _get_file_lock, tools/knowledge_base.py:29-34.

The pattern is worth naming because it recurs: enforcement by return value is enforcement the caller must honour, and the only thing that makes it safe here is that the write mode returns before it touches the disk.

## 14. Failure modes, invariants and the extension guide

### 14.1 Invariants

The invariants are collected here with their citations rather than restated, because each is argued where it is implemented and a second prose copy would only drift.

The executor overwrites injected arguments after validation, Section 8.5, so a model cannot supply the values the executor itself controls.

A refusal is a result, not an exception, Section 8.6, which is why a denied tool call reaches the model as text it can read and react to.

The turn generator's 36 yields, 8 of them terminal-adjacent under the adjacency rule of Section 6, are the complete event vocabulary; any other event a UI sees was synthesised further out.

Worker mode comes from configuration, agent/core/agent.py:167, and never from tool arguments, and the permission footprint is capped at agent/core/tool_executor.py:498-503, so a worker cannot escalate by forging a flag.

With no interactive user the gate denies instead of prompting, security/security_gate.py:1612-1616, and that is the defined behaviour rather than a degradation.

### 14.2 Defects, sorted by how quietly they fail

The list is ordered by silent-ness: the first group reports success while doing nothing.

(1) Silent. Every Permission denied: result is reported to the UI as a success, because success defaults to True at agent/core/agent.py:1327 and the classifier at agent/core/agent.py:1330-1332 matches only a leading cross mark, TOOL CALL REJECTED and Error executing tool.

(2) Silent. ConfigService.validate() returns True unconditionally, agent/config/service.py:225-236, so configuration validation cannot fail.

(3) Silent. register_mcp_tools is fired from the constructor, agent/core/agent.py:161-165, after self.tool_classes is captured at agent/core/agent.py:158 and after tool_definitions is frozen at agent/core/agent.py:166, so background-registered MCP classes never reach a live agent.

(4) Silent. A worker reply's status defaults to ok when the reply is not a dict or cannot be parsed, tools/workspace/worker_query.py:287 and :293, discussed in Section 11.3.

(5) Silent. Permission decisions are not written to the audit log, Section 13.5, so the audit trail cannot answer the question it exists to answer.

(6) Only partly loud. DockerCodeRunner's docstring promises container pooling with idle close, tools/docker_code_runner.py:113-116, and the field is declared, tools/docker_code_runner.py:185-188, while execute is a single shell-out path, tools/docker_code_runner.py:295-506, that builds and uses its own container; the pooling claim is a docstring artefact and no idle timer ever closes anything.

(7) Silent. MCPServerConnect, tools/mcp_server_connect.py:2 and :54 with required_categories at :57, is not reachable through executor resolution, and tools/mcp_manager.py:223-227 only mirrors its grain, so the declared permission surface exists without the call path.

(8) Silent. The trailer convention described in Section 8.7 is unenforced: nothing checks that a resource tool emitted it.

(9) Silent. Three parallel turn-grouping implementations coexist, agent/core/agent.py:1500, agent/core/conversation_manager.py:61-117 and agent/core/message_utils.py:43-49, and nothing asserts they agree.

(10) Silent. Token accounting mixes chars/4 truncation with tiktoken counting, Section 12.2, so the two numbers reported about the same conversation are not measuring the same thing.

One pattern runs through the list: wherever a subsystem defaults to success and signals failure only through an explicit marker, the marker is the part that goes missing.

### 14.3 Adding a tool

Subclass ToolBase, tools/base.py, and declare a name plus the categories the tool needs; the required_categories pattern is the one to copy, tools/mcp_server_connect.py:57, with the provider side at tools/knowledge_base.py:158-171.

Register through TOOL_CLASSES, tools/__init__.py:25, either with the register_tool decorator, tools/__init__.py:62-67, or with register_tool_class, tools/__init__.py:75-83, keeping in mind SIMPLIFIED_TOOL_CLASSES, tools/__init__.py:26, FILE_TOOL_BLACKLIST, tools/__init__.py:30-39, and _update_simplified_toolset, tools/__init__.py:41-60.

Add the class to a preset, AGENT_TOOLS or ENGINEER_TOOLS, following tools/ci_read_tool.py:41-48, because the registry alone does not put a tool in front of a model.

The live path is then the config filter, agent/core/agent.py:158, the frozen definitions, agent/core/agent.py:166, and the executor, agent/core/agent.py:167.

Four gotchas decide whether the tool behaves as written. Output is capped at tools/base.py:312-343, with Respond and SummarizeTool exempt, tools/base.py:100-102.

Injected arguments are overwritten after validation, Section 8.5, so a tool cannot defend itself by checking an argument the executor owns.

The trailer is unenforced, Section 8.7, so a resource tool that omits it looks identical to one that obeyed.

Registration checks class identity, cls not in TOOL_CLASSES at tools/__init__.py:64 and :81, and never name uniqueness, so two classes may share a tool name and the resolution winner is whoever was registered last.

The registry is unlocked module state, as the comment at tools/__init__.py:71-74 says, so registration during a turn is not safe.

### 14.4 Adding a seventh permission grain

Start from the canonical shape, documented at security/security_gate.py:891-894, and add the value to the catalog at security/resource_catalog.py:116-121, since RESOURCE_CATALOG is what makes a grain name legal.

Add the key to the ceiling keys, security/security_gate.py:219-222, and check apply_workspace_ceiling, security/security_gate.py:224-227, so a workspace ceiling can constrain the new grain rather than silently ignoring it.

Teach the store's normalisation about it, thoughtmachine/permission_store.py:222-260, or the grain will be dropped on the next write, and add it to the deny-all sentinel, thoughtmachine/permission_store.py:318-326, or the fail-closed default will be incomplete.

Decide the ask behaviour in the gate and the executor, remembering that a worker turns ask into deny, security/security_gate.py:1612-1616.

Two sites will not need a per-grain change and should not be edited speculatively: tool-declared categories through required_categories, which name resources rather than grains, and the audit call sites, thoughtmachine/security.py:22 and :714, which have no per-grain site to extend.

### 14.5 Adding a second worker mode

A second mode is not a boolean and cannot be added as one. The current controls are four reads of worker_mode, agent/core/agent.py:167, security/security_gate.py:1612-1616, agent/core/tool_executor.py:498-503 and tools/host_bash_tool.py:301-307, plus the labelling at agent/core/worker_context.py:40-53.

A mode enum replaces the boolean, and each of those four sites must be re-read and given an explicit case; the labelling site follows the enum for free.

Anything that reads is_worker_context without being on this list is outside the policy surface, which is why the list is enumerated here rather than described.

### 14.6 Gaps

The following are unverified rather than wrong, and are recorded so they are not mistaken for settled: _resolve_terminal, tools/workspace/worker_query.py:307, is unread, Section 11.3.

No session-scoped force-restart rule was found, Section 11.6.

Whether the three grouping implementations agree is unverified, Section 12.4.

The commit provenance asserted on line 32 of this document is asserted and not verified in this pass.

The in-session increment of _display_turn, agent/core/agent.py:899, is not re-derived from history, and the consequences for a very long session are unverified.

