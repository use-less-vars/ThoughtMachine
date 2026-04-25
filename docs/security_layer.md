Security Layer – Current State (v0.9, pre‑v1.0)
Purpose

Provide capability‑based access control, session‑scoped security policies, and interactive user approval for sensitive tool operations.
Status: Partially implemented; disabled in v1.0 by setting default_policy: "allow". Workspace validation and Docker sandbox remain active.
Architecture Overview
Components

    thoughtmachine/security.py – Core security logic (capability registry, policy evaluation, prompting).

    session/models.py – Session.security_config stores per‑session policies.

    agent/core/tool_executor.py – Calls is_allowed() before tool execution.

    agent/events.py – EventBus for SECURITY_PROMPT / SECURITY_RESPONSE events.

    GUI – No subscription to security events yet (planned for v2.0).

Data Flow
text

ToolExecutor → CapabilityRegistry.check() → is_allowed()
    ↓ (if "ask")
_request_security_prompt() → publish(SECURITY_PROMPT) → wait on queue.Queue (timeout 300s)
    ↓ (response or timeout)
return (approved, remember) → update session.security_config if remember

Key Data Structures
Session Security Config (JSON)
python

{
    "version": 1,
    "session_policy": {
        "read_only": False,
        "allowed_networks": [],        # list of domains or "*"
        "tool_overrides": {},          # {"tool_name": "allow"|"ask"|"deny"}
        "default_policy": "ask"        # "allow", "ask", "deny"
    },
    "agent_overrides": {}              # reserved for multi‑agent
}

Security Profiles (predefined)

Defined in security.py lines 895‑982:

    "default" – default_policy: "ask"

    "read_only" – forces read_only: True

    "file_editor" – allows only fs:read, fs:write

    "sandboxed" – ask for Docker/MCP/git

    "permissive" – default_policy: "allow"

    "restricted" – default_policy: "deny"

Capability Registry

Tools declare requires_capabilities class attribute (list of strings).
Examples: ["fs:read"], ["fs:write"], ["container:exec"], ["mcp:access"], ["git:access"].
Registry built at import time by scanning ToolBase subclasses.
Event Types (agent/events.py)

    EventType.SECURITY_PROMPT – emitted when policy is "ask".
    Payload: request_id, agent_id, tool_name, capabilities, arguments, session_id.

    EventType.SECURITY_RESPONSE – expected from GUI to resume execution.
    Payload: request_id, approved (bool), remember (bool).

Known Issues (Pre‑v1.0)

    No GUI subscription – SECURITY_PROMPT events are published but never handled.
    → Main thread blocks on queue.Queue.get(timeout=300) → times out after 5 minutes → denies tool.

    Default policy "ask" – causes every tool to trigger a prompt → system hangs.
    → Workaround for v1.0: changed default_policy to "allow" in get_default_security_config().

    Blocking in main thread – The agent’s main thread waits for user input. Proper implementation requires async handshake or separate prompt thread.

    EventBus API mismatch – Previously used .emit(); fixed to .publish() (no further issues).

Integration Points for v1.0 (Config Thread)

    The security layer is disabled by policy (default_policy: "allow"). It does not interfere with normal operation.

    Workspace validation (validate_path()) and Docker sandboxing remain active and independent.

    The Session.security_config field exists but is not used by the GUI. It can be ignored for v1.0.

    No changes needed to configuration loading/saving; security config is saved alongside session data but not exposed in UI.

Future Work (v2.0)

    Add GUI dialog for SECURITY_PROMPT events (PyQt6 modal).

    Replace blocking queue with event‑based handshake.

    Expose security panel in GUI (read‑only toggle, network domains, tool overrides).

    Migrate workspace restriction from agent_config.json into security_config.

    Implement hierarchical policies (global → session → agent → tool).

File Reference
File	Key Functions/Classes
thoughtmachine/security.py	CapabilityRegistry, is_allowed(), _request_security_prompt(), get_profile(), get_default_security_config()
agent/events.py	EventBus, EventType, SecurityPromptEvent
agent/core/tool_executor.py	ToolExecutor.execute() – line 155 calls security check
session/models.py	Session.security_config
qt_gui/ (v2.0)	Planned: security_prompt_dialog.py, subscription in main window