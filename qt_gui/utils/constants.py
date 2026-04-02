"""Constants used throughout the GUI module."""
# Maximum length for tool result display before truncation (characters)
MAX_RESULT_LENGTH = 10000
MAX_TOOL_RESULTS_PER_TURN = 10
MAX_LINES_PER_RESULT = 5
ENABLE_RESULT_TRUNCATION = False

# Internal event types that should be hidden from output even with "all" filter
INTERNAL_EVENT_TYPES = [
    "execution_state_change",
    "session_state_change",
    "token_update",
    "turn_update",
    "token_critical_countdown_start",
    "turn_critical_countdown_start",
    "token_critical_countdown_expired",
    "turn_critical_countdown_expired",
    "conversation_update",
    "conversation_prune",
    "file_access",
    "security_violation",
    "docker_sandbox",
    "capability_check",
    "agent_start",
    "agent_end",
    "llm_request",
    "llm_response",
    "raw_response",
]
