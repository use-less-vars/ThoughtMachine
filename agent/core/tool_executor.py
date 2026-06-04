"""
Tool execution and dispatch logic.

Extracted from agent.py to separate tool execution concerns.
"""
import json
import uuid
import queue
import threading
from typing import List, Dict, Any, Optional, Tuple
import tiktoken
from pydantic import ValidationError
from agent.logging import log
from fast_json_repair import loads as repair_loads
from agent.core.turn_transaction import TurnTransaction
from tools.respond import Respond
from tools.summarize_tool import SummarizeTool

# Try to import event system for security prompts
try:
    from agent.events import global_event_bus, EventType, create_event, SecurityPromptEvent
    EVENT_SYSTEM_AVAILABLE = True
except ImportError:
    EVENT_SYSTEM_AVAILABLE = False
    global_event_bus = None
    EventType = None
    create_event = None
    SecurityPromptEvent = None

# Try to import security module for resolve_security_prompt
try:
    from thoughtmachine.security import resolve_security_prompt, _pending_security_requests, _pending_requests_lock
    SECURITY_MODULE_AVAILABLE = True
except ImportError:
    SECURITY_MODULE_AVAILABLE = False
    resolve_security_prompt = None
    _pending_security_requests = None
    _pending_requests_lock = None


# ---------------------------------------------------------------------------
# Default session permissions profile (six categories)
# ---------------------------------------------------------------------------
# Fallback used when no live SessionPermissions model is available on config.
DEFAULT_SESSION_PERMISSIONS = {
    "container": False,
    "network": False,
    "filesystem": "read",
    "security": "read",
    "git": "read",
    "execution": "banned",
}


def _value_satisfies(required: str, allowed: object) -> bool | str:
    """
    Check whether a single required value is satisfied by the allowed setting.

    Returns:
        - ``True`` if permission is granted.
        - ``False`` if permission is denied.
        - ``"ASK"`` if the session value is ``'ask'`` for this category
          and the requested access is non-trivial (user approval needed).

    Supports boolean and string levels:
      - Boolean required 'true' / 'false' matches the allowed bool directly.
      - String levels: 'banned' < 'ask' < 'read' < 'write' < 'full'.
      - Exact string match also works.
    """
    # Normalise required value
    sentinel_ask = "ASK"
    if required.lower() in ("true", "yes"):
        required_val = True
    elif required.lower() in ("false", "no"):
        required_val = False
    else:
        required_val = required  # keep as string (e.g. "read", "write")

    # --- Handle 'ask' session value ---
    allowed_str = str(allowed).lower() if not isinstance(allowed, bool) else ""
    if allowed_str == "ask":
        # 'ask' means: ask the user for anything that isn't banned-equivalent
        if isinstance(required_val, str):
            req_lower = required_val.lower()
            if req_lower in ("false", "no", "banned"):
                return True  # asking for banned is always fine
            if req_lower == "ask":
                return True  # no-op
            return sentinel_ask  # needs user approval
        if isinstance(required_val, bool):
            return True  # bool required with ask — allow (no formal hierarchy)
        return sentinel_ask  # fallback: ask

    if isinstance(allowed, bool):
        if isinstance(required_val, bool):
            return allowed == required_val or allowed is True  # True satisfies everything
        # String required vs bool allowed – True satisfies all, False satisfies nothing
        return allowed is True

    # Both are strings – compare by hierarchy
    _level_map = {"banned": 0, "ask": 1, "read": 2, "write": 3, "full": 4}
    req_level = _level_map.get(str(required_val).lower(), 0)
    all_level = _level_map.get(str(allowed).lower(), 0)
    return all_level >= req_level


def _check_permissions(
    required_categories: list, session_permissions: dict,
    tool_name: str = "", agent_id: int = 0
) -> str | None:
    """
    Check whether *all* required categories are satisfied by the session profile.

    If a category's value is ``'ask'`` we publish a ``SECURITY_PROMPT`` event
    and wait for the user to approve or deny (via a queue that is populated by
    a call to ``resolve_security_prompt()``).

    Args:
        required_categories: List of strings like ["container:true", "filesystem:write"].
        session_permissions: Dict of category → value (bool or str level).
        tool_name: Name of the tool being checked (for prompt context).
        agent_id: ID of the agent (for prompt context).

    Returns:
        None if all checks pass.
        Error message string if any check fails.
    """

    PROMPT_TIMEOUT = 120.0  # seconds to wait for user response

    for req in required_categories:
        if ":" not in req:
            continue  # malformed, skip
        category, required_value = req.split(":", 1)
        allowed = session_permissions.get(category)
        if allowed is None:
            return f"Permission denied: Unknown category '{category}' required by tool"

        result = _value_satisfies(required_value, allowed)
        if result == "ASK":
            # --- Publish security prompt and wait for user approval ---
            request_id = str(uuid.uuid4())
            response_queue = queue.Queue()

            # Register the queue so resolve_security_prompt can find it
            if _pending_security_requests is not None and _pending_requests_lock is not None:
                with _pending_requests_lock:
                    _pending_security_requests[request_id] = response_queue

            # Build the prompt description
            prompt_description = (
                f"Tool '{tool_name}' requires {category}:{required_value} "
                f"(session allows {category}:ask)."
            )

            # Publish SECURITY_PROMPT event if event system is available
            if EVENT_SYSTEM_AVAILABLE and global_event_bus is not None:
                event = SecurityPromptEvent(
                    data={
                        'request_id': request_id,
                        'agent_id': str(agent_id),
                        'tool_name': tool_name,
                        'capabilities': [str(required_value)],
                        'arguments': {req: required_value},
                        'session_id': '',
                    }
                )
                global_event_bus.publish(event)
                log('INFO', 'core.security',
                    f'SECURITY_PROMPT published: {prompt_description} '
                    f'(request_id={request_id})')

            # Wait for response (with timeout)
            try:
                response = response_queue.get(timeout=PROMPT_TIMEOUT)
                if response.get('approved'):
                    log('INFO', 'core.security',
                        f'User APPROVED {category}:{required_value} '
                        f'for tool {tool_name} (request_id={request_id})')
                    continue  # approved — skip to next category
                else:
                    reason = response.get('reason', 'User denied the request')
                    log('INFO', 'core.security',
                        f'User DENIED {category}:{required_value} '
                        f'for tool {tool_name}: {reason}')
                    return (
                        f"Permission denied: {category}:{required_value} required by '"
                        f"{tool_name}' — user denied the request."
                    )
            except queue.Empty:
                log('WARNING', 'core.security',
                    f'Security prompt timed out after {PROMPT_TIMEOUT}s '
                    f'(request_id={request_id})')
                return (
                    f"Permission denied: {category}:{required_value} required by '"
                    f"{tool_name}' — security prompt timed out."
                )
        elif not result:
            return (
                f"Permission denied: Tool requires {category}:{required_value} "
                f"but session allows {category}:{allowed}"
            )

    return None

class ToolExecutor:
    """Handles tool execution, JSON repair, and tool result processing."""

    def __init__(self, tool_classes, config, state, logger=None, security_available=False, agent=None):
        """
        Initialize tool executor.
        
        Args:
            tool_classes: List of tool classes available.
            config: AgentConfig instance.
            state: AgentState instance for tool allowance checking.
            logger: Optional logger instance.
            security_available: Whether security module is available.
            agent: Optional Agent instance for token update callbacks.
        """
        self.tool_classes = tool_classes
        self.config = config
        self.state = state
        self.logger = logger
        self.security_available = security_available
        self.agent = agent

    def execute_tool_calls(self, tool_calls: List[Dict[str, Any]], add_to_conversation_func, update_token_func=None, agent_id: int = 0, turn_transaction: Optional[TurnTransaction]=None) -> Tuple[List[Dict[str, Any]], bool, Optional[Dict[str, Any]], Optional[str], Optional[int]]:
        """
        Execute multiple tool calls from an assistant message.
        
        Args:
            tool_calls: List of tool call dictionaries from LLM.
            add_to_conversation_func: Function to add messages to conversation.
            update_token_func: Deprecated, use agent._update_tokens_after_tool.
            agent_id: ID of the agent for security checks.
            turn_transaction: Optional TurnTransaction to buffer messages (if None, use add_to_conversation_func).
            
        Returns:
            Tuple of:
            - executed_tools: List of executed tool information
            - final_detected: Whether a terminal tool was executed (Respond, Final, etc.)
            - respond_result: Dict with 'response_type' and 'content' if Respond was executed, else None
            - summary_text: Summary text if SummarizeTool was called
            - summary_keep_recent_turns: Number of turns to keep for summarization
        """
        # Resolve update_token_func: if not provided, use agent._update_tokens_after_tool
        if update_token_func is None:
            if self.agent is not None:
                update_token_func = self.agent._update_tokens_after_tool
            else:
                update_token_func = lambda x: None
        executed_tools = []
        final_detected = False
        respond_result = None
        summary_requested = False
        summary_text = None
        summary_keep_recent_turns = 0

        def add_tool_result(message):
            if turn_transaction is not None:
                turn_transaction.add_tool_result(message)
            else:
                add_to_conversation_func(message)
        for tool_call in tool_calls:
            tool_name = tool_call['function']['name']
            if not self.state.is_tool_allowed(tool_name):
                tool_result = self._create_tool_rejection_message(tool_name)
                add_tool_result({'role': 'tool', 'tool_call_id': tool_call['id'], 'content': tool_result})
                tool_tokens = self.agent.token_counter.estimate_tokens(tool_result) if self.agent is not None else self._estimate_tokens_fallback(tool_result)
                update_token_func(tool_tokens)
                executed_tools.append({'name': tool_name, 'arguments': {}, 'result': tool_result})
                continue
            arguments_str = tool_call['function']['arguments']
            try:
                arguments = json.loads(arguments_str)
            except json.JSONDecodeError:
                try:
                    arguments = repair_loads(arguments_str)
                    if self.logger:
                        log('INFO', 'core.tool_executor', f'JSON repaired for {tool_name}')
                except Exception as e:
                    tool_result = f'Invalid JSON in arguments: {e}. Raw: {arguments_str}'
                    if self.logger:
                        self.logger.log_error('JSON_DECODE_ERROR', f'Failed to parse JSON for {tool_name}: {e}')
                    add_tool_result({'role': 'tool', 'tool_call_id': tool_call['id'], 'content': tool_result})
                    executed_tools.append({'name': tool_name, 'arguments': {'error': 'Invalid JSON', 'raw': arguments_str}, 'result': tool_result})
                    continue
            if self.logger:
                self.logger.log_tool_call(tool_name, arguments, tool_call['id'])
            tool_class = next((cls for cls in self.tool_classes if cls.__name__ == tool_name), None)
            if not tool_class:
                error_msg = f'Unknown tool: {tool_name}'
                tool_result = error_msg
                tool_execution_result = {'result': tool_result, 'tool_type': 'normal'}
                tool_type = 'normal'
            else:
                tool_execution_result = self._execute_single_tool(tool_class, arguments, tool_name, agent_id, lambda: summary_requested, lambda: summary_text, lambda: summary_keep_recent_turns)
                tool_result = tool_execution_result['result']
                tool_type = tool_execution_result.get('tool_type', 'normal')
                if tool_type == 'respond':
                    final_detected = True
                    respond_result = {
                        'response_type': tool_execution_result.get('response_type'),
                        'content': tool_execution_result.get('content')
                    }
                elif tool_type == 'summary':
                    summary_requested = True
                    summary_text = tool_execution_result.get('summary_text')
                    summary_keep_recent_turns = tool_execution_result.get('summary_keep_recent_turns')
                    log('DEBUG', 'core.summary', f'SummarizeTool executed: summary length={len(summary_text)}, keep_recent_turns={summary_keep_recent_turns}')
            if self.logger:
                self.logger.log_tool_result(tool_name, tool_result, tool_call['id'])
            tool_result_msg = {'role': 'tool', 'tool_call_id': tool_call['id'], 'content': tool_result}
            if tool_type == 'respond':
                tool_result_msg['response_type'] = tool_execution_result.get('response_type')
            add_tool_result(tool_result_msg)
            tool_tokens = self.agent.token_counter.estimate_tokens(tool_result) if self.agent is not None else self._estimate_tokens_fallback(tool_result)
            update_token_func(tool_tokens)
            if self.logger:
                pass
            executed_tools.append({'name': tool_name, 'arguments': arguments, 'result': tool_result})
        return (executed_tools, final_detected, respond_result, summary_text if summary_requested else None, summary_keep_recent_turns if summary_requested else None)

    def _execute_single_tool(self, tool_class, arguments: Dict[str, Any], tool_name: str, agent_id: int, get_summary_requested, get_summary_text, get_summary_keep_recent_turns) -> Dict[str, Any]:
        """
        Execute a single tool instance.

        Returns:
            Dictionary with keys:
            - result: tool result string
            - tool_type: 'normal', 'respond', or 'summary'
            - response_type: if tool_type == 'respond'
            - content: if tool_type == 'respond'
            - summary_text: if tool_type == 'summary'
            - summary_keep_recent_turns: if tool_type == 'summary'
        """
        try:
            tool_args = arguments.copy()
            # Validate LLM's original arguments BEFORE injecting infrastructure fields.
            # This ensures that if validation fails, the error message shows the LLM's
            # own input (e.g., empty {}) rather than the injected fields like
            # {'workspace_path': '/home/...', 'token_limit': 10000} which makes it
            # look like a system bug.
            try:
                tool_class.model_validate(tool_args)
            except ValidationError as e:
                # Provide LLM-friendly error with valid field names
                try:
                    infra_fields = {"workspace_path", "token_limit", "is_docker", "container_workspace_path", "tool"}
                    valid_fields = [f for f in tool_class.model_fields.keys() if f not in infra_fields]
                    valid_fields_str = ', '.join(valid_fields)
                    return {'result': f'Invalid arguments: {e}\n\nValid fields: {valid_fields_str}', 'tool_type': 'normal'}
                except Exception:
                    return {'result': f'Invalid arguments: {e}', 'tool_type': 'normal'}
            # Now inject infrastructure fields (safe because validation passed)
            if self.config.workspace_path:
                tool_args['workspace_path'] = self.config.workspace_path
            if self.config.tool_output_token_limit is not None:
                tool_args['token_limit'] = self.config.tool_output_token_limit
            # Check permission categories before executing
            session_perms = self.config.session_permissions
            if session_perms is None:
                session_perms_dict = DEFAULT_SESSION_PERMISSIONS
            else:
                session_perms_dict = session_perms.to_dict()
            error = _check_permissions(
                tool_class.get_required_categories(arguments),
                session_perms_dict,
                tool_name=tool_name,
                agent_id=agent_id,
            )
            if error is not None:
                return {'result': error, 'tool_type': 'normal'}
            tool_instance = tool_class(**tool_args)
            if self.logger:
                if hasattr(self.logger, 'py_logger'):
                    tool_instance._set_logger(self.logger.py_logger)
                    if hasattr(tool_instance, '_set_agent_logger'):
                        tool_instance._set_agent_logger(self.logger)
                elif hasattr(self.logger, 'log_tool_debug'):
                    tool_instance._set_logger(self.logger)
                    if hasattr(self.logger, 'log_tool_debug') and hasattr(tool_instance, '_set_agent_logger'):
                        tool_instance._set_agent_logger(self.logger)
            log('DEBUG', 'core.pause', f'TOOL EXECUTE START [{tool_name}]')
            tool_result = tool_instance.execute()
            log('DEBUG', 'core.pause', f'TOOL EXECUTE END [{tool_name}]')
            # Apply framework-level output truncation unless tool opts out
            if not tool_instance.skip_output_truncation:
                tool_result = tool_instance._truncate_output(tool_result)
            if isinstance(tool_instance, Respond):
                return {'result': tool_result, 'tool_type': 'respond', 'response_type': tool_instance.response_type, 'content': tool_result}
            elif isinstance(tool_instance, SummarizeTool):
                return {'result': tool_result, 'tool_type': 'summary', 'summary_text': tool_instance.summary, 'summary_keep_recent_turns': tool_instance.keep_recent_turns}
            else:
                return {'result': tool_result, 'tool_type': 'normal'}
        except ValidationError as e:
            return {'result': f'Invalid arguments: {e}', 'tool_type': 'normal'}
        except Exception as e:
            return {'result': f'Error executing tool: {e}', 'tool_type': 'normal'}

    def close(self):
        """Close and release any resources held by this executor."""
        self.tool_classes = []
        self.agent = None
        self.state = None

    @staticmethod
    def _estimate_tokens_fallback(text: str) -> int:
        """Fallback token estimation using tiktoken when agent is unavailable."""
        try:
            encoder = tiktoken.get_encoding('cl100k_base')
            return len(encoder.encode(str(text)))
        except Exception:
            return len(str(text)) // 4

    def _create_tool_rejection_message(self, tool_name: str) -> str:
        """Create rejection message for disallowed tool calls."""
        allowed_tools = self.state.get_allowed_tools()
        if allowed_tools:
            # Dynamically list all allowed tools
            allowed_list = '\n'.join(f'- {t}' for t in allowed_tools)
            return (
                f"❌ TOOL CALL REJECTED ❌"
                f"\n\nYou attempted to use '{tool_name}', which is currently FORBIDDEN."
                f"\n\nCurrent state: restrictions_active (limit exceeded)"
                f"\nWhy: Token or turn limits exceeded."
                f"\n\nYou may call:\n{allowed_list}"
                f"\n\nPlease use one of the allowed tools now."
            )
        else:
            return f"❌ TOOL CALL REJECTED ❌\n\nYou attempted to use '{tool_name}', which is currently FORBIDDEN.\n\nCurrent state: token_state={self.state.token_state.value}, turn_state={self.state.turn_state.value}\nPossible reasons: Token or turn limits exceeded with active restrictions.\n\nCheck system warnings for required actions."