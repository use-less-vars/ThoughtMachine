"""
Tool execution and dispatch logic.

Extracted from agent.py to separate tool execution concerns.
"""
import json
import threading
from typing import List, Dict, Any, Optional, Tuple
import tiktoken
from pydantic import ValidationError
from agent.logging import log
from agent.logging.lifecycle import log_tool_call_raw
from fast_json_repair import loads as repair_loads
from agent.core.turn_transaction import TurnTransaction
from tools.respond import Respond
from tools.summarize_tool import SummarizeTool

# Try to import event system for security prompts
try:
    from agent.events import global_event_bus
    _EVENT_SYSTEM_AVAILABLE = True
except ImportError:
    _EVENT_SYSTEM_AVAILABLE = False
    global_event_bus = None

# Try to import security module
from thoughtmachine.security import SessionPermissions

# Import unified security gate (always available)
try:
    from security.security_gate import (
        get_workspace_capabilities,
        get_effective_permissions,
        check_required_categories,
    )
    from thoughtmachine.workspace_capabilities import (
        WorkspaceCapabilities,
        resolve_workspace_id,
    )
    GATE_AVAILABLE = True
except ImportError:
    GATE_AVAILABLE = False
    get_workspace_capabilities = None
    get_effective_permissions = None
    check_required_categories = None
    WorkspaceCapabilities = None
    resolve_workspace_id = None

# ---------------------------------------------------------------------------
# Default session permissions profile (seven categories)
# ---------------------------------------------------------------------------
# Fallback used when no live SessionPermissions model is available on config.
DEFAULT_SESSION_PERMISSIONS = {
    "container": False,
    "network": "banned",
    "filesystem": "read",
    "system": "read",
    "git": "read",
    "mcp": "banned",
    "execution": "banned",
}

class ToolExecutor:
    """Handles tool execution, JSON repair, and tool result processing."""

    def __init__(self, tool_classes, config, state, logger=None, security_available=False, agent=None, event_bus=None, is_worker_context=False):
        """
        Initialize tool executor.
        
        Args:
            tool_classes: List of tool classes available.
            config: AgentConfig instance.
            state: AgentState instance for tool allowance checking.
            logger: Optional logger instance.
            security_available: Whether security module is available.
            agent: Optional Agent instance for token update callbacks.
            is_worker_context: Whether this executor runs inside a worker (no interactive user).
        """
        self.tool_classes = tool_classes
        self.config = config
        self.state = state
        self.logger = logger
        self.security_available = security_available
        self.agent = agent
        self._event_bus = event_bus
        self._is_worker_context = is_worker_context

    def execute_tool_calls(self, tool_calls: List[Dict[str, Any]], add_to_conversation_func, update_token_func=None, agent_id: int = 0, session_id: str = "", turn_transaction: Optional[TurnTransaction]=None) -> Tuple[List[Dict[str, Any]], bool, Optional[Dict[str, Any]], Optional[str], Optional[int]]:
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
            - respond_result: Dict with 'response_type', 'content', 'status', 'confidence', 'meta' if Respond was executed, else None
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
            json_repair_needed = False
            try:
                arguments = json.loads(arguments_str)
            except json.JSONDecodeError:
                try:
                    arguments = repair_loads(arguments_str)
                    json_repair_needed = True
                    if self.logger:
                        log('INFO', 'core.tool_executor', f'JSON repaired for {tool_name}')
                except Exception as e:
                    tool_result = f'Invalid JSON in arguments: {e}. Raw: {arguments_str}'
                    if self.logger:
                        self.logger.log_error('JSON_DECODE_ERROR', f'Failed to parse JSON for {tool_name}: {e}')
                    add_tool_result({'role': 'tool', 'tool_call_id': tool_call['id'], 'content': tool_result})
                    executed_tools.append({'name': tool_name, 'arguments': {'error': 'Invalid JSON', 'raw': arguments_str}, 'result': tool_result})
                    log_tool_call_raw(tool_name=tool_name, tool_call_id=tool_call['id'], arguments_raw=arguments_str, json_repair_needed=True, session_id=session_id)
                    continue
            log_tool_call_raw(tool_name=tool_name, tool_call_id=tool_call['id'], arguments_raw=arguments_str, json_repair_needed=json_repair_needed, session_id=session_id)
            if self.logger:
                self.logger.log_tool_call(tool_name, arguments, tool_call['id'])
            tool_class = next((cls for cls in self.tool_classes if cls.__name__ == tool_name), None)
            if not tool_class:
                error_msg = f'Unknown tool: {tool_name}'
                tool_result = error_msg
                tool_execution_result = {'result': tool_result, 'tool_type': 'normal'}
                tool_type = 'normal'
            else:
                tool_execution_result = self._execute_single_tool(tool_class, arguments, tool_name, agent_id, lambda: summary_requested, lambda: summary_text, lambda: summary_keep_recent_turns, session_id=session_id)
                tool_result = tool_execution_result['result']
                tool_type = tool_execution_result.get('tool_type', 'normal')
                if tool_type == 'respond':
                    final_detected = True
                    respond_result = {
                        'response_type': tool_execution_result.get('response_type'),
                        'content': tool_execution_result.get('content'),
                        'status': tool_execution_result.get('status'),
                        'confidence': tool_execution_result.get('confidence'),
                        'meta': tool_execution_result.get('meta'),
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

    def _execute_single_tool(self, tool_class, arguments: Dict[str, Any], tool_name: str, agent_id: int, get_summary_requested, get_summary_text, get_summary_keep_recent_turns, session_id: str = "") -> Dict[str, Any]:
        """
        Execute a single tool instance.

        Returns:
            Dictionary with keys:
            - result: tool result string
            - tool_type: 'normal', 'respond', or 'summary'
            - response_type: if tool_type == 'respond'
            - content: if tool_type == 'respond'
            - status: if tool_type == 'respond'
            - confidence: if tool_type == 'respond'
            - meta: if tool_type == 'respond'
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
            #
            # First, strip any keys that aren't valid model fields.
            # The JSON repair library (fast_json_repair) can create bogus fields like
            # "Number" when the LLM passes malformed JSON (e.g., line_numbers="229-245"
            # written without quotes as 229-245, which isn't valid JSON).
            # These ghost fields trigger "Extra inputs not permitted" errors from Pydantic.
            valid_field_names = set(tool_class.model_fields.keys())
            for extra_key in list(tool_args.keys()):
                if extra_key not in valid_field_names:
                    tool_args.pop(extra_key)
            try:
                tool_class.model_validate(tool_args)
            except ValidationError as e:
                # Provide LLM-friendly error with valid field names
                try:
                    infra_fields = {"workspace_path", "token_limit", "is_docker", "container_workspace_path", "tool", "agent_config", "session_permissions", "session_id"}
                    valid_fields = [f for f in valid_field_names if f not in infra_fields]
                    valid_fields_str = ', '.join(valid_fields)
                    return {'result': f'Invalid arguments: {e}\n\nValid fields: {valid_fields_str}', 'tool_type': 'normal'}
                except Exception:
                    return {'result': f'Invalid arguments: {e}', 'tool_type': 'normal'}
            # Now inject infrastructure fields (safe because validation passed)
            if self.config.workspace_path:
                tool_args['workspace_path'] = self.config.workspace_path
            if self.config.tool_output_token_limit is not None:
                tool_args['token_limit'] = self.config.tool_output_token_limit
            # Check permission categories using the unified security gate
            session_perms_obj = self.config.session_permissions
            if session_perms_obj is None:
                session_perms_obj = SessionPermissions() if SessionPermissions else None

            if GATE_AVAILABLE and session_perms_obj is not None:
                # Resolve workspace ID to load workspace capabilities
                workspace_path = getattr(self.config, 'workspace_path', None)
                ws_id = resolve_workspace_id(workspace_path) if (workspace_path and resolve_workspace_id) else None
                caps = get_workspace_capabilities(ws_id) if ws_id else WorkspaceCapabilities()
                effective = get_effective_permissions(session_perms_obj, caps)

                ok, error_msg = check_required_categories(
                    tool_class.get_required_categories(arguments),
                    effective,
                    tool_name=tool_name,
                    tool_args=arguments,
                    description=getattr(tool_class, 'describe_action', lambda a: '')(arguments),
                    event_bus=self._event_bus or global_event_bus,
                    agent_id=str(agent_id),
                    session_id=session_id,
                    is_worker_context=self._is_worker_context,
                )
                if not ok:
                    return {'result': error_msg, 'tool_type': 'normal'}
            # Inject session permissions dict so the tool can apply
            # fine-grained security (e.g., DockerCodeRunner uses it
            # to decide container network/filesystem modes).
            if session_perms_obj is not None:
                tool_args['session_permissions'] = session_perms_obj.to_dict()
            else:
                tool_args['session_permissions'] = DEFAULT_SESSION_PERMISSIONS

            # Inject effective permissions for in-tool atomic re-checks
            if GATE_AVAILABLE and session_perms_obj is not None:
                tool_args['effective_permissions'] = effective
                tool_args['workspace_id'] = ws_id
            else:
                tool_args['effective_permissions'] = {}

            # Inject agent config for introspection tools
            if hasattr(self, 'config') and self.config is not None:
                tool_args['agent_config'] = {
                    'temperature': getattr(self.config, 'temperature', None),
                    'max_turns': getattr(self.config, 'max_turns', None),
                    'provider': getattr(self.config, 'provider_type', None),
                    'api_key': getattr(self.config, 'api_key', None),
                    'base_url': getattr(self.config, 'base_url', None),
                    'model': getattr(self.config, 'model', None),
                    'tool_output_token_limit': getattr(self.config, 'tool_output_token_limit', None),
                    'token_monitor_warning_threshold': getattr(self.config, 'token_monitor_warning_threshold', None),
                    'token_monitor_critical_threshold': getattr(self.config, 'token_monitor_critical_threshold', None),
                    'use_workspace_lifecycle_manager': getattr(self.config, 'use_workspace_lifecycle_manager', False),
                    'use_container_registry': getattr(self.config, 'use_container_registry', False),
                }
            else:
                tool_args['agent_config'] = None

            # Inject session_id only for tools that support it (e.g. Worker)
            if 'session_id' in valid_field_names:
                tool_args['session_id'] = session_id

            # Credential injection: resolve {{credential:...}} placeholders in tool args
            if tool_args:
                try:
                    from agent.credentials import CredentialInjector
                    # Get workspace_id from the session context already available
                    ws_id = None
                    if 'session_permissions' in tool_args and isinstance(tool_args['session_permissions'], dict):
                        ws_id = tool_args['session_permissions'].get('workspace_id')
                    if not ws_id and 'workspace_id' in tool_args:
                        ws_id = tool_args['workspace_id']
                    if ws_id:
                        injector = CredentialInjector(ws_id)
                        tool_args = injector.inject(tool_args)
                except Exception as exc:
                    log('ERROR', 'core.credentials', "Credential injection failed for tool '%s': %s", tool_name, exc)
                    return {'result': f"Credential injection failed: {exc}", 'tool_type': 'normal'}

            try:
                tool_instance = tool_class(**tool_args)
            except ValidationError:
                # Safety net: if an injected infra field isn't accepted by this
                # tool (e.g. a field was added unconditionally instead of gated),
                # strip any key not in the model and retry once.
                valid = set(tool_class.model_fields.keys())
                for key in list(tool_args.keys()):
                    if key not in valid:
                        tool_args.pop(key)
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
                return {
                    'result': tool_result,
                    'tool_type': 'respond',
                    'response_type': tool_instance.response_type,
                    'content': tool_result,
                    'status': tool_instance.status,
                    'confidence': tool_instance.confidence,
                    'meta': tool_instance.meta,
                }
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
        """Create concise rejection message for disallowed tool calls.
        
        Includes the restriction reason so the LLM understands why the tool
        was blocked (e.g., 'timeout', 'token', 'turn') rather than retrying
        the same call blindly.
        """
        allowed_tools = self.state.get_allowed_tools()
        restriction_reason = getattr(self.state, 'restriction_reason', None)
        if allowed_tools:
            allowed_list = ', '.join(allowed_tools)
            if restriction_reason:
                return f"Tool '{tool_name}' not allowed. Reason: {restriction_reason}. Available tools: {allowed_list}"
            return f"Tool not allowed. Available tools: {allowed_list}"
        else:
            return f"Tool not allowed. Available tools: none"